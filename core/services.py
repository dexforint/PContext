from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from .config import EnvMode, cache_dir
from .envs import EnvHandle, ensure_environment
from .errors import IPCError, ServiceError
from .ipc import ReadyInfo, ServiceResponse, ServiceSession
from .logs import RunLog, open_run_log
from .params import coerce_all_params
from .types import Input, ScriptMeta
from .utils import generate_run_id, summarize_inputs


def _inputs_to_jsonable(inputs: Sequence[Input]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for i in inputs:
        out.append(
            {
                "type": i.kind.value,
                "path": str(i.path) if i.path else None,
                "name": i.name,
                "mime": i.mime,
                "size": i.size,
                "created": i.created_ts,
                "modified": i.modified_ts,
            }
        )
    return out


def _creation_flags() -> int:
    if os.name == "nt":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return 0


def _service_worker_path() -> Path:
    return Path(__file__).resolve().parent.parent / "workers" / "service_worker.py"


@dataclass
class ServiceHandle:
    id: str
    meta: ScriptMeta
    env: EnvHandle
    process: subprocess.Popen  # type: ignore[type-arg]
    session: ServiceSession
    run_id: str
    tmp_dir: Path
    ctx: Dict[str, Any]
    start_ts: float
    last_used_ts: float
    log: RunLog
    shutting_down: bool = False

    def is_alive(self) -> bool:
        return self.process.poll() is None


class ServiceManager:
    def __init__(
        self,
        env_mode: EnvMode = EnvMode.CACHED,
        resolver: Optional[Callable[[str], Optional[ScriptMeta]]] = None,
        idle_check_interval: float = 10.0,
    ) -> None:
        self.env_mode = env_mode
        self.resolver = resolver
        self.idle_check_interval = idle_check_interval

        self._services: Dict[str, ServiceHandle] = {}
        self._lock = threading.RLock()

        self._idle_evt = threading.Event()
        self._idle_thread = threading.Thread(
            target=self._idle_loop, name="ServiceIdleGC", daemon=True
        )
        self._idle_thread.start()

    def get(self, script_id: str) -> Optional[ServiceHandle]:
        with self._lock:
            h = self._services.get(script_id)
            if h and h.is_alive():
                return h
            return None

    def ensure_dependencies(self, meta: ScriptMeta) -> None:
        deps = list(getattr(meta.depends, "scripts", []) or [])
        if not deps or self.resolver is None:
            return
        for sid in deps:
            dep_meta = self.resolver(sid)
            if dep_meta is None:
                continue
            if dep_meta.type.value != "service":
                continue
            self.get_or_start(dep_meta)

    def get_or_start(
        self,
        meta: ScriptMeta,
        init_params: Optional[Mapping[str, Any]] = None,
        cwd: Optional[Path] = None,
        ready_timeout: float = 60.0,
    ) -> ServiceHandle:
        sid = meta.stable_id
        with self._lock:
            handle = self._services.get(sid)
            if handle and handle.is_alive():
                return handle
        return self._start_service(
            meta, init_params=init_params, cwd=cwd, ready_timeout=ready_timeout
        )

    def request(
        self,
        meta: ScriptMeta,
        inputs: Sequence[Input],
        params_overrides: Optional[Mapping[str, Any]] = None,
        request_timeout: Optional[float] = None,
        cwd: Optional[Path] = None,
        request_entry: Optional[str] = None,
    ) -> ServiceResponse:
        """
        Выполняет запрос к сервису meta: вызов функции request_entry (или pcontext_request по умолчанию).
        """
        self.ensure_dependencies(meta)
        handle = self.get_or_start(meta, cwd=cwd)
        params_values = coerce_all_params(meta, params_overrides)
        jinputs = _inputs_to_jsonable(inputs)
        try:
            resp = handle.session.request(
                jinputs,
                params_values,
                ctx=None,
                timeout=request_timeout,
                entry=request_entry,
            )
            handle.last_used_ts = time.time()
            return resp
        except IPCError:
            try:
                self._stop_handle(handle, reason="restart", grace_timeout=2.0)
            except Exception:
                pass
            handle = self._start_service(
                meta, init_params=None, cwd=cwd, ready_timeout=30.0
            )
            resp = handle.session.request(
                jinputs,
                params_values,
                ctx=None,
                timeout=request_timeout,
                entry=request_entry,
            )
            handle.last_used_ts = time.time()
            return resp

    def shutdown(
        self, meta_or_id: str | ScriptMeta, grace_timeout: float = 3.0
    ) -> bool:
        sid = meta_or_id if isinstance(meta_or_id, str) else meta_or_id.stable_id
        with self._lock:
            handle = self._services.get(sid)
            if not handle:
                return True
        return self._stop_handle(handle, reason="shutdown", grace_timeout=grace_timeout)

    def shutdown_all(self, grace_timeout: float = 3.0) -> None:
        with self._lock:
            handles = list(self._services.values())
        for h in handles:
            try:
                self._stop_handle(h, reason="shutdown_all", grace_timeout=grace_timeout)
            except Exception:
                pass

    def prune_idle(self) -> List[str]:
        stopped: List[str] = []
        now = time.time()
        with self._lock:
            handles = list(self._services.values())
        for h in handles:
            ttl = h.meta.timeout.service_idle_seconds or None
            if not ttl or ttl <= 0:
                continue
            idle = now - h.last_used_ts
            if idle > ttl:
                try:
                    self._stop_handle(h, reason=f"idle>{int(ttl)}s", grace_timeout=2.0)
                    stopped.append(h.id)
                except Exception:
                    pass
        return stopped

    def _start_service(
        self,
        meta: ScriptMeta,
        init_params: Optional[Mapping[str, Any]] = None,
        cwd: Optional[Path] = None,
        ready_timeout: float = 60.0,
    ) -> ServiceHandle:
        run_id = generate_run_id()
        base_tmp_dir = cache_dir() / "runs" / "services" / meta.stable_id / run_id
        base_tmp_dir.mkdir(parents=True, exist_ok=True)
        cancel_flag_path = base_tmp_dir / "cancel.flag"

        ctx: Dict[str, Any] = {
            "run_id": run_id,
            "os": ("windows" if os.name == "nt" else "linux"),
            "user": _safe_username(),
            "cwd": str(cwd) if cwd else None,
            "tmp_dir": str(base_tmp_dir),
            "cache_dir": str(cache_dir()),
            "log_file": None,
            "cancel_flag_path": str(cancel_flag_path),
        }

        rl = open_run_log(
            meta,
            run_id,
            inputs_summary="service init (background)",
            params_values=dict(init_params or {}),
        )

        def env_log(line: str) -> None:
            rl.log_out(f"[env] {line}")

        env_handle = ensure_environment(meta, self.env_mode, on_output=env_log)

        entries = {
            "init": "pcontext_init",
            "request": "pcontext_request",
            "shutdown": "pcontext_shutdown",
        }
        payload = {
            "script_path": str(meta.file_path),
            "entries": entries,
            "init_params": dict(init_params or {}),
            "ctx": ctx,
        }
        payload_path = base_tmp_dir / "payload.json"
        payload_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        worker = (
            Path(__file__).resolve().parent.parent / "workers" / "service_worker.py"
        )
        cmd = [str(env_handle.python), str(worker), "--payload", str(payload_path)]

        creationflags = _creation_flags()
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            universal_newlines=True,
            cwd=str(cwd) if cwd else None,
            creationflags=creationflags,
        )
        assert proc.stdin is not None and proc.stdout is not None

        def on_raw(line: str) -> None:
            rl.log_out(line)

        sess = ServiceSession(stdin=proc.stdin, stdout=proc.stdout, on_raw=on_raw)
        sess.start()
        ready = sess.wait_ready(timeout=ready_timeout)
        if not ready or (isinstance(ready, ReadyInfo) and not ready.ok):
            try:
                sess.stop()
            except Exception:
                pass
            try:
                _terminate_tree(proc)
            except Exception:
                pass
            rl.log_err("[ready] сервис не инициализировался")
            rl.finalize(
                status=_RunStatusLike.ERROR,
                elapsed_seconds=0.0,
                result_summary=None,
                error_brief="service not ready",
            )
            rl.close()
            raise ServiceError(f"Сервис '{meta.name}' не инициализировался")

        ctx["log_file"] = str(rl.path) if rl.path else None

        handle = ServiceHandle(
            id=meta.stable_id,
            meta=meta,
            env=env_handle,
            process=proc,
            session=sess,
            run_id=run_id,
            tmp_dir=base_tmp_dir,
            ctx=ctx,
            start_ts=time.time(),
            last_used_ts=time.time(),
            log=rl,
        )
        with self._lock:
            self._services[meta.stable_id] = handle
        rl.log_out("[ready] service is up")
        return handle

    def _stop_handle(
        self, handle: ServiceHandle, reason: str, grace_timeout: float = 3.0
    ) -> bool:
        with self._lock:
            if handle.shutting_down:
                return True
            handle.shutting_down = True

        ok = False
        try:
            try:
                ok = handle.session.shutdown(timeout=grace_timeout)
            except Exception:
                ok = False
            handle.session.stop()
            try:
                handle.process.wait(timeout=1.0)
            except Exception:
                pass
            if handle.process.poll() is None:
                _terminate_tree(handle.process)
            ok = True
        finally:
            elapsed = max(0.0, time.time() - handle.start_ts)
            try:
                handle.log.finalize(
                    status=_RunStatusLike.OK if ok else _RunStatusLike.ERROR,
                    elapsed_seconds=elapsed,
                    result_summary=f"service stopped ({reason})",
                    error_brief=None if ok else f"failed to stop ({reason})",
                )
                handle.log.close()
            except Exception:
                pass
            with self._lock:
                self._services.pop(handle.id, None)
        return ok

    def _idle_loop(self) -> None:
        while not self._idle_evt.wait(timeout=self.idle_check_interval):
            try:
                self.prune_idle()
            except Exception:
                pass

    def close(self) -> None:
        self._idle_evt.set()
        try:
            self._idle_thread.join(timeout=1.5)
        except Exception:
            pass


def _safe_username() -> str:
    try:
        import getpass

        return getpass.getuser()
    except Exception:
        return "user"


def _terminate_tree(proc: subprocess.Popen) -> None:
    try:
        import psutil  # type: ignore
    except Exception:
        psutil = None  # type: ignore

    if psutil is not None:
        try:
            p = psutil.Process(proc.pid)  # type: ignore[attr-defined]
            children = p.children(recursive=True)
            for ch in children:
                try:
                    ch.terminate()
                except Exception:
                    pass
            try:
                p.terminate()
            except Exception:
                pass
            gone, alive = psutil.wait_procs(children + [p], timeout=3.0)  # type: ignore[operator]
            for a in alive:
                try:
                    a.kill()
                except Exception:
                    pass
            return
        except Exception:
            pass

    try:
        proc.terminate()
    except Exception:
        pass
    try:
        proc.wait(timeout=3.0)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


class _RunStatusLike:
    OK = type("E", (), {"value": "OK"})()
    ERROR = type("E", (), {"value": "ERROR"})()
