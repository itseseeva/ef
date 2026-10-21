"""
tools/exe_selftest.py — самопроверка собранного EZF.exe (2026-10-11).

Запуск: dist\\EZF\\EZF.exe --selftest   (build_exe.bat делает это сам сразу после сборки)
Из python тоже работает: python ezf_main.py --selftest — проверка проекта до сборки.

Что проверяет — всё, что ломается именно при упаковке в exe (а не логика бота — она в тестах):
  1. модули грузятся: окно (pywebview), зрение (cv2, mss, numpy), бот, чат, EasyOCR/torch;
  2. файлы на месте: фронт (dist/index.html), образцы панели и списка целей, модель движения,
     конфиг скиллов, модели EasyOCR внутри exe;
  3. конфиг скиллов читается, порядок ротации строится;
  4. распознаватель чата поднимается ОТДЕЛЬНЫМ процессом (EZF.exe --ocr-worker) и читает текст;
  5. ключ OpenRouter найден (.env рядом с exe) — сам ключ не печатается.
Ничего не нажимает, окно не открывает. Итог — в консоль и в selftest.txt рядом с exe.
"""
import os
import sys
import time
import traceback

RESULTS = []


def check(name, fn):
    t0 = time.perf_counter()
    try:
        info = fn()
        ok = info is not False
        RESULTS.append((ok, name, "" if info in (None, True) else str(info), time.perf_counter() - t0))
    except Exception as e:
        RESULTS.append((False, name, "%s: %s" % (type(e).__name__, e), time.perf_counter() - t0))
        traceback.print_exc()


def base_dir() -> str:
    """Где лежат данные программы: _internal в exe, папка проекта из python."""
    return getattr(sys, "_MEIPASS", None) or os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def need_files():
    b = base_dir()
    files = ["dist/index.html",
             "src/config/key_labels.npz", "src/config/skill_bar_labels.png", "src/config/list_digits.npz",
             "src/config/world_digits.npz", "src/config/list_tab.png", "src/config/movement_model.json",
             "src/config/skills_config.json", "src/config/chat_icon_normal.png", "src/config/chat_icon_whisper.png"]
    missing = [f for f in files if not os.path.isfile(os.path.join(b, f))]
    if missing:
        raise FileNotFoundError(", ".join(missing))
    return "%d файлов" % len(files)


def easyocr_models():
    from src.core.chat.ocr_engine import easyocr_models_dir
    d = easyocr_models_dir()
    if d is None:
        if getattr(sys, "frozen", False):
            raise FileNotFoundError("в exe нет папки easyocr_models")
        return "не exe — модели из ~/.EasyOCR"
    names = sorted(os.listdir(d))
    for n in ("craft_mlt_25k.pth", "cyrillic_g2.pth"):
        if n not in names:
            raise FileNotFoundError("нет %s (есть: %s)" % (n, ", ".join(names)))
    return ", ".join(names)


def imports_ui():
    import webview          # noqa: F401
    import cv2              # noqa: F401
    import mss              # noqa: F401
    import numpy            # noqa: F401
    return "pywebview, cv2, mss, numpy"


def imports_bot():
    import app_launcher                                   # noqa: F401
    from src.core.bot import FarmBot                       # noqa: F401
    from src.core.vision import bar_layout, ready_capture  # noqa: F401
    from src.core import game_window                       # noqa: F401
    return "app_launcher, bot, vision"


def imports_chat():
    import tools.chat_bot_test as cb                      # noqa: F401
    return "chat_bot_test"


def imports_ocr():
    import torch            # noqa: F401
    import easyocr          # noqa: F401
    return "torch %s, easyocr" % torch.__version__


def skills():
    from src.core.skills_config import load_skills_config
    from src.core.rotation import PriorityRotation
    p = os.path.join(base_dir(), "src", "config", "skills_config.json")
    ch = load_skills_config(p)
    return "%d цепочек: %s" % (len(ch), PriorityRotation.from_chains(ch).describe())


def label_bank():
    from src.core.vision import bar_layout
    n = sum(len(v) for v in bar_layout._bank().values())
    if n == 0:
        raise RuntimeError("образцы табличек не загрузились")
    return "образцов табличек: %d" % n


def ocr_worker():
    import numpy as np
    import cv2
    from src.core.chat.ocr_process import OcrProcess
    t0 = time.perf_counter()
    eng = OcrProcess()
    start = time.perf_counter() - t0
    img = np.full((48, 400), 255, np.uint8)
    cv2.putText(img, "privet 123", (10, 36), cv2.FONT_HERSHEY_SIMPLEX, 1.2, 0, 2)
    t1 = time.perf_counter()
    txt = eng(img)
    eng.close()
    if not txt.strip():
        raise RuntimeError("распознаватель вернул пусто")
    return "старт %.1f с, строка %.2f с: %r" % (start, time.perf_counter() - t1, txt)


def env_key():
    """Ключ модели: llm.env (сервис-посредник) или OPENROUTER_API_KEY (.env). Сам ключ не печатаем."""
    d = os.path.dirname(os.path.abspath(sys.executable)) if getattr(sys, "frozen", False) else base_dir()
    p = os.path.join(d, "llm.env")
    if os.path.isfile(p):
        with open(p, encoding="utf-8-sig") as f:
            cfg = dict(line.strip().split("=", 1) for line in f if "=" in line and not line.strip().startswith("#"))
        if cfg.get("LLM_API_KEY", "").strip():
            return "llm.env: %s, ключ %d символов" % (cfg.get("LLM_BASE_URL", "?").strip(), len(cfg["LLM_API_KEY"].strip()))
    k = os.environ.get("OPENROUTER_API_KEY", "")
    if not k:
        raise RuntimeError("нет ключа: положи llm.env (или .env) рядом с EZF.exe — иначе чат не ответит")
    return "OpenRouter, ключ найден (%d символов)" % len(k)


def main() -> int:
    print("EZF самопроверка (%s)\n" % ("exe" if getattr(sys, "frozen", False) else "python"), flush=True)
    check("Модули окна и зрения", imports_ui)
    check("Модули бота", imports_bot)
    check("Модуль чата", imports_chat)
    check("EasyOCR + torch", imports_ocr)
    check("Файлы программы", need_files)
    check("Модели EasyOCR", easyocr_models)
    check("Конфиг скиллов и ротация", skills)
    check("Образцы табличек панели", label_bank)
    check("Распознаватель чата (отдельный процесс)", ocr_worker)
    check("Ключ модели для чата", env_key)
    lines = []
    for ok, name, info, dt in RESULTS:
        lines.append("%s  %-42s %5.1f с  %s" % ("OK  " if ok else "FAIL", name, dt, info))
    bad = sum(1 for r in RESULTS if not r[0])
    lines.append("")
    lines.append("ИТОГ: всё работает" if not bad else "ИТОГ: проблем %d — пришли этот текст" % bad)
    text = "\n".join(lines)
    print(text, flush=True)
    try:
        out = os.path.join(os.path.dirname(os.path.abspath(sys.executable)) if getattr(sys, "frozen", False)
                           else base_dir(), "selftest.txt")
        with open(out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    except OSError:
        pass
    return 1 if bad else 0


if __name__ == "__main__":
    sys.path.insert(0, base_dir())
    sys.exit(main())
