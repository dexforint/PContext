from __future__ import annotations

import traceback
from pathlib import Path
from typing import Optional


class PContextError(Exception):
    """
    Базовое исключение для всех ошибок PContext.
    """

    pass


class MetadataError(PContextError):
    """
    Ошибка чтения/валидации метаданных скрипта.
    """

    def __init__(self, message: str, file_path: Optional[Path] = None):
        self.file_path = Path(file_path) if file_path else None
        super().__init__(message)

    def __str__(self) -> str:
        base = super().__str__()
        if self.file_path:
            return f"{base} [script: {self.file_path}]"
        return base


class DependencyError(PContextError):
    """
    Ошибка установки зависимостей (pip) или их разрешения.
    """

    pass


class EnvironmentSetupError(PContextError):
    """
    Ошибка создания/инициализации виртуального окружения.
    """

    pass


class RunnerError(PContextError):
    """
    Ошибка выполнения одноразового скрипта (oneshot).
    """

    pass


class ServiceError(PContextError):
    """
    Ошибка управления сервисом (инициализация/запрос/остановка).
    """

    pass


class IPCError(PContextError):
    """
    Ошибка межпроцессного взаимодействия (IPC).
    """

    pass


class OSIntegrationError(PContextError):
    """
    Ошибка интеграции с ОС (реестр Windows, скрипты Nautilus и т.д.).
    """

    pass


class ConfigError(PContextError):
    """
    Ошибка в конфигурации PContext (неправильные значения, отсутствующие поля).
    """

    pass


class ParamValidationError(PContextError):
    """
    Ошибка валидации параметров, переданных в скрипт.
    """

    pass


class TimeoutExceeded(PContextError):
    """
    Заданный таймаут выполнения был превышен.
    """

    pass


class CancelledError(PContextError):
    """
    Выполнение было отменено пользователем или системой.
    """

    pass


class ScriptRejected(PContextError):
    """
    Скрипт отклонил входные данные (pcontext_accept вернул False).
    """

    pass


def format_exception(exc: BaseException, include_traceback: bool = True) -> str:
    """
    Преобразует исключение в человекочитаемую строку.
    """
    if include_traceback and exc.__traceback__ is not None:
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        return tb.rstrip()
    # Без трейсбека — только тип и сообщение
    return f"{exc.__class__.__name__}: {exc}"


def brief_exception(exc: BaseException) -> str:
    """
    Короткая форма: "Type: message"
    """
    return f"{exc.__class__.__name__}: {exc}"
