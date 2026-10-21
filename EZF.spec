# -*- mode: python ; coding: utf-8 -*-
"""
EZF.spec — рецепт сборки EZF.exe (PyInstaller 6, 2026-10-11). Собирать: build_exe.bat.

Режим onedir (папка release\\EZF с EZF.exe и _internal\\), а не один файл: с torch+EasyOCR сборка
~1-2 ГБ — одиночный exe распаковывал бы их во временную папку при КАЖДОМ запуске (минута ожидания)
и чаще пугал бы антивирус. Данные программы лежат в _internal\\ по тем же путям, что в проекте
(src\\config\\..., dist\\...) — код ищет их от своего __file__ и в exe находит без правок.
Без консоли (console=False, 2026-10-11): у пользователя одно окно программы, итог сессии — в интерфейсе.
Всё, что печаталось в терминал, ezf_main.py пишет в _internal\\EZF_log.txt. Распознаватель чата
(EZF.exe --ocr-worker) общается через свои каналы (pipe), консоль ему не нужна.
"""
import glob
import os
import sys

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = os.path.abspath(SPECPATH)
sys.path.insert(0, ROOT)                     # чтобы collect_submodules("src") нашёл пакеты проекта
datas, binaries = [], []
hidden = ["tools.chat_bot_test", "tools.exe_selftest", "app_launcher",
          "dotenv", "requests", "pyperclip", "concurrent.futures"]
hidden += collect_submodules("src")

# Пакеты с данными/DLL, которые обычный анализ импорта не видит целиком.
for pkg in ("webview", "easyocr", "dxcam", "winsdk", "clr_loader", "pythonnet"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hidden += h
    except Exception as e:                   # необязательный пакет не установлен — без него
        print("EZF.spec: пропускаю %s (%s)" % (pkg, e))


# torchvision (его тянет EasyOCR): нативные библиотеки рядом с кодом — _C.pyd (операторы nms и др.),
# image.pyd, zlib/libpng/jpeg *.dll. Сам PyInstaller их пропускает (лог сборки 2026-10-11:
# 'Hidden import "torchvision._C" not found!' — _C.pyd не грузится отдельно от torch), и в exe
# torchvision падает: "operator torchvision::nms does not exist". torchvision ищет их в своей папке —
# туда и кладём.
import importlib.util
_tv = importlib.util.find_spec("torchvision")
if _tv is not None and _tv.origin:
    _tv_dir = os.path.dirname(_tv.origin)
    for f in glob.glob(os.path.join(_tv_dir, "*.pyd")) + glob.glob(os.path.join(_tv_dir, "*.dll")):
        binaries.append((f, "torchvision"))
    print("EZF.spec: torchvision — нативных библиотек: %d" % len(glob.glob(os.path.join(_tv_dir, "*.pyd"))
                                                              + glob.glob(os.path.join(_tv_dir, "*.dll"))))


def add(src_rel, dst_rel):
    datas.append((os.path.join(ROOT, src_rel), dst_rel))


# Фронт (React, собранный npm run build) — папка dist\ проекта. Готовый exe кладём НЕ в dist\
# (это фронт), а в release\ (build_exe.bat): иначе следующая сборка упаковала бы прошлый exe внутрь.
for f in glob.glob(os.path.join(ROOT, "dist", "**", "*"), recursive=True):
    rel = os.path.relpath(f, ROOT)
    if os.path.isfile(f) and not rel.replace("\\", "/").startswith("dist/EZF/"):
        add(rel, os.path.dirname(rel))
# Данные бота: образцы, модели движения, конфиг скиллов, иконки.
for f in glob.glob(os.path.join(ROOT, "src", "config", "*")):
    if os.path.isfile(f) and not f.endswith((".py", ".pyc")):
        add(os.path.relpath(f, ROOT), "src/config")
add("src/config/icons", "src/config/icons")
# Снимки готовых скиллов (src/config/skill_ready) в exe НЕ кладём (2026-10-11): они сняты с ТВОЕГО экрана
# (яркость, гамма, разрешение, масштаб интерфейса). У другого человека готовые скиллы с ними не совпадали
# и считались "на откате" — бот их пропускал. Без снимков бот при первом "Старт" в игре снимает панель
# сам (SkillBarWatcher._auto_capture) — под экран того, кто играет. Переснять: кнопка "Снять скилы".
for f in glob.glob(os.path.join(ROOT, "src", "core", "chat", "*.png")):
    add(os.path.relpath(f, ROOT), "src/core/chat")

# Модели EasyOCR (детектор + русский/английский) — внутрь exe: качать у нового человека не нужно.
MODEL_DIR = os.path.join(os.path.expanduser("~"), ".EasyOCR", "model")
for name in ("craft_mlt_25k.pth", "cyrillic_g2.pth", "english_g2.pth"):
    p = os.path.join(MODEL_DIR, name)
    if os.path.isfile(p):
        datas.append((p, "easyocr_models"))

a = Analysis(
    [os.path.join(ROOT, "ezf_main.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    excludes=["tkinter", "matplotlib", "IPython", "jupyter", "notebook", "pytest",
              "tensorboard", "pytesseract", "pyautogui"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="EZF",
    console=False,          # без чёрного окна терминала; вывод — в _internal\EZF_log.txt (ezf_main.py)
    upx=False,
    disable_windowed_traceback=False,
)
coll = COLLECT(exe, a.binaries, a.datas, upx=False, name="EZF")
