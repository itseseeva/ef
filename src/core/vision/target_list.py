"""
src/core/vision/target_list.py

Список целей "Астрального зрения" — панель слева, вкладка "Персонаж" (2026-10-04,
идея пользователя). Игра сама показывает до 8 мобов рядом: сетка 2 столбца x 4
строки, в каждой строке номер (он же номер над мобом в мире), имя, расстояние
"NN м" и полоска; выбранная цель — строка в фиолетовой рамке. Клик ЛКМ по строке
(с зажатым Alt, чтобы появился курсор) выбирает моба, и персонаж сам бежит к нему.

Этот модуль только ЧИТАЕТ список из кадра зоны (никакого ввода и захвата экрана):
чистые функции -> легко проверять на скриншотах (без игры, на сохранённых кадрах).

Что читаем в каждой ячейке (замеры по двум скриншотам игрока 1920x1080):
  - занята ли: светлая полоска под именем (у пустой ячейки её нет);
  - выбрана ли: лавандовая рамка по краю ячейки (30% пикселей рамки против 0%);
  - агрессивный ли моб: номер/имя красные (G/R ~0.40), у обычных жёлтые (~0.77);
  - расстояние: белые цифры перед "м" — распознаём сравнением с образцами цифр
    (src/config/list_digits.npz), как текст HP на панели, без нейросетей.

Почему образцы, а не OCR-библиотека: шрифт и размер всегда одни и те же, кадр
пиксель-в-пиксель (mss), поэтому сравнение с образцом (нормированная корреляция,
cv2.matchTemplate) точнее и в сотни раз быстрее Tesseract и не требует установки.

Цифр 0 и 5 на скриншотах не было: их образцов пока нет. Такая цифра узнаётся по
форме (у "0" есть замкнутая дырка, у "5" нет), а сам кадр цифры сохраняется в
list_digit_samples/ — по нему добавим настоящий образец.

Как проверить: python src/core/vision/target_list.py при открытой игре — окно с
разбором строк (рамки: зелёная — моб, красная — агрессивный, фиолетовая — выбран,
жёлтая точка — куда кликнет бот) и строка в консоли раз в 0.2 с.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import cv2
import numpy as np

# --- Геометрия (px на Full HD; зона TARGET_LIST_* из vision_config) ---
# Ячейки внутри зоны: левый верхний угол столбцов и строк, размер ячейки.
# Строки идут с шагом ~35 px; номер строки в игре = 1..4 в левом столбце, 5..8 в правом.
COL_X = (7, 208)
ROW_Y = (37, 73, 108, 143)
CELL_W, CELL_H = 198, 33

# Вкладка "Персонаж" (активная — фиолетовая, неактивные — серые ~28,28,28).
TAB_RECT = (9, 6, 84, 22)                 # x, y, w, h внутри зоны
# Полоска под именем: строки 25..31 ячейки, столбцы 38..196.
UNDERLINE_Y = (25, 32)
UNDERLINE_X = (38, 196)
# Зона расстояния "NN м" внутри ячейки (по x и y).
DIST_X = (140, 196)
DIST_Y = (7, 25)          # нижняя граница выше полоски (строки 27-30): она светлая и "склеила" бы цифры
# Номер строки (кружок слева) — по его цвету узнаём агрессивного моба.
BADGE_RECT = (8, 6, 22, 22)
NAME_X = (38, 136)        # имя моба: от номера до метров (у длинных имён игра обрезает текст сама)
NAME_Y = (7, 25)

# Пороги (замеры по скриншотам: см. докстринг модуля).
TAB_MIN_PURPLE = 12                       # B-G и R-G у активной вкладки ~25
UNDERLINE_MIN_FRAC = 0.6                  # у занятой ячейки 0.99-1.0, у пустой 0
SELECTED_MIN_FRAC = 0.15                  # доля лавандовых пикселей рамки: 0.30 / 0.00
HOSTILE_MAX_G_OVER_R = 0.6                # красный номер ~0.40, жёлтый ~0.77
TEXT_MIN_MAX = 90                         # ярче этого в зоне цифр — текст есть
DIGIT_MIN_SCORE = 0.70                    # уверенное совпадение с образцом
# Серое имя = моб попал в скан, но уже ушёл далеко (игра "гасит" строку). Цветных (жёлтых/
# красных) пикселей имени у живой строки 207-282, у серой 0; серых у серой ~260. Пороги с
# большим запасом в обе стороны: серость решают оба условия сразу, а не одно.
FADED_MAX_INK = 40                        # меньше стольких цветных пикселей в имени...
FADED_MIN_GREY = 60                       # ...и хотя бы столько серого текста -> строка серая
GLYPH_WIN_H, GLYPH_WIN_W = 12, 9          # окно образца цифры (с фоном вокруг)

_TEMPLATES_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "config", "list_digits.npz")


@dataclass(frozen=True)
class ListRow:
    index: int                                 # 1..8 — номер в игре (и над мобом)
    cell: "tuple[int, int, int, int]"          # x, y, w, h в координатах зоны
    distance_m: "int | None"                   # None — не прочитали
    selected: bool                             # фиолетовая рамка = выбранная цель
    hostile: bool                              # красный номер = агрессивный моб
    distance_conf: float = 0.0                 # худшее совпадение среди цифр (0..1)
    faded: bool = False                        # серое имя: моб ушёл далеко — целью не берём

    @property
    def click_point(self) -> "tuple[float, float]":
        """Середина имени в координатах зоны — туда кликаем (бот добавит разброс)."""
        x, y, w, h = self.cell
        return x + w * 0.45, y + h * 0.45


@dataclass(frozen=True)
class TargetList:
    visible: bool
    rows: "tuple[ListRow, ...]" = ()
    reason: str = ""
    # Кадры цифр, которые не узнали уверенно: (догадка, окно 12x9) — для сборщика образцов.
    unsure_glyphs: "tuple[tuple[str, np.ndarray], ...]" = field(default=(), compare=False)

    @property
    def selected_row(self) -> "ListRow | None":
        # next(генератор, None) — первый подходящий без построения списка.
        return next((r for r in self.rows if r.selected), None)

    def nearest(self, exclude: "set[int] | frozenset[int]" = frozenset()) -> "ListRow | None":
        """
        Ближний моб, кроме уже выбранного, серых (ушли далеко) и номеров из exclude. Строки
        без прочитанного расстояния — в самом конце (лучше хоть какая-то цель, чем никакой).
        """
        cands = [r for r in self.rows if not r.selected and not r.faded and r.index not in exclude]
        if not cands:
            return None
        return min(cands, key=lambda r: (r.distance_m is None, r.distance_m or 0, r.index))


# ---------------------------------------------------------------------------
# Образцы цифр
# ---------------------------------------------------------------------------

def _load_templates(path: str = _TEMPLATES_PATH) -> "dict[str, list[np.ndarray]]":
    """{'3': [окно 12x9 float32, ...], ...}. Нет файла — пустой словарь (всё по форме)."""
    if not os.path.exists(path):
        return {}
    data = np.load(path)
    out: "dict[str, list[np.ndarray]]" = {}
    for key in data.files:                     # ключи вида "d3_0", "d3_1", ...
        out.setdefault(key[1], []).append(data[key].astype(np.float32))
    return out


_TEMPLATES = _load_templates()


def _segment(zone: np.ndarray) -> "list[tuple[int, int]]":
    """
    Столбцы-глифы в зоне "NN м": группы подряд идущих столбцов ярче 0.45*max.
    Порог от максимума, а не число: у некоторых строк текст тусклее (171 против 255).
    Слипшиеся цифры (у выбранной строки белое свечение) режем по самому тёмному
    столбцу в середине — цифра здесь не шире 9 px.
    """
    t = 0.45 * float(zone.max())
    proj = np.clip(zone - t, 0, None).sum(axis=0)
    groups: "list[tuple[int, int]]" = []
    start = None
    for i, on in enumerate(proj > 0):
        if on and start is None:
            start = i
        elif not on and start is not None:
            groups.append((start, i))
            start = None
    if start is not None:
        groups.append((start, len(proj)))
    out: "list[tuple[int, int]]" = []
    for a, b in groups:
        while b - a > 10:
            mid = a + 3 + int(np.argmin(proj[a + 3:b - 3]))
            out.append((a, mid))
            a = mid
        out.append((a, b))
    return out


def glyph_window(zone: np.ndarray, a: int, b: int) -> np.ndarray:
    """Окно 12x9 вокруг глифа (с фоном): по нему и сравниваем, и сохраняем образцы."""
    cx = (a + b) // 2
    x0 = max(0, cx - GLYPH_WIN_W // 2)
    x0 = min(x0, zone.shape[1] - GLYPH_WIN_W)
    return zone[5:5 + GLYPH_WIN_H, x0:x0 + GLYPH_WIN_W].astype(np.float32)


def _has_hole(win: np.ndarray) -> bool:
    """Есть ли у глифа замкнутая дырка (как у 0). Заливка фона от краёв окна."""
    t = 0.45 * float(win.max())
    ink = (win > t).astype(np.uint8)
    bg = (1 - ink).astype(np.uint8)
    h, w = bg.shape
    mask = np.zeros((h + 2, w + 2), np.uint8)
    filled = bg.copy()
    for x in range(w):                         # заливаем фон, связанный с краем окна
        for y in (0, h - 1):
            if filled[y, x] == 1:
                cv2.floodFill(filled, mask, (x, y), 2)
    for y in range(h):
        for x in (0, w - 1):
            if filled[y, x] == 1:
                cv2.floodFill(filled, mask, (x, y), 2)
    return bool((filled == 1).sum() >= 2)      # остался фон, не связанный с краем = дырка


def classify_digit(zone: np.ndarray, a: int, b: int) -> "tuple[str, float]":
    """
    (цифра, уверенность). Образец сдвигаем по окну глифа на +-2 px (cv2.matchTemplate,
    TM_CCOEFF_NORMED — корреляция без учёта яркости и фона), берём лучшую цифру.
    Нет уверенного совпадения -> это одна из цифр без образца (0 или 5): по дырке.
    """
    cx = (a + b) // 2                          # окно шире образца: и узкая "1" влезает
    x0, x1 = max(0, cx - 7), min(zone.shape[1], cx + 8)
    region = zone[2:18, x0:x1].astype(np.float32)        # +-2 строки запаса по высоте
    best_d, best_s = "?", -1.0
    for d, tmpls in _TEMPLATES.items():
        for t in tmpls:
            if region.shape[0] < t.shape[0] or region.shape[1] < t.shape[1]:
                continue
            s = float(cv2.matchTemplate(region, t, cv2.TM_CCOEFF_NORMED).max())
            if s > best_s:
                best_d, best_s = d, s
    if best_s >= DIGIT_MIN_SCORE:
        return best_d, best_s
    win = glyph_window(zone, a, b)
    known = set(_TEMPLATES)
    guess = "0" if _has_hole(win) else "5"
    if guess in known:                         # образец есть, но совпадение слабое — честно "не знаю"
        return best_d, best_s
    return guess, 0.5


def read_distance(cell_img: np.ndarray) -> "tuple[int | None, float, list[tuple[str, np.ndarray]]]":
    """
    (метры, уверенность, сомнительные глифы). Текст выровнен вправо: последний глиф —
    "м", перед ним 1-2 цифры (фильтр игры — до 40 м; поддерживаем и 3 цифры).
    """
    zone = cell_img[DIST_Y[0]:DIST_Y[1], DIST_X[0]:DIST_X[1]].min(axis=2).astype(np.float32)
    if float(zone.max()) < TEXT_MIN_MAX:
        return None, 0.0, []
    glyphs = _segment(zone)
    if len(glyphs) < 2:
        return None, 0.0, []
    m_a, m_b = glyphs[-1]
    if not (6 <= m_b - m_a <= 11) or m_a < 38:     # "м" всегда у правого края, ширина 8-9
        return None, 0.0, []
    digits = [g for g in glyphs[:-1] if g[1] <= m_a - 2][-3:]
    if not digits:
        return None, 0.0, []
    text, conf, unsure = "", 1.0, []
    for a, b in digits:
        d, s = classify_digit(zone, a, b)
        if d == "?":
            return None, 0.0, [("?", glyph_window(zone, a, b))]
        text += d
        conf = min(conf, s)
        if s < 0.9:
            unsure.append((d, glyph_window(zone, a, b)))
    return int(text), conf, unsure


# ---------------------------------------------------------------------------
# Разбор всей зоны
# ---------------------------------------------------------------------------

def _tab_active(frame: np.ndarray) -> bool:
    x, y, w, h = TAB_RECT
    b, g, r = frame[y:y + h, x:x + w].reshape(-1, 3).mean(axis=0)
    return (b - g) >= TAB_MIN_PURPLE and (r - g) >= TAB_MIN_PURPLE


def _underline_frac(cell: np.ndarray) -> float:
    band = cell[UNDERLINE_Y[0]:UNDERLINE_Y[1], UNDERLINE_X[0]:UNDERLINE_X[1]].astype(np.int16)
    lo, hi = band.min(axis=2), band.max(axis=2)
    bright_grey = (lo > 85) & ((hi - lo) < 50)           # светлая и серая (не жёлтая, не фиолетовая)
    return float(bright_grey.any(axis=0).mean())


def _selected(cell: np.ndarray) -> bool:
    c = cell.astype(np.int16)
    ring = np.concatenate([c[0:2].reshape(-1, 3), c[-2:].reshape(-1, 3),
                           c[:, 0:2].reshape(-1, 3), c[:, -2:].reshape(-1, 3)])
    lavender = (ring[:, 0] > 170) & (ring[:, 0] - ring[:, 1] > 30)
    return float(lavender.mean()) >= SELECTED_MIN_FRAC


def _badge_lit(cell: np.ndarray) -> bool:
    x, y, w, h = BADGE_RECT
    return bool((cell[y:y + h, x:x + w, 2] > 150).sum() >= 8)   # жёлтый/красный номер строки


def _hostile(cell: np.ndarray) -> bool:
    x, y, w, h = BADGE_RECT
    badge = cell[y:y + h, x:x + w].astype(np.float32)
    lit = badge[..., 2] > 150                             # ярко-красный канал: сам номер
    if not lit.any():
        return False
    return float((badge[..., 1][lit] / badge[..., 2][lit]).mean()) < HOSTILE_MAX_G_OVER_R


def _faded(cell: np.ndarray) -> bool:
    """
    Имя строки серое? Канал "красный минус синий": у жёлтого (R~195, B~47) и красного
    (R~208, B~30) имени он большой, у серого текста R~B. Фиолетовая подсветка выбранной
    строки не мешает: у неё синий ВЫШЕ красного, в "цветные" она не попадает, а в
    "серые" — тоже нет (насыщенная). Два среза numpy на окне 98x18 — микросекунды.
    """
    reg = cell[NAME_Y[0]:NAME_Y[1], NAME_X[0]:NAME_X[1]].astype(np.int16)
    b, r = reg[..., 0], reg[..., 2]
    ink = int(((r - b > 60) & (r > 120)).sum())
    if ink >= FADED_MAX_INK:
        return False                                      # early return: цветная — частый случай
    hi, lo = reg.max(axis=2), reg.min(axis=2)
    grey = int(((hi - lo < 35) & (hi > 80)).sum())        # светлый и бесцветный пиксель — серая буква
    return grey >= FADED_MIN_GREY


def read_target_list(frame: "np.ndarray | None") -> TargetList:
    """
    Кадр зоны TARGET_LIST_* (BGR) -> TargetList. ~1 мс: 8 ячеек, в каждой пара
    срезов numpy и до трёх matchTemplate на крошечных окнах — безопасно для 60 FPS.
    """
    if frame is None or frame.size == 0:
        return TargetList(False, reason="нет кадра")
    if frame.shape[0] < ROW_Y[-1] + CELL_H or frame.shape[1] < COL_X[-1] + CELL_W:
        return TargetList(False, reason="зона меньше сетки списка")
    if not _tab_active(frame):
        return TargetList(False, reason="список закрыт или не вкладка 'Персонаж'")
    rows: "list[ListRow]" = []
    unsure: "list[tuple[str, np.ndarray]]" = []
    for ci, cx in enumerate(COL_X):
        for ri, ry in enumerate(ROW_Y):
            cell = frame[ry:ry + CELL_H, cx:cx + CELL_W]
            if _underline_frac(cell) < UNDERLINE_MIN_FRAC:
                continue
            dist, conf, uns = read_distance(cell)
            # Вторая проверка "строка, а не мир сквозь полупрозрачную панель": есть "NN м"
            # или горит номер слева. Одна светлая полоса ещё не строка.
            if dist is None and not _badge_lit(cell):
                continue
            unsure.extend(uns)
            rows.append(ListRow(
                index=ci * len(ROW_Y) + ri + 1,
                cell=(cx, ry, CELL_W, CELL_H),
                distance_m=dist,
                selected=_selected(cell),
                hostile=_hostile(cell),
                distance_conf=conf,
                faded=_faded(cell),
            ))
    return TargetList(True, tuple(rows), "ok" if rows else "пусто", tuple(unsure))


def format_list(tl: TargetList) -> str:
    """Строка для лога: '1:12м 2:4м* 6:37м! 3:30м~' (* выбран, ! агрессивный, ~ серый, ? не прочли)."""
    if not tl.visible:
        return "нет списка (%s)" % tl.reason
    if not tl.rows:
        return "пусто"
    return " ".join(
        "%d:%s%s%s%s" % (r.index, "?" if r.distance_m is None else "%dм" % r.distance_m,
                         "*" if r.selected else "", "!" if r.hostile else "", "~" if r.faded else "")
        for r in tl.rows
    )


if __name__ == "__main__":
    # Песочница на живой игре: окно с разбором списка, раз в 0.2 с. Esc — выход.
    import sys
    import time
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))
    import mss
    from src.config import vision_config as VC

    with mss.mss() as sct:
        mon = sct.monitors[1]
        cx, cy = mon["left"] + mon["width"] // 2, mon["top"] + mon["height"] // 2
        roi = {"left": cx + VC.TARGET_LIST_OFFSET_X, "top": cy + VC.TARGET_LIST_OFFSET_Y,
               "width": VC.TARGET_LIST_WIDTH, "height": VC.TARGET_LIST_HEIGHT}
        while True:
            img = np.array(sct.grab(roi))[..., :3].copy()
            t0 = time.perf_counter()
            tl = read_target_list(img)
            ms = (time.perf_counter() - t0) * 1000
            for r in tl.rows:
                x, y, w, h = r.cell
                color = (255, 0, 255) if r.selected else ((0, 0, 255) if r.hostile else (0, 255, 0))
                cv2.rectangle(img, (x, y), (x + w, y + h), color, 1)
            near = tl.nearest()
            if near is not None:
                px, py = near.click_point
                cv2.circle(img, (int(px), int(py)), 4, (0, 255, 255), -1)
            print("%-60s ближний: %s  (%.1f мс)" % (format_list(tl), near.index if near else "-", ms))
            cv2.imshow("TargetList", cv2.resize(img, None, fx=2, fy=2, interpolation=cv2.INTER_NEAREST))
            if cv2.waitKey(200) == 27:
                break
