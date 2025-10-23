from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, TextIO, Tuple

from .errors import IPCError


READY_PREFIX = "PCTX:READY "
RESP_PREFIX = "PCTX:RESP "
EXC_PREFIX = "PCTX:EXC "
BYE_PREFIX = "PCTX:BYE "


def generate_req_id() -> str:
    return uuid.uuid4().hex


def parse_service_line(line: str) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    if not line.startswith("PCTX:"):
        return None, None
    if line.startswith(READY_PREFIX):
        raw = line[len(READY_PREFIX) :]
        try:
            payload = json.loads(raw) if raw else {}
        except Exception:
            payload = {}
        return "READY", payload
    if line.startswith(RESP_PREFIX):
        raw = line[len(RESP_PREFIX) :]
        try:
            payload = json.loads(raw) if raw else {}
        except Exception:
            payload = {}
        return "RESP", payload
    if line.startswith(EXC_PREFIX):
        raw = line[len(EXC_PREFIX) :]
        try:
            payload = json.loads(raw) if raw else {}
        except Exception:
            payload = {}
        return "EXC", payload
    if line.startswith(BYE_PREFIX):
        raw = line[len(BYE_PREFIX) :]
        try:
            payload = json.loads(raw) if raw else {}
        except Exception:
            payload = {}
        return "BYE", payload
    return None, None


@dataclass
class ServiceResponse:
    ok: bool
    req_id: Optional[str]
    result: Any = None
    error_type: Optional[str] = None
    error_message: Optional[str] = None
    traceback: Optional[str] = None


@dataclass
class ReadyInfo:
    ok: bool = True
    entry: Optional[str] = None
    data: Dict[str, Any] = None  # type: ignore[assignment]


@dataclass
class ByeInfo:
    ok: bool = True
    reason: Optional[str] = None
    data: Dict[str, Any] = None  # type: ignore[assignment]


class ServiceSession:
    """
    Сеанс взаимодействия с сервисным воркером через stdin/stdout (JSON-lines).

    Использование:
      sess = ServiceSession(stdin=proc.stdin, stdout=proc.stdout, on_raw=logger)
      sess.start()
      sess.wait_ready(timeout=10.0)
      resp = sess.request(inputs=[...], params={...}, ctx={...}, timeout=5.0, entry="custom_func")
      sess.shutdown(grace_timeout=3.0)
      sess.stop()
    """

    def __init__(
        self,
        stdin: TextIO,
        stdout: TextIO,
        on_raw: Optional[Callable[[str], None]] = None,
    ) -> None:
        if stdin is None or stdout is None:
            raise IPCError("Ожидались валидные stdin/stdout для ServiceSession")

        self._stdin = stdin
        self._stdout = stdout
        self._on_raw = on_raw

        self._reader_thread: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()

        self._ready_evt = threading.Event()
        self._ready_info: Optional[ReadyInfo] = None

        self._bye_evt = threading.Event()
        self._bye_info: Optional[ByeInfo] = None

        self._pending: Dict[str, Dict[str, Any]] = {}
        self._pending_lock = threading.Lock()

        self._write_lock = threading.Lock()

    def _reader_loop(self) -> None:
        try:
            for raw in self._stdout:
                if self._stop_evt.is_set():
                    break
                line = raw.rstrip("\r\n")
                if self._on_raw:
                    try:
                        self._on_raw(line)
                    except Exception:
                        pass
                tag, payload = parse_service_line(line)
                if tag is None:
                    continue
                if tag == "READY":
                    info = ReadyInfo(
                        ok=bool(payload.get("ok", True)),
                        entry=payload.get("entry"),
                        data=payload or {},
                    )
                    self._ready_info = info
                    self._ready_evt.set()
                elif tag == "RESP":
                    req_id = payload.get("req_id")
                    with self._pending_lock:
                        slot = self._pending.get(req_id)
                    if slot is None:
                        continue
                    slot["resp"] = ServiceResponse(
                        ok=True, req_id=req_id, result=payload.get("result")
                    )
                    evt = slot["evt"]
                    evt.set()
                elif tag == "EXC":
                    req_id = payload.get("req_id")
                    resp = ServiceResponse(
                        ok=False,
                        req_id=req_id,
                        result=None,
                        error_type=str(payload.get("error_type") or "Error"),
                        error_message=str(payload.get("error_message") or ""),
                        traceback=str(payload.get("traceback") or ""),
                    )
                    if req_id:
                        with self._pending_lock:
                            slot = self._pending.get(req_id)
                        if slot is not None:
                            slot["resp"] = resp
                            slot["evt"].set()
                    else:
                        if not self._ready_evt.is_set():
                            self._ready_info = ReadyInfo(
                                ok=False, entry=None, data={"error": resp.error_message}
                            )
                            self._ready_evt.set()
                elif tag == "BYE":
                    info = ByeInfo(
                        ok=bool(payload.get("ok", True)),
                        reason=payload.get("reason"),
                        data=payload or {},
                    )
                    self._bye_info = info
                    self._bye_evt.set()
        except Exception:
            self._ready_evt.set()
            self._bye_evt.set()
            with self._pending_lock:
                for slot in self._pending.values():
                    if "resp" not in slot:
                        slot["resp"] = ServiceResponse(
                            ok=False,
                            req_id=slot.get("req_id"),
                            error_type="IPCError",
                            error_message="connection lost",
                        )
                        slot["evt"].set()
        finally:
            self._ready_evt.set()
            self._bye_evt.set()
            with self._pending_lock:
                for slot in self._pending.values():
                    slot["evt"].set()

    def start(self) -> None:
        if self._reader_thread is not None:
            return
        self._stop_evt.clear()
        t = threading.Thread(
            target=self._reader_loop, name="ServiceSessionReader", daemon=True
        )
        self._reader_thread = t
        t.start()

    def stop(self) -> None:
        self._stop_evt.set()
        t = self._reader_thread
        if t is not None:
            t.join(timeout=1.0)
        self._reader_thread = None

    def _send(self, obj: Dict[str, Any]) -> None:
        line = json.dumps(obj, ensure_ascii=False)
        with self._write_lock:
            try:
                self._stdin.write(line + "\n")
                self._stdin.flush()
            except Exception as e:
                raise IPCError(f"Не удалось написать в stdin сервиса: {e}") from e

    def wait_ready(self, timeout: Optional[float] = None) -> Optional[ReadyInfo]:
        if self._ready_evt.wait(timeout=timeout):
            return self._ready_info
        return None

    def request(
        self,
        inputs,
        params,
        ctx=None,
        timeout: Optional[float] = None,
        entry: Optional[str] = None,
    ) -> ServiceResponse:
        """
        Отправляет запрос в сервис. Если указать entry, будет вызвана соответствующая функция сервиса;
        иначе — функция по умолчанию (обычно pcontext_request).
        """
        req_id = generate_req_id()
        slot = {"evt": threading.Event(), "resp": None, "req_id": req_id}
        with self._pending_lock:
            self._pending[req_id] = slot
        try:
            payload = {
                "op": "request",
                "req_id": req_id,
                "inputs": inputs,
                "params": params,
                "ctx": ctx,
            }
            if entry:
                payload["entry"] = entry
            self._send(payload)
        except Exception:
            with self._pending_lock:
                self._pending.pop(req_id, None)
            raise

        ok = slot["evt"].wait(timeout=timeout)
        with self._pending_lock:
            self._pending.pop(req_id, None)

        if not ok or slot["resp"] is None:
            raise IPCError("Таймаут ожидания ответа сервиса")
        return slot["resp"]  # type: ignore[return-value]

    def ping(self, timeout: Optional[float] = 2.0) -> bool:
        req_id = generate_req_id()
        slot = {"evt": threading.Event(), "resp": None, "req_id": req_id}
        with self._pending_lock:
            self._pending[req_id] = slot
        try:
            self._send({"op": "ping", "req_id": req_id})
        except Exception:
            with self._pending_lock:
                self._pending.pop(req_id, None)
            return False

        if not slot["evt"].wait(timeout=timeout):
            with self._pending_lock:
                self._pending.pop(req_id, None)
            return False

        resp: ServiceResponse = slot["resp"]  # type: ignore[assignment]
        return resp.ok

    def shutdown(self, timeout: Optional[float] = 3.0) -> bool:
        try:
            self._send({"op": "shutdown", "req_id": generate_req_id()})
        except Exception:
            return False
        if timeout is None:
            timeout = 3.0
        return self._bye_evt.wait(timeout=timeout)


__all__ = [
    "READY_PREFIX",
    "RESP_PREFIX",
    "EXC_PREFIX",
    "BYE_PREFIX",
    "generate_req_id",
    "parse_service_line",
    "ServiceResponse",
    "ReadyInfo",
    "ByeInfo",
    "ServiceSession",
]
