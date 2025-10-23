from __future__ import annotations

import json
import os
import platform
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from .errors import ConfigError


try:
    import yaml  # type: ignore
except Exception:
    yaml = None  # YAML опционален для конфигурации


APP_NAME_WIN = "PContext"
APP_NAME_UNIX = "pcontext"


class EnvMode(str, Enum):
    CACHED = "cached"  # venv по хэшу зависимостей (общий кэш)
    PER_SCRIPT = "per-script"  # отдельный venv на скрипт


@dataclass
class Config:
    """
    Основной конфиг PContext.
    """

    version: int = 1

    # Где искать пользовательские скрипты
    script_dirs: List[Path] = field(default_factory=list)

    # Режим управления окружениями и зависимостями
    env_mode: EnvMode = EnvMode.CACHED

    # Авто-открытие результатов (можно переопределять пер-скрипту)
    auto_open_result: bool = True

    # Настройки pip (опционально)
    pip_index_url: Optional[str] = None
    pip_extra_index_urls: List[str] = field(default_factory=list)
    pip_proxy: Optional[str] = None

    # Нотификации (системные уведомления)
    notifications: bool = True

    # Ограничение параллелизма по ресурсным «замкам»
    # Пример: {"GPU": 1, "GPU:0": 1}
    locks_concurrency: Dict[str, int] = field(default_factory=lambda: {"GPU": 1})

    # Внутренние поля (не сериализуемые):
    _loaded_from: Optional[Path] = field(default=None, repr=False, compare=False)

    def to_dict(self) -> Dict[str, Any]:
        def conv(v: Any) -> Any:
            if isinstance(v, Path):
                return str(v)
            if isinstance(v, Enum):
                return v.value
            if isinstance(v, list):
                return [conv(x) for x in v]
            if isinstance(v, dict):
                return {str(k): conv(x) for k, x in v.items()}
            return v

        d = asdict(self)
        # Удаляем служебные поля
        d.pop("_loaded_from", None)
        # Конверсия типов
        for k, v in list(d.items()):
            d[k] = conv(v)
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Config":
        def to_path_list(v: Any) -> List[Path]:
            out: List[Path] = []
            if isinstance(v, list):
                for x in v:
                    if isinstance(x, (str, Path)):
                        out.append(Path(x))
            return out

        cfg = Config()
        cfg.version = int(d.get("version", 1))
        cfg.script_dirs = (
            to_path_list(d.get("script_dirs", [])) or default_script_dirs()
        )
        env_mode = str(d.get("env_mode", EnvMode.CACHED.value))
        cfg.env_mode = (
            EnvMode(env_mode)
            if env_mode in (e.value for e in EnvMode)
            else EnvMode.CACHED
        )
        cfg.auto_open_result = bool(d.get("auto_open_result", True))

        cfg.pip_index_url = d.get("pip_index_url") or None
        cfg.pip_extra_index_urls = list(d.get("pip_extra_index_urls", []) or [])
        cfg.pip_proxy = d.get("pip_proxy") or None

        cfg.notifications = bool(d.get("notifications", True))
        locks = d.get("locks_concurrency", None)
        if isinstance(locks, dict):
            # Приводим ключи к строкам, значения к int>=1
            parsed: Dict[str, int] = {}
            for k, v in locks.items():
                try:
                    iv = int(v)
                except Exception:
                    continue
                if iv < 1:
                    iv = 1
                parsed[str(k)] = iv
            if parsed:
                cfg.locks_concurrency = parsed
        return cfg


# -----------------------------
# Пути и директории приложения
# -----------------------------


def is_windows() -> bool:
    return platform.system().lower().startswith("win")


def config_dir() -> Path:
    """
    Директория конфигурации.
    - Windows: %APPDATA%/PContext
    - Linux: ~/.config/pcontext
    """
    if is_windows():
        base = os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))
        return Path(base) / APP_NAME_WIN
    else:
        return Path.home() / ".config" / APP_NAME_UNIX


def data_dir() -> Path:
    """
    Директория данных/состояния.
    - Windows: %LOCALAPPDATA%/PContext
    - Linux: ~/.local/share/pcontext
    """
    if is_windows():
        base = os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
        return Path(base) / APP_NAME_WIN
    else:
        return Path.home() / ".local" / "share" / APP_NAME_UNIX


def cache_dir() -> Path:
    """
    Директория кэша.
    - Windows: %LOCALAPPDATA%/PContext/cache (используем общий base, но выделяем поддиректорию cache)
    - Linux: ~/.cache/pcontext
    """
    if is_windows():
        return data_dir() / "cache"
    else:
        return Path.home() / ".cache" / APP_NAME_UNIX


def logs_dir() -> Path:
    """
    Директория логов.
    - Windows: %LOCALAPPDATA%/PContext/logs
    - Linux: ~/.cache/pcontext/logs
    """
    if is_windows():
        return data_dir() / "logs"
    else:
        return cache_dir() / "logs"


def venvs_root() -> Path:
    """
    Корень виртуальных окружений.
    - Windows: %LOCALAPPDATA%/PContext/venvs
    - Linux: ~/.cache/pcontext/venvs
    """
    if is_windows():
        return data_dir() / "venvs"
    else:
        return cache_dir() / "venvs"


def wheels_cache_dir() -> Path:
    """
    Кэш wheel-пакетов для ускорения установки зависимостей.
    """
    return cache_dir() / "wheels"


def default_script_dirs() -> List[Path]:
    """
    Директории по умолчанию для пользовательских скриптов.
    - Windows: ~/Documents/PContext/scripts
    - Linux:  ~/PContext/scripts
    """
    if is_windows():
        base = Path.home() / "Documents" / "PContext" / "scripts"
    else:
        base = Path.home() / "PContext" / "scripts"
    return [base]


def ensure_app_dirs() -> None:
    """
    Создает базовые директории приложения при необходимости.
    """
    for p in [
        config_dir(),
        data_dir(),
        cache_dir(),
        logs_dir(),
        venvs_root(),
        wheels_cache_dir(),
    ]:
        p.mkdir(parents=True, exist_ok=True)
    # Предпочитаемые директории скриптов (не требуются обязательно, но создадим если указаны)
    for p in default_script_dirs():
        p.mkdir(parents=True, exist_ok=True)


def config_file_path() -> Path:
    """
    Путь к файлу конфигурации.
    - используем YAML, если доступен; иначе JSON (расширение .json)
    """
    d = config_dir()
    yaml_path = d / "config.yaml"
    json_path = d / "config.json"
    if yaml is not None:
        return yaml_path
    else:
        return json_path


# -----------------------------
# Загрузка/сохранение конфигурации
# -----------------------------


def load_config(create_if_missing: bool = True) -> Config:
    """
    Загружает конфиг. Если файла нет — создает с настройками по умолчанию.
    """
    ensure_app_dirs()
    path = config_file_path()
    if not path.exists():
        cfg = Config(script_dirs=default_script_dirs())
        cfg._loaded_from = path
        if create_if_missing:
            save_config(cfg)
        return cfg

    try:
        if path.suffix.lower() in (".yaml", ".yml"):
            if yaml is None:
                # Файл yaml, но модуль отсутствует — попробуем как JSON на всякий случай
                with path.open("r", encoding="utf-8") as f:
                    raw = f.read()
                try:
                    d = json.loads(raw)
                except Exception:
                    raise ConfigError("Не установлен PyYAML для чтения config.yaml")
            else:
                with path.open("r", encoding="utf-8") as f:
                    d = yaml.safe_load(f) or {}
        else:
            with path.open("r", encoding="utf-8") as f:
                d = json.load(f)
    except Exception as e:
        raise ConfigError(f"Ошибка чтения конфигурации {path}: {e}")

    if not isinstance(d, dict):
        raise ConfigError("Формат конфигурации должен быть объектом (mapping)")

    cfg = Config.from_dict(d)
    cfg._loaded_from = path

    # Гарантируем, что все директории из конфигурации существуют
    for p in cfg.script_dirs:
        try:
            Path(p).mkdir(parents=True, exist_ok=True)
        except Exception:
            # Не критично — просто пропустим
            pass

    return cfg


def save_config(cfg: Config) -> None:
    """
    Сохраняет конфиг на диск. Формат зависит от доступности YAML.
    """
    ensure_app_dirs()
    path = cfg._loaded_from or config_file_path()
    d = cfg.to_dict()

    try:
        if path.suffix.lower() in (".yaml", ".yml"):
            if yaml is None:
                # Если yaml недоступен, переключаемся на JSON
                path = path.with_suffix(".json")
                with path.open("w", encoding="utf-8") as f:
                    json.dump(d, f, ensure_ascii=False, indent=2)
            else:
                with path.open("w", encoding="utf-8") as f:
                    yaml.safe_dump(d, f, sort_keys=False, allow_unicode=True)
        else:
            with path.open("w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception as e:
        raise ConfigError(f"Не удалось сохранить конфигурацию {path}: {e}")


__all__ = [
    "EnvMode",
    "Config",
    "is_windows",
    "config_dir",
    "data_dir",
    "cache_dir",
    "logs_dir",
    "venvs_root",
    "wheels_cache_dir",
    "default_script_dirs",
    "ensure_app_dirs",
    "config_file_path",
    "load_config",
    "save_config",
]
