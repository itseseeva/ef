"""
Проверка чтения чата на ТВОЁМ ПК (OCR Windows): что бот видит в чате.

    python tools/chat_ocr_check.py                 — снимок экрана через 5 с (успей переключиться в игру)
    python tools/chat_ocr_check.py --delay=10      — то же, через 10 с
    python tools/chat_ocr_check.py --watch         — ЖИВОЙ режим 60 с: печатает новые сообщения, как бот
    python tools/chat_ocr_check.py скрин.png       — полный скриншот 1920x1080 (как присылал)
    python tools/chat_ocr_check.py окно.png --crop — уже вырезанное окно чата

Печатает каждую строку (канал + что распознано) и разобранные сообщения; рядом сохраняет
chat_ocr_check.png — зона чата с рамками строк (зелёная — ЛС, жёлтая — обычный).
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import cv2  # noqa: E402

from src.core.chat.chat_reader import WHISPER, parse, region_rect, segment_lines  # noqa: E402
from src.core.chat.ocr_engine import make_ocr  # noqa: E402


def watch(seconds: int) -> None:
    """
    Живой режим, как будет в боте: раз в 0.4 с снимаем зону чата и печатаем НОВЫЕ сообщения.
    Закрытый чат гасит строки через несколько секунд — разовый снимок мог их не застать
    (2026-10-07), а частый опрос успевает.
    """
    import mss
    import numpy as np
    from src.core.chat.chat_reader import ChatReader
    ocr = make_ocr()
    if ocr is None:
        sys.exit("OCR не поднялся")
    print("OCR:", ocr.name)
    with getattr(mss, "MSS", mss.mss)() as sct:
        mon = sct.monitors[1]
        roi = region_rect(mon["left"], mon["top"], mon["width"], mon["height"])
        reader = ChatReader(ocr)
        print("слежу за чатом %d с — переключись в игру и пусть тебе пишут (Ctrl+C — стоп)" % seconds)
        end = time.time() + seconds
        while time.time() < end:
            frame = np.array(sct.grab(roi))[..., :3]
            t0 = time.perf_counter()
            for m in reader.poll(frame):
                print("  %s [%s] %s: %s" % (time.strftime("%H:%M:%S"), "ЛС" if m.channel == WHISPER else "чат",
                                            m.sender, m.text), flush=True)
            dt = time.perf_counter() - t0
            time.sleep(max(0.05, 0.4 - dt))
    print("готово")


def main() -> None:
    for a in sys.argv[1:]:
        if a.startswith("--watch"):
            watch(int(a.split("=", 1)[1]) if "=" in a else 60)
            return
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if args:
        img = cv2.imread(args[0])
        if img is None:
            sys.exit("не открылся файл %s" % args[0])
    else:
        # Снимок через несколько секунд: успеть переключиться в игру (иначе снимается сам терминал —
        # 2026-10-07 так и вышло: на картинке было окно Claude, а не чат игры).
        delay = 5
        for a in sys.argv[1:]:
            if a.startswith("--delay="):
                delay = int(a.split("=", 1)[1])
        for i in range(delay, 0, -1):
            print("переключись в игру — снимок через %d с..." % i, flush=True)
            time.sleep(1)
        import mss
        import numpy as np
        with getattr(mss, "MSS", mss.mss)() as sct:
            mon = sct.monitors[1]
            img = np.array(sct.grab(mon))[..., :3]
    if not args:
        cv2.imwrite("chat_ocr_full.png", img)        # весь экран — видно, где на самом деле чат
        print("экран %dx%d, весь снимок: chat_ocr_full.png" % (img.shape[1], img.shape[0]))
    if "--crop" not in sys.argv:
        h, w = img.shape[:2]
        r = region_rect(0, 0, w, h)
        img = img[r["top"]:r["top"] + r["height"], r["left"]:r["left"] + r["width"]]
    ocr = make_ocr()
    if ocr is None:
        from src.core.chat.ocr_engine import LAST_ERRORS
        print("OCR не поднялся. Причины:")
        for e in LAST_ERRORS:
            print("  -", e)
        print("Python:", sys.executable)
        sys.exit(1)
    print("OCR:", ocr.name)
    lines = segment_lines(img)
    dbg = img.copy()
    rows = []
    for ln in lines:
        t0 = time.perf_counter()
        txt = ocr(ln.image)
        dt = (time.perf_counter() - t0) * 1000
        rows.append((ln.channel, txt, ln.y0, ln.y1, ln.header))
        print("  %-7s y=%3d-%3d  %5.1f мс  | hdr=%s | %s" % (
            ln.channel, ln.y0, ln.y1, dt, "Y" if ln.header else "N", txt.encode("utf-8", "replace").decode("utf-8")
        ))
        cv2.rectangle(dbg, (0, ln.y0), (dbg.shape[1] - 1, ln.y1), (0, 255, 0) if ln.channel == WHISPER else (0, 220, 255), 1)
    print("\nСообщения:")
    for m in parse(rows):
        print("  [%s] %s: %s  (%s)" % (
            "ЛС" if m.channel == WHISPER else "чат", 
            m.sender, 
            m.text.encode("utf-8", "replace").decode("utf-8"), 
            m.time
        ))
    cv2.imwrite("chat_ocr_check.png", dbg)
    print("\nкартинка с рамками строк: chat_ocr_check.png")


if __name__ == "__main__":
    main()
