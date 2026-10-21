"""
tools/check_hud.py — проверка "видит ли бот обе панели": список целей и панель цели (HP моба).

Зачем: после перестановки интерфейса в игре (сдвинул/уменьшил панель) бот может ослепнуть
молча — не входит в бой, не жмёт комбо (2026-10-05). Этот скрипт за секунду говорит, что
не так, ДО запуска бота. Ничего не нажимает — только один снимок экрана.

Как пользоваться (бот выключен, игра открыта, ВЫБЕРИ любого моба, вкладка "Персонаж"):
    python tools/check_hud.py                 -> снимок экрана через 3 с (успеешь переключиться в игру)
    python tools/check_hud.py скрин.png       -> то же на сохранённом скриншоте (без игры)
Картинка с рамками сохраняется в check_hud.png рядом с проектом.
"""

import os
import sys
import time

import cv2
import numpy as np

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
from src.config import vision_config as VC                                    # noqa: E402
from src.core.vision.panel_detector import locate_panel, read_panel           # noqa: E402
from src.core.vision.target_list import (REF_H, REF_W, format_list, has_native_templates,  # noqa: E402
                                         locate_list, read_target_list, warp_to_reference)


def grab_screen() -> np.ndarray:
    import mss                                     # только для живого режима: на скриншоте не нужен
    factory = getattr(mss, "MSS", None) or mss.mss  # новые mss: MSS (mss.mss устарел и шумит в консоль)
    with factory() as sct:
        return np.array(sct.grab(sct.monitors[1]))[..., :3].copy()


def check(frame: np.ndarray) -> int:
    """Печатает вердикт по обеим панелям. Возвращает число проблем (0 — всё хорошо)."""
    h, w = frame.shape[:2]
    cx, cy = w // 2, h // 2
    problems = 0
    vis = frame.copy()

    # --- Панель цели (HP моба): сначала в зоне из vision_config, нет — ищем по всему экрану
    #     (её можно перетащить, бот найдёт сам так же — VisionManager.PanelLocator) ---
    px, py = cx + VC.TARGET_PANEL_OFFSET_X, cy + VC.TARGET_PANEL_OFFSET_Y
    pw, ph = VC.TARGET_PANEL_WIDTH, VC.TARGET_PANEL_HEIGHT
    pr = read_panel(frame[max(0, py):py + ph, max(0, px):px + pw])
    moved = False
    if not pr.ok:
        found = locate_panel(frame, pw, ph)
        if found is not None and found[0] >= 0 and found[1] >= 0:
            px, py = found
            pr = read_panel(frame[py:py + ph, px:px + pw])
            moved = pr.ok
    if pr.ok:
        print("[OK]   Панель цели видна: HP %.0f%%  (зона x=%d y=%d %dx%d)%s" % (
            pr.hp_percent, px, py, pw, ph, " — перетащена, бот найдёт её сам" if moved else ""))
    else:
        problems += 1
        print("[!!]   Панель цели НЕ видна (%s). Выбран ли моб? Не закрыта ли она другой панелью?"
              " Зона x=%d y=%d %dx%d" % (pr.reason, px, py, pw, ph))
    cv2.rectangle(vis, (px, py), (px + pw, py + ph), (0, 255, 0) if pr.ok else (0, 0, 255), 2)

    # --- Список целей: ищем где угодно и в любом масштабе ---
    loc = locate_list(frame)
    if loc is None:
        problems += 1
        print("[!!]   Список целей НЕ найден: открыт ли он, вкладка 'Персонаж'?")
    else:
        fx, fy, sc, score, precise = loc
        tl = read_target_list(warp_to_reference(frame, fx, fy, sc), sc)
        lx, ly = int(fx), int(fy)
        lw, lh = int(round(REF_W * sc)), int(round(REF_H * sc))
        print("[OK]   Список найден: x=%.1f y=%.1f, размер %.2f%%%s -> %s"
              % (fx, fy, sc * 100, "" if precise else " (грубо: список пуст, уточню по строкам)", format_list(tl)))
        if not has_native_templates(sc):
            problems += 1
            print("[!!]   Для размера списка %.1f%% нет образцов цифр — метры могут читаться неверно."
                  " Пришли скриншоты списка с разными метрами — сниму образцы." % (sc * 100))
        ox = min(px + pw, lx + lw) - max(px, lx)
        oy = min(py + ph, ly + lh) - max(py, ly)
        if ox > 10 and oy > 10:
            problems += 1
            print("[!!]   Список ПЕРЕКРЫВАЕТ панель цели на %d px — сдвинь список ниже или в сторону." % oy)
        cv2.rectangle(vis, (lx, ly), (lx + lw, ly + lh), (255, 0, 255), 2)

    out = os.path.join(ROOT, "check_hud.png")
    cv2.imwrite(out, vis)
    print("\nИтог: %s. Картинка с рамками: %s" % ("всё в порядке" if problems == 0 else "проблем: %d" % problems, out))
    return problems


if __name__ == "__main__":
    if len(sys.argv) > 1:
        img = cv2.imread(sys.argv[1])
        if img is None:
            sys.exit("Не открыл картинку: %s" % sys.argv[1])
    else:
        print("Снимок экрана через 3 с — переключись в игру (выбери моба, вкладка 'Персонаж')...")
        time.sleep(3.0)
        img = grab_screen()
    sys.exit(1 if check(img) else 0)
