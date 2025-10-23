# PContext

<p align="center">
  <img src="https://github.com/waldyr/Sublime-Installer/blob/master/assets/logo-small.png?raw=true" alt="PContext Logo"/>
</p>

---

PContext — библиотека и инструменты для запуска пользовательских Python‑скриптов из контекстного меню Windows 10/11 (классическое меню) и Linux (Nautilus Scripts). Скрипты выполняются в изолированных окружениях (venv), зависимости устанавливаются автоматически, результаты удобно «авто‑открываются» (файлы/ссылки/текст).

- Без зависимостей от PContext внутри ваших скриптов — лишь соглашение по метаданным и именам функций.
- Поддержка одноразовых скриптов (one-shot) и сервисов (долго живущий процесс).
- Трей‑иконка: просмотр активных задач и сервисов, настройка, быстрый запуск background‑скриптов.
- Логи: на запуск формируется текстовый лог (50 последних на скрипт).
- Windows: интеграция через HKCU (без админ‑прав). Linux: Nautilus Scripts.

Статус: MVP.

## Требования

- Python 3.10+
- Windows 10/11 (классическое контекстное меню) или Ubuntu (GNOME/Nautilus).
- Опционально:
  - PySide6 — для трея и улучшенного попап‑меню.
  - watchdog — для live‑перескана скриптов (будущее расширение).
  - psutil — аккуратное завершение процессов, окно задач.
  - PyYAML — метаданные в docstring (YAML).

## Установка

```bash
pip install pcontext[full]
# или минимально:
pip install pcontext
# и по необходимости доберите extras:
pip install PyYAML PySide6 psutil watchdog
```

## Быстрый старт

1. Установите интеграцию с ОС:

   ```bash
   # Windows: создаст пункты в HKCU класcического контекстного меню
   python -m pcontext.cli.main os install-integration

   # Linux (Nautilus): создаст Script ~/.local/share/nautilus/scripts/PContext
   python -m pcontext.cli.main os install-integration
   ```

2. Положите ваши .py скрипты в каталог скриптов:

   - Windows: %USERPROFILE%\Documents\PContext\scripts
   - Linux: ~/PContext/scripts

3. Проверьте список скриптов:

   ```bash
   python -m pcontext.cli.main scripts list
   ```

4. Клик правой кнопкой → пункт «PContext…» → выберите скрипт. Видимый список скриптов зависит от выделенных файлов/папок и фильтров «accepts» скрипта.

5. Запустите трей (иконка в области уведомлений):

   ```bash
   # Запустить
   python -m pcontext.cli.daemonctl start

   # Статус/остановка
   python -m pcontext.cli.daemonctl status
   python -m pcontext.cli.daemonctl stop
   ```

## Метаданные скрипта (ключи)

- id: стабильный идентификатор (если не указан — генерируется).
- name: отображаемое имя.
- type: one-shot | service.
- description: описание.
- group: иерархия групп "Vision/YOLO" (или берется из папок).
- icon: путь к иконке (необязательно).
- accepts:
  - scope: files | directories | background | mixed.
  - mimes: список MIME (например image/\*, application/pdf).
  - extensions: список расширений (.png, .jpg).
  - count: _, >=1, 1, 2..5, 1.._ — ограничение количества выделений.
  - mode: batch (по умолчанию) | per-item.
- depends:
  - pip: список пакетов для pip install в окружение.
  - scripts: список id сервисов, от которых зависит скрипт (поднятие сервисов — поддерживается менеджером).
- params: карта параметров (см. ниже).
- timeout:
  - one_shot_seconds: таймаут выполнения one‑shot.
  - service_idle_seconds: автоостановка сервиса при простое.
  - grace_seconds: мягкое завершение.
- auto_open_result: bool — автооткрытие результатов.
- python_interpreter: альтернативный интерпретатор для venv (опционально).

Параметры (params):

- Типы: bool, int, float, str, enum (options), slider, text, file, folder, list[str], list[int], dict, secret.
- Ограничения: min, max, step, regex, options, placeholder, file_filter, hidden, secret.
- Значения приводятся по типам, валидация выполняется до запуска.

## Контракт функций скрипта

- One‑shot: pcontext_run(inputs, params, ctx) -> result
- Service:
  - pcontext_init(params, ctx) — опционально (разовая инициализация).
  - pcontext_request(inputs, params, ctx) -> result — основной обработчик.
  - pcontext_shutdown(ctx) — опционально (освобождение ресурсов).
- Доп.: pcontext_accept(inputs, ctx) -> bool — опциональная предвалидация входов (feature на будущее).

Аргументы:

- inputs: список объектов

  ```json
  { "type": "file|directory|background", "path": "...", "name": "...", "mime": "...", "size": 123, "created": 0, "modified": 0 }
  ```

- params: значения, приведенные по метаданным.
- ctx: служебный контекст
  ```json
  { "run_id": "...", "os": "windows|linux", "user": "name", "cwd": "...?", "tmp_dir": "...", "cache_dir": "...", "log_file": "...", "cancel_flag_path": "..." }
  ```

## Результат выполнения

Возврат result из pcontext_run/pcontext_request:

- None — ничего не делаем.
- str:
  - http(s):// — открыть в браузере,
  - иначе — скопировать в буфер обмена (и сообщить пользователю).
- dict[str,str] — «типизированный путь»:
  - "image", "video", "audio", "textfile", "pdf", "doc", "ppt", "xls", "archive", "folder", "link", "any".
- list[str|dict] — массив результатов.

Автооткрытие можно отключить глобально или в метаданных скрипта.

## Логи и прогресс

- Логи: %LOCALAPPDATA%/PContext/logs (Windows), ~/.cache/pcontext/logs (Linux).
- Сохраняются 50 последних логов на скрипт, только текст: шапка (время/скрипт/параметры), вывод, итог/длительность.
- Префиксный протокол прогресса/уведомлений без импортов:

```
PCTX:PROGRESS 0.42
PCTX:NOTICE Текст
PCTX:WARN Текст
```

Эти строки пишите в stdout — PContext покажет прогресс/уведомления и добавит в лог.

## Трей

- «Скрипты (фон)»: быстрый запуск скриптов, у которых accepts.scope = background.
- «Сервисы»: окно со списком активных сервисов (старт/стоп/idle TTL).
- «Задачи»: окно текущих запущенных воркеров (one‑shot и сервисы), лог, отмена/kill (psutil).
- «Настройки»: каталоги со скриптами, режим окружений, параметры pip, авто‑открытие.

## Запуск

```bash
python -m pcontext.cli.daemonctl start
```

## Папки и конфиги

- Конфиг:
  - Windows: %APPDATA%/PContext/config.yaml
  - Linux: ~/.config/pcontext/config.yaml
- Директории скриптов по умолчанию:
  - Windows: %USERPROFILE%/Documents/PContext/scripts
  - Linux: ~/PContext/scripts
- Venv и кэш:
  - Windows: %LOCALAPPDATA%/PContext/venvs
  - Linux: ~/.cache/pcontext/venvs

## Ограничения и заметки

- На Windows 11 с включённым «новым» меню наш пункт окажется в «Дополнительные параметры». Вы указали, что используете классическое — для него мы пишем записи напрямую.
- Прокси‑вызов сервисов из one‑shot без импорта библиотеки (автоматическая маршрутизация по depends.scripts) — в разработке. Базовый менеджер сервисов уже доступен (ServiceManager).
- Иконки resources/icons/pcontext.ico и resources/icons/pcontext.png нужно положить в пакет (репозиторий).

## Лицензия

MIT — см. LICENSE.

## Вклад

PR/Issues приветствуются. Идеи/баги — в трекер. Для разработки: Python 3.11+, pip install -e .[full].
