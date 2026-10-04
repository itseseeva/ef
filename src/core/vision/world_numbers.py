"""
src/core/vision/world_numbers.py

Номер моба НАД его табличкой в мире (2026-10-04). Тот же номер стоит у его строки в
списке целей "Астрального зрения" (target_list.py), поэтому по номеру бот точно
знает, какой моб на экране — тот, кого он выбрал в списке. Надёжнее красных стрелок
выбора: у агрессивных (красных) мобов стрелки распознаются плохо.

Геометрия (замер по 6 номерам на двух скриншотах 1920x1080, разброс +-1 px): центр
номера = (левый край заливки полоски HP + 43.5, верх полоски - 36.5). Номер —
жёлтая (у агрессивного моба красная) цифра ~11x17 px в тёмном кружке, размер не
зависит от расстояния (элемент интерфейса, как и сама полоска).

Распознавание — сравнение с образцами цифр (src/config/world_digits.npz) по каналу
"красный минус синий": и жёлтая, и красная цифра в нём яркие, тёмный кружок и
трава — тёмные. Образцы пока есть для 1, 2, 3, 5 (их было видно на скриншотах);
кадры остальных номеров бот сохраняет в world_number_samples/ — добавим образцы.

Как проверить без игры: read_number(кадр_мира, полоска) на скриншоте с номерами.
"""

from __future__ import annotations

import os

import cv2
import numpy as np

NUM_DX_FROM_LEFT = 43.5        # центр номера правее левого края заливки
NUM_DY_ABOVE_TOP = 36.5        # и выше верха полоски
WIN_H, WIN_W = 20, 14          # окно образца цифры
SEARCH_PX = 3                  # запас поиска вокруг ожидаемого центра (+-px)
MIN_SCORE = 0.88               # настоящие номера совпадают на 0.98-1.0 (даже под мечом выбора
                               # и под чужим именем); высокий порог — чтобы цифра без образца
                               # (4, 6, 7, 8) не сошла за похожую (8 за 3, 6 за 5)
MIN_MARGIN = 0.06              # отрыв от второй по сходству цифры
MIN_BAR_W = 60                 # номер ищем только над настоящими полосками (не буквами имени)

_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "config", "world_digits.npz")


def ink(frame: np.ndarray) -> np.ndarray:
    """Канал 'красный минус синий' (float32): яркий у жёлтых и красных цифр."""
    f = frame.astype(np.int16)
    return np.clip(f[..., 2] - f[..., 0], 0, 255).astype(np.float32)


def _load(path: str = _PATH) -> "dict[str, list[np.ndarray]]":
    if not os.path.exists(path):
        return {}
    data = np.load(path)
    out: "dict[str, list[np.ndarray]]" = {}
    for key in data.files:                       # "d2_0", "d5_0", ...
        out.setdefault(key[1], []).append(data[key].astype(np.float32))
    return out


_TEMPLATES = _load()


def number_center(bar: "tuple[int, int, int, int]") -> "tuple[float, float]":
    x, y, _w, _h = bar
    return x + NUM_DX_FROM_LEFT, y - NUM_DY_ABOVE_TOP


def number_window(frame: np.ndarray, bar: "tuple[int, int, int, int]", pad: int = 0) -> "np.ndarray | None":
    """Окно 'ink' вокруг ожидаемого номера (+pad со всех сторон); None — вне кадра."""
    cx, cy = number_center(bar)
    x0 = int(round(cx - WIN_W / 2)) - pad
    y0 = int(round(cy - WIN_H / 2)) - pad
    x1, y1 = x0 + WIN_W + 2 * pad, y0 + WIN_H + 2 * pad
    if x0 < 0 or y0 < 0 or x1 > frame.shape[1] or y1 > frame.shape[0]:
        return None
    return ink(frame[y0:y1, x0:x1])


def read_number(frame: np.ndarray, bar: "tuple[int, int, int, int]") -> "tuple[int | None, float]":
    """
    (номер, уверенность) над полоской bar (x, y, w, h в координатах frame).
    None — полоска не та (узкая), номер вне кадра, не похож ни на один образец или
    похож на два сразу. Дёшево: окно 26x20 и до 4-6 matchTemplate — доли мс.
    """
    if bar[2] < MIN_BAR_W or not _TEMPLATES:
        return None, 0.0
    region = number_window(frame, bar, pad=SEARCH_PX)
    if region is None or float(region.max()) < 60.0:   # ничего яркого — номера нет
        return None, 0.0
    scores: "dict[str, float]" = {}
    for d, tmpls in _TEMPLATES.items():
        scores[d] = max(float(cv2.matchTemplate(region, t, cv2.TM_CCOEFF_NORMED).max()) for t in tmpls)
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    best_d, best_s = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else -1.0
    if best_s < MIN_SCORE or best_s - second < MIN_MARGIN:
        return None, best_s
    return int(best_d), best_s


def supported(number: int) -> bool:
    """Есть ли образец этой цифры (иначе искать её номером бесполезно — только по стрелкам)."""
    return str(number) in _TEMPLATES
