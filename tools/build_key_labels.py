"""
tools/build_key_labels.py — образцы табличек клавиш (src/config/key_labels.npz) для bar_layout.

Зачем: клавишу под слотом бот узнаёт, сравнивая табличку с образцами. Образцы вырезаются из
скриншотов панели, где надписи известны. Шрифт и рамка табличек в игре одни на всех — поэтому
образцы с двух аккаунтов узнают таблички и у третьего (масштаб приводится к одному).

Запуск (разработчику, не пользователю): python tools/build_key_labels.py скрин.png "1 2 3 4 5 6 _ 7 8 9 0 - =" "Q E C Num5" [ещё пары]
"_" — слот без таблички/не клавиша; LMB/RMB/MMB — значок кнопки мыши под слотом. Порядок — как на панели слева направо (нижний ряд, верхний).
"""
import os
import sys

import cv2
import numpy as np

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
from src.core.vision import bar_layout as BL                                 # noqa: E402


def boxes(img):
    """(шаг, нижний ряд, верхний ряд) — таблички слева направо (None — нет таблички). Та же логика, что у бота."""
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)
    h = g.shape[0]
    P, seps, top = BL._find_row(g, int(h * 0.70), h, *BL.PITCH_RANGE, min_cells=8)
    sc = BL._BoxScorer(g)
    return P, BL.bottom_boxes(sc, seps, top, P), BL.top_boxes(sc, seps, top, P)[0]


def collect(path, rows):
    img = cv2.imread(path)
    P, bottom, top = boxes(img)
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)
    h = g.shape[0]
    _, seps, top_y = BL._find_row(g, int(h * 0.70), h, *BL.PITCH_RANGE, min_cells=8)
    sc = BL._BoxScorer(g)
    yb = BL.row_label_y(sc, [a + P / 2 for a in seps[:-1]], int(top_y + 0.75 * P), int(top_y + 1.25 * P), P)
    out = {}
    for names, bxs in zip(rows, (bottom, top)):
        names = names.split()
        if len(names) != len(bxs) and len(names) > len(bxs):
            raise SystemExit("%s: подписей %d, табличек %d" % (path, len(names), len(bxs)))
        for i, (n, b) in enumerate(zip(names, bxs)):
            if n.upper() in BL.MOUSE_NAMES and bxs is bottom:
                # значок мыши — образец "MOUSE_LMB" с места под слотом
                reg = BL.mouse_region(g, seps[i] + P / 2, yb, P).astype(np.uint8)
                out.setdefault("MOUSE_" + n.upper(), []).append((reg, float(P)))
                continue
            if n == "_" or b is None:
                continue
            x, y, w, h = b
            # как в игре, без пересчёта масштаба + шаг панели (масштаб приводится при сравнении)
            out.setdefault(n, []).append((cv2.cvtColor(img[y:y + h, x:x + w], cv2.COLOR_BGR2GRAY), float(P)))
    return out


def main():
    args = sys.argv[1:]
    bank = {}
    while args:
        path, bottom, top = args[0], args[1], args[2]
        args = args[3:]
        for k, v in collect(path, (bottom, top)).items():
            bank.setdefault(k, []).extend(v)
    BL.save_label_bank(bank)
    print("образцов: %d (%s)" % (sum(len(v) for v in bank.values()), " ".join(sorted(bank))))


if __name__ == "__main__":
    main()
