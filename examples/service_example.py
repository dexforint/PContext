"""
---
name: "YOLO Service"
type: "service"
accepts:
  scope: "background"
depends:
  pip: ["ultralytics>=8", "torch"]
params:
  device: {type: "str", default: "cuda:0"}
timeout:
  service_idle_seconds: 600
---
"""

_model = None


def pcontext_init(params, ctx):
    global _model
    from ultralytics import YOLO

    _model = YOLO("yolov8n.pt")
    _model.to(params.get("device") or "cpu")


def pcontext_request(inputs, params, ctx):
    # inputs: список изображений/папок — зависит от вызывающей стороны
    results = []
    for it in inputs:
        if it["type"] != "file":
            continue
        out_path = Path(ctx["tmp_dir"]) / ("out_" + Path(it["path"]).name)
        r = _model(it["path"])
        # сохранение/визуализация — по желанию
        r[0].save(filename=str(out_path))
        results.append({"image": str(out_path)})
    return results


def pcontext_shutdown(ctx):
    global _model
    _model = None
