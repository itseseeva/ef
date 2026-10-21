"""
tools/build_check.py — проверка ПЕРЕД сборкой exe (2026-10-11). Запускает build_exe.bat сам.

Зачем: сборка идёт 5-15 минут, и падать в её конце из-за того, что нет пакета или моделей, обидно.
Здесь за секунды:
  - все пакеты, которые попадут в exe, ставятся/импортируются в .venv;
  - модели EasyOCR есть на диске (~/.EasyOCR/model) — их вшиваем в exe, чтобы у нового человека
    чат работал без интернета до серверов моделей. Нет — скачиваем один раз сейчас;
  - фронт собран (dist/index.html), .env на месте (без него чат не ответит).
"""
import importlib
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

PACKAGES = [("webview", "pywebview"), ("cv2", "opencv-python"), ("mss", "mss"), ("numpy", "numpy"),
            ("easyocr", "easyocr"), ("torch", "torch"), ("dotenv", "python-dotenv"),
            ("requests", "requests"), ("pyperclip", "pyperclip")]
OPTIONAL = [("dxcam", "dxcam — быстрый захват экрана"), ("winsdk", "winsdk — Windows OCR, запасной")]
MODELS = ("craft_mlt_25k.pth", "cyrillic_g2.pth")
MODEL_DIR = os.path.join(os.path.expanduser("~"), ".EasyOCR", "model")


def main() -> int:
    bad = 0
    for mod, pip_name in PACKAGES:
        try:
            importlib.import_module(mod)
            print("  OK   %s" % mod)
        except Exception as e:
            bad += 1
            print("  НЕТ  %s — поставь: pip install %s  (%s)" % (mod, pip_name, e))
    for mod, what in OPTIONAL:
        try:
            importlib.import_module(mod)
            print("  OK   %s" % mod)
        except Exception:
            print("  --   %s нет (необязательно: %s)" % (mod, what))

    missing = [m for m in MODELS if not os.path.isfile(os.path.join(MODEL_DIR, m))]
    if missing:
        print("  ...  моделей EasyOCR нет (%s) — скачиваю один раз (~100 МБ)..." % ", ".join(missing), flush=True)
        try:
            import easyocr
            easyocr.Reader(["ru", "en"], gpu=False, verbose=False)
        except Exception as e:
            print("  НЕТ  модели не скачались: %s" % e)
        missing = [m for m in MODELS if not os.path.isfile(os.path.join(MODEL_DIR, m))]
    if missing:
        bad += 1
        print("  НЕТ  модели EasyOCR: %s (папка %s)" % (", ".join(missing), MODEL_DIR))
    else:
        print("  OK   модели EasyOCR (%s)" % MODEL_DIR)

    for rel, why in (("dist/index.html", "собери фронт (npm run build) и положи dist/ в папку проекта"),
                     ("src/config/skills_config.json", "настрой цепочки в программе и сохрани"),
                     ("src/config/key_labels.npz", "файл из обновления 2026-10-11")):
        if os.path.isfile(os.path.join(ROOT, rel)):
            print("  OK   %s" % rel)
        else:
            bad += 1
            print("  НЕТ  %s — %s" % (rel, why))
    if os.path.isfile(os.path.join(ROOT, ".env")):
        print("  OK   .env (ключ будет скопирован рядом с exe)")
    else:
        print("  !!   нет .env — exe соберётся, но чат-бот не ответит, пока не положишь .env рядом с EZF.exe")

    print("\nПроверка перед сборкой: %s" % ("всё на месте" if not bad else "проблем %d — сборку не начинаю" % bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
