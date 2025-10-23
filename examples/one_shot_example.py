"""
---
name: "Сжать изображения"
type: "one-shot"
description: "Конвертация в .jpg с качеством 90"
accepts:
  scope: "files"
  mimes: ["image/*"]
  count: ">=1"
depends:
  pip: ["Pillow>=10"]
params:
  quality:
    type: int
    default: 90
    min: 1
    max: 100
timeout:
  one_shot_seconds: 120
auto_open_result: true
---
"""

from pathlib import Path
from PIL import Image


def pcontext_run(inputs, params, ctx):
    out_files = []
    out_dir = Path(ctx["tmp_dir"]) / "out"
    out_dir.mkdir(parents=True, exist_ok=True)

    q = params.get("quality", 90)

    for item in inputs:
        if item["type"] != "file":
            continue
        inp = Path(item["path"])
        img = Image.open(inp)
        outp = out_dir / (inp.stem + ".jpg")
        img.convert("RGB").save(outp, "JPEG", quality=int(q))
        out_files.append({"image": str(outp)})

    return out_files  # PContext откроет их системной программой
