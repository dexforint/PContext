Так же хотелось бы иметь возможность запускать скрипты из Python кода. Например:

```python
import time
from pcontext import run_app, get_service,

# запускает программу, если она не запущена
run_app()

# поиск скрипта (сервиса), если скрипт не нашёлся по id - выдаёт ошибку
service_object = get_service(
    "yolo_service",
    model_name="yolov8n.pt",
)
service_object.get_status() # stopped
# данная команда запускает сервис, но не ожидает его полного запуска
service_object.run()
time.sleep(15)
service_object.get_status() # running

# возвращает список скриптов сервиса
service_scripts = service_object.get_scripts()
detect_script = service_scripts[0]
assert detect_script.id == "yolo_service.detect"

detect_run = detect_script.run()

```
