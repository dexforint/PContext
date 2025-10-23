Типы:

- "open" - открыть файл в приложении по умолчанию исходя из расширения файла;
- "text" - вывести текст в отдельном окне;
- "link" - открыть ссылку в браузере по умолчанию;
- "copy" - скопировать текст в буфер обмена без уведомления;
- "notify" - уведомить пользователя (библиотека ntfy);
- "folder" - открыть папку;

Пример простого скрипта:

```python
"""
requests>=2.28
"""
import os
from enum import Enum
import requests
from pcontext import oneshot, File, Param

def get_file_size_mb(file_path: str):
    """
    Возвращает размер файла в мегабайтах (МБ).
    """
    try:
        # Получаем размер файла в байтах
        size_in_bytes = os.path.getsize(file_path)

        # Конвертируем байты в мегабайты
        # 1 МБ = 1024 КБ = 1024 * 1024 Байт
        size_in_mb = size_in_bytes / (1024 * 1024)

        return size_in_mb
    except FileNotFoundError:
        print(f"Ошибка: Файл не найден по пути '{file_path}'")
        return None
    except Exception as e:
        print(f"Произошла ошибка: {e}")
        return None

class ServerURL(str, Enum):
    tempsh = "https://temp.sh/upload"

@oneshot(
    # Название в пункте контекстного меню
    title="Upload to temp.sh",
    # Максимальное время выполнения в секундах
    timeout=60,
)
def upload_file(
        # указываем, что для любых файлов мы можем вызвать в контекстом меню данную функцию
        # получаем путь до файла
        file_path: str = File(),
        # далее идёт параметр, чьё значение можно менять в трее
        server_url: str = Param(default="https://temp.sh/upload")
    ):
        file_size = get_file_size_mb(file_path)

        if file_size > (1024 * 4):
            raise RuntimeError(
                "Файл превышает лимит 4 ГБ и не будут загружен!
            )

    return [
        ("copy", link),
        ("notify", "Файл загружен! Ссылка скопирована в буфер обмена!")
    ]

```

Пример сервиса:

```python
"""
ultralytics

"""
from enum import Enum
from ultralytics import YOLO
from pcontext import oneshot, service, Image, Param

class ModelName(str, Enum):
    yolov8n = "yolov8n.pt"

@service(
    title="Запустить Yolo детектор",
    # максимальное время работы сервиса, если превышено - сервис останавливается
    timeout=3600,
    # максимальное время простоя без дела, если превышено - сервис останавливается
    max_downtime=600
)
class YoloDetector:
    def __init__(
        self,
        model_name: ModelName = Param("yolov8n.pt")
    ):
        """
        Инициализирует детектор, загружает модель и перемещает ее на GPU (если возможно).

        Args:
            model_name (str): Имя или путь к файлу весов модели YOLO (например, 'yolov8n.pt', 'yolov8s.pt').
        """
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        print(f"Используется устройство: {self.device.upper()}")

        try:
            self.model = YOLO(model_name)
            # Перемещаем модель на выбранное устройство
            self.model.to(self.device)
            print(f"Модель '{model_name}' успешно загружена на {self.device.upper()}.")
        except Exception as e:
            print(f"Ошибка при загрузке модели: {e}")
            raise


    @oneshot(
        # Название в пункте контекстного меню
        title="Задетектировать объекты",
        # Максимальное время выполнения в секундах
        timeout=10,
    )
    def detect(
        self,
        # даём понять, что на вход ожидаем путь до изображения
        image_path: str = Image,
        conf_threshold: float = Param(0.4)) -> list:
        """
        Обнаруживает объекты на изображении.

        Args:
            image (np.ndarray): Изображение в формате NumPy array (BGR).
            conf_threshold (float): Порог уверенности для детекции.

        Returns:
            list: Список словарей, где каждый словарь представляет обнаруженный объект.
                  Пример: [{'box': [x1, y1, x2, y2], 'confidence': 0.95, 'class_id': 0, 'class_name': 'person'}, ...]
        """
        if not hasattr(self, 'model'):
            print("Ошибка: Модель не была инициализирована.")
            return []

        # verbose=False отключает вывод логов от YOLO во время детекции
        results = self.model.predict(source=image, conf=conf_threshold, verbose=False)

        detections = []
        # results[0] содержит результаты для первого (и единственного) изображения
        for box in results[0].boxes:
            # Координаты рамки
            x1, y1, x2, y2 = map(int, box.xyxy[0])
            # Уверенность
            confidence = float(box.conf[0])
            # ID класса
            class_id = int(box.cls[0])
            # Имя класса
            class_name = self.model.names[class_id]

            detections.append({
                'box': [x1, y1, x2, y2],
                'confidence': confidence,
                'class_id': class_id,
                'class_name': class_name
            })

        return detections

    def __del__(self):
        """
        Освобождает ресурсы при удалении объекта класса (при завершении сервиса).
        """
        print("Удаление объекта YoloDetector и освобождение ресурсов...")
        if hasattr(self, 'model'):
            del self.model
            if self.device == 'cuda':
                # Очень важный шаг для освобождения VRAM
                torch.cuda.empty_cache()
                print("Кэш CUDA очищен.")
        # Принудительно вызываем сборщик мусора
        gc.collect()
        print("Ресурсы освобождены.")



```
