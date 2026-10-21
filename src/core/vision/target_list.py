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
# Полоска HP под именем (скрин 2026-10-06): светло-серая заливка (127,130,130) = оставшееся HP,
# тёмно-серая дорожка (56,56,56) = потерянное. Строку узнаём по полоске ЦЕЛИКОМ (заливка + дорожка):
# раньше считали только светлую часть, и раненый моб (HP < 60%) пропадал из списка — бот думал,
# что его нет, и не добивал. HP строки = доля заливки.
TRACK_LO, TRACK_HI, TRACK_SPREAD = 38, 80, 10
WOUNDED_HP_MAX = 0.6                       # строка с HP не выше этого — раненый, добиваем первым
# "Дерётся с нами" (2026-10-11, blackbox 060426_017): после убийства в списке стоял моб на 0 м с 82% HP —
# он бил нас, а бот ушёл на моба в 11 м. Моб вплотную, у которого HP уже не полное, — первая цель.
# Нижний порог отсекает труп/пустую полоску (0%), верхний — шум полоски у целого моба.
FIGHTING_MAX_M = 5
FIGHTING_HP = (0.03, 0.97)
WOUNDED_MAX_M = 20   # было 35 (blackbox 2026-10-09 22:01): "раненые" в 25-35 м оказывались игроком или
                     # чужим мобом — бот бежал через поле и бросал своего. Наш недобитый — рядом.
UNDERLINE_MIN_FRAC = 0.6                  # у занятой ячейки 0.99-1.0, у пустой 0
SELECTED_MIN_FRAC = 0.15                  # доля лавандовых пикселей рамки: 0.30 / 0.00
HOSTILE_MAX_G_OVER_R = 0.6                # красный номер ~0.40, жёлтый ~0.77
# Моб БЬЁТ НАС (2026-10-13, скрин игрока): вместо номера — скрещённые мечи на тёмно-красном круге.
# Мерили на скрине: доля тёмно-красных пикселей в значке 0.53 у иконки, 0.00-0.05 у обычного
# номера (0.05 — у красной цифры агрессивного моба: она яркая, R > 160, в "тёмно-красный" не идёт).
# Плюс светлые клинки (кремовые, с заметным синим — у жёлтой цифры синего нет): 0.13 у иконки, 0 у номера.
ATTACK_GLOW_MIN_FRAC = 0.20
ATTACK_CREAM_MIN_FRAC = 0.02
TEXT_MIN_MAX = 90                         # ярче этого в зоне цифр — текст есть
DIGIT_MIN_SCORE = 0.70                    # уверенное совпадение с образцом
# Серое имя = моб попал в скан, но уже ушёл далеко (игра "гасит" строку). Цветных (жёлтых/
# красных) пикселей имени у живой строки 207-282, у серой 0; серых у серой ~260. Пороги с
# большим запасом в обе стороны: серость решают оба условия сразу, а не одно.
FADED_MAX_INK = 40                        # меньше стольких цветных пикселей в имени...
FADED_MIN_GREY = 60                       # ...и хотя бы столько серого текста -> строка серая
GLYPH_WIN_H, GLYPH_WIN_W = 12, 9          # окно образца цифры (с фоном вокруг)
SCALE_EXACT_EPS = 0.015                   # масштаб ~100% -> кадр не трогаем (цифры пиксель-в-пиксель)
DIGIT_MAX_GAP = 3                         # зазор между цифрами одного числа, px (замер: 0-3)
MAX_DIGITS = 2                            # метры в списке двузначные: 3 цифры = мусор из имени

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
    hp: "float | None" = None                  # доля HP 0..1 по полоске под именем (None — не прочли)
    attacking: bool = False                    # мечи вместо номера: этот моб бьёт нас (см. _attacking)

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

    def fighting(self, exclude: "set[int] | frozenset[int]" = frozenset()) -> "ListRow | None":
        """Моб, который бьёт нас: сначала по значку мечей (точно, 2026-10-13), иначе — моб вплотную
        (<= FIGHTING_MAX_M) с уже неполным HP. Ближний из таких.
        Проверить без игры: TargetList(True, (ListRow(2, cell, 0, False, True, hp=0.82),
        ListRow(3, cell, 11, False, True))).fighting() -> строка №2."""
        atk = [r for r in self.rows if r.attacking and not r.selected and not r.faded and r.index not in exclude]
        if atk:
            # Строки без метров — в конец (кортеж-ключ: False < True), среди остальных — ближний.
            return min(atk, key=lambda r: (r.distance_m is None, r.distance_m or 0))
        lo, hi = FIGHTING_HP
        near = [r for r in self.rows if not r.selected and not r.faded and r.index not in exclude
                and r.distance_m is not None and r.distance_m <= FIGHTING_MAX_M
                and r.hp is not None and lo < r.hp < hi]
        # min с ключом-кортежем: сначала ближний, при равенстве — самый раненый (одна строка, без сортировки всего)
        return min(near, key=lambda r: (r.distance_m, r.hp)) if near else None

    def nearest(self, exclude: "set[int] | frozenset[int]" = frozenset()) -> "ListRow | None":
        """
        Ближний моб, кроме уже выбранного, серых (ушли далеко) и номеров из exclude. Строки
        без прочитанного расстояния — в самом конце (лучше хоть какая-то цель, чем никакой).
        """
        cands = [r for r in self.rows if not r.selected and not r.faded and r.index not in exclude]
        if not cands:
            return None
        fight = self.fighting(exclude)             # бьёт нас вплотную — важнее всех (2026-10-11)
        if fight is not None:
            return fight
        # ЖЕЛЕЗНОЕ ДОБИВАНИЕ (2026-10-06): раненый моб в списке (полоска под именем почти серая)
        # важнее ближнего — это недобитая цель (наша, сброшенная игрой, или чужая), её добиваем
        # первой. Самый раненый, при равенстве — ближний. Дальше WOUNDED_MAX_M — не бежим через карту.
        wounded = [r for r in cands if r.hp is not None and r.hp <= WOUNDED_HP_MAX
                   and r.distance_m is not None and r.distance_m <= WOUNDED_MAX_M]
        if wounded:
            return min(wounded, key=lambda r: (round(r.hp, 1), r.distance_m is None, r.distance_m or 0))
        return min(cands, key=lambda r: (r.distance_m is None, r.distance_m or 0, r.index))


# ---------------------------------------------------------------------------
# Образцы цифр
# ---------------------------------------------------------------------------

def _load_templates(path: str = _TEMPLATES_PATH) -> "tuple[dict[str, list[np.ndarray]], dict[float, dict[str, list[np.ndarray]]]]":
    """
    (образцы 100%, {масштаб: образцы}). Ключи npz: "d3_0" — цифра 3, снята при 100%;
    "d3_s9375_0" — снята с панели 93.75% (кадр приведён к 100% тем же warp_to_reference,
    что и при чтении — поэтому совпадает с живыми кадрами этого масштаба почти 1:1).
    Нет файла — пустые словари (всё по форме).
    """
    if not os.path.exists(path):
        return {}, {}
    data = np.load(path)
    base: "dict[str, list[np.ndarray]]" = {}
    scaled: "dict[float, dict[str, list[np.ndarray]]]" = {}
    for key in data.files:
        parts = key.split("_")
        t = data[key].astype(np.float32)
        if len(parts) == 3 and parts[1].startswith("s"):
            sc = int(parts[1][1:]) / 10000.0
            scaled.setdefault(sc, {}).setdefault(parts[0][1], []).append(t)
        else:
            base.setdefault(key[1], []).append(t)
    return base, scaled


_TEMPLATES, _NATIVE_TEMPLATES = _load_templates()
_SCALED_TEMPLATES: "dict[float, dict[str, list[np.ndarray]]]" = {}
NATIVE_MATCH_EPS = 0.02                   # образцы масштаба 93.75% годятся для панели 92-96%


def _templates_for(scale: float) -> "dict[str, list[np.ndarray]]":
    """
    Образцы под панель в масштабе `scale`:
      - есть снятые с панели этого масштаба (_NATIVE_TEMPLATES) — они главные: игра рисует
        шрифт под размер сама, и растянутые 100%-образцы на нём путают 5/6/8 (72 м -> 55 м);
      - для цифр, которых в "родных" ещё нет, — 100%-образец, уменьшенный до scale и
        растянутый обратно (как сам кадр) — грубая замена, пока не наберём настоящие.
    Кэш по масштабу (с точностью 1%): считаем один раз, дальше — словарь.
    """
    if abs(scale - 1.0) < SCALE_EXACT_EPS:
        return _TEMPLATES
    key = round(scale, 2)
    cached = _SCALED_TEMPLATES.get(key)
    if cached is not None:
        return cached
    native: "dict[str, list[np.ndarray]]" = {}
    for sc, d2t in _NATIVE_TEMPLATES.items():
        if abs(sc - scale) <= NATIVE_MATCH_EPS:
            for d, lst in d2t.items():
                native.setdefault(d, []).extend(lst)
    down = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
    up = cv2.INTER_LINEAR if scale < 1.0 else cv2.INTER_AREA
    out: "dict[str, list[np.ndarray]]" = {d: list(lst) for d, lst in native.items()}
    for d, tmpls in _TEMPLATES.items():
        if d in native:
            continue                           # "родные" есть — растянутые только мешали бы
        lst = []
        for t in tmpls:
            h, w = t.shape
            small = cv2.resize(t, (max(2, round(w * scale)), max(2, round(h * scale))), interpolation=down)
            lst.append(cv2.resize(small, (w, h), interpolation=up).astype(np.float32))
        out[d] = lst
    _SCALED_TEMPLATES[key] = out
    return out


def has_native_templates(scale: float) -> bool:
    """Есть ли образцы цифр, снятые с панели этого масштаба (или 100% — свои всегда есть)."""
    if abs(scale - 1.0) < SCALE_EXACT_EPS:
        return True
    return any(abs(sc - scale) <= NATIVE_MATCH_EPS for sc in _NATIVE_TEMPLATES)


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


def classify_digit(zone: np.ndarray, a: int, b: int, scale: float = 1.0) -> "tuple[str, float]":
    """
    (цифра, уверенность). Образец сдвигаем по окну глифа на +-2 px (cv2.matchTemplate,
    TM_CCOEFF_NORMED — корреляция без учёта яркости и фона), берём лучшую цифру.
    Нет уверенного совпадения -> это одна из цифр без образца (0 или 5): по дырке.
    """
    cx = (a + b) // 2                          # окно шире образца: и узкая "1" влезает
    x0, x1 = max(0, cx - 7), min(zone.shape[1], cx + 8)
    region = zone[2:18, x0:x1].astype(np.float32)        # +-2 строки запаса по высоте
    best_d, best_s = "?", -1.0
    templates = _templates_for(scale)
    for d, tmpls in templates.items():
        for t in tmpls:
            if region.shape[0] < t.shape[0] or region.shape[1] < t.shape[1]:
                continue
            s = float(cv2.matchTemplate(region, t, cv2.TM_CCOEFF_NORMED).max())
            if s > best_s:
                best_d, best_s = d, s
    if best_s >= DIGIT_MIN_SCORE:
        return best_d, best_s
    win = glyph_window(zone, a, b)
    known = set(templates)
    guess = "0" if _has_hole(win) else "5"
    if guess in known:                         # образец есть, но совпадение слабое — честно "не знаю"
        return best_d, best_s
    return guess, 0.5


def read_distance(cell_img: np.ndarray, scale: float = 1.0) -> "tuple[int | None, float, list[tuple[str, np.ndarray]]]":
    """
    (метры, уверенность, сомнительные глифы). Текст выровнен вправо: последний глиф —
    "м", перед ним 1-2 цифры.
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
    # Цифры — плотная группа прямо перед "м": между цифрами 0-3 px, "м" — через 4-6 px
    # (замеры на 19 строках, 100% и 93.75%). Идём от "м" влево и берём глифы, пока зазор
    # маленький, и не больше двух: метры в списке двузначные (до ~70 м). Всё левее — хвост
    # имени, залезший в зону цифр (лог 2026-10-05: "619 м" вместо 19, "116 м" вместо 16).
    cands = [g for g in glyphs[:-1] if g[1] <= m_a - 2]
    if not cands:
        return None, 0.0, []
    digits = [cands[-1]]
    for g in reversed(cands[:-1]):
        if len(digits) >= MAX_DIGITS or digits[0][0] - g[1] > DIGIT_MAX_GAP:
            break
        digits.insert(0, g)
    text, conf, unsure = "", 1.0, []
    for a, b in digits:
        d, s = classify_digit(zone, a, b, scale)
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


def _hp_bar(cell: np.ndarray) -> "tuple[float, float | None]":
    """
    (доля ширины, занятая полоской HP: заливка + дорожка; HP 0..1 или None).
    Столбец — "заливка", если в нём есть светло-серый пиксель, иначе "дорожка", если тёмно-серый
    нейтральный (каналы почти равны: фиолетовый фон выбранной строки и тёмный фон пустой не подходят).
    """
    band = cell[UNDERLINE_Y[0]:UNDERLINE_Y[1], UNDERLINE_X[0]:UNDERLINE_X[1]].astype(np.int16)
    lo, hi = band.min(axis=2), band.max(axis=2)
    fill = ((lo > 85) & ((hi - lo) < 60)).any(axis=0)    # светлая и серая (не жёлтая, не фиолетовая)
    track = ((lo >= TRACK_LO) & (hi <= TRACK_HI) & ((hi - lo) < TRACK_SPREAD)).any(axis=0) & ~fill
    bar = fill | track
    n = int(bar.sum())
    if n == 0:
        return 0.0, None
    # Настоящая полоска начинается с заливки (HP растёт слева) и идёт сплошняком. Одна тёмно-серая
    # дорожка без заливки — это мир сквозь полупрозрачную панель (серый камень), а не моб с 0% HP:
    # мёртвый моб из списка исчезает (list_locate_test V8: ложная строка "2:72м").
    idx0 = int(np.flatnonzero(bar)[0])
    if not fill[idx0]:
        return float(fill.mean()), None
    # HP — по длине заливки от начала полоски (заливка растёт слева): правее первой дорожки
    # светлое — это уже не заливка (блик, буква), его не считаем.
    idx = np.flatnonzero(bar)
    first_track = np.flatnonzero(track[idx[0]:])
    filled = int(fill[idx[0]:idx[0] + (first_track[0] if first_track.size else n)].sum())
    return float(bar.mean()), min(1.0, filled / n)


def _underline_frac(cell: np.ndarray) -> float:
    return _hp_bar(cell)[0]


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


def _attacking(cell: np.ndarray) -> bool:
    """
    Значок "бьёт нас" вместо номера строки: тёмно-красный круг + светлые скрещённые клинки.
    Две маски numpy на окне 22x22 — микросекунды. Песочница: tools/pull_logic_test.py (блок A —
    синтетический значок, обычный жёлтый и красный номер).
    """
    x, y, w, h = BADGE_RECT
    c = cell[y:y + h, x:x + w].astype(np.int16)
    b, g, r = c[..., 0], c[..., 1], c[..., 2]
    # Целочисленное сравнение вместо деления: g/r < 0.55 -> g*100 < r*55 (нет деления на ноль).
    glow = (r >= 50) & (r <= 160) & (g * 100 < r * 55) & (b * 100 < r * 75)
    if float(glow.mean()) < ATTACK_GLOW_MIN_FRAC:
        return False                                      # early return: обычный номер — частый случай
    cream = (r > 150) & (g > 120) & (b > 90)
    return float(cream.mean()) >= ATTACK_CREAM_MIN_FRAC


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


def read_target_list(frame: "np.ndarray | None", scale: float = 1.0) -> TargetList:
    """
    Кадр зоны TARGET_LIST_* (BGR) -> TargetList. ~1 мс: 8 ячеек, в каждой пара
    срезов numpy и до трёх matchTemplate на крошечных окнах — безопасно для 60 FPS.
    scale — масштаб панели в игре, если кадр уже приведён к 100% через to_reference
    (тогда цифры сравниваются ещё и с "размытыми" образцами, см. _templates_for).
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
            bar_frac, hp = _hp_bar(cell)              # один проход: и "строка ли это", и HP строки
            if bar_frac < UNDERLINE_MIN_FRAC:
                continue
            dist, conf, uns = read_distance(cell, scale)
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
                hp=hp,
                attacking=_attacking(cell),
            ))
    return TargetList(True, tuple(rows), "ok" if rows else "пусто", tuple(unsure))


# ---------------------------------------------------------------------------
# Автопоиск панели (2026-10-05: игрок сдвинул панель вверх на 59 px и уменьшил до ~93% —
# бот перестал видеть список). Ищем панель по шапке вкладок "Персонаж | Группа |
# Контратака" в любом месте экрана и в любом масштабе 70-135%, а кадр зоны приводим к
# 100% (cv2.resize) -> все пороги и образцы цифр выше остаются как есть.
# ---------------------------------------------------------------------------

REF_W, REF_H = 420, 194                   # зона при масштабе 100% (TARGET_LIST_WIDTH/HEIGHT)
_TAB_TMPL_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "config", "list_tab.png")
TAB_TMPL_ORIGIN = (5, 3)                  # где шапка вкладок лежит внутри зоны при 100%
LOCATE_SCALES = tuple(round(0.70 + 0.05 * i, 2) for i in range(14))   # 0.70..1.35, грубо
LOCATE_COARSE = 0.5                       # грубый проход на кадре в полразмера: в 4 раза быстрее
LOCATE_MIN_SCORE = 0.6                    # точное совпадение шапки (100% -> 1.0, 93% -> ~0.72)
# Зона может выходить за край экрана на столько px (скрин 2026-10-05 21:40: список прижат к
# левому краю, шапка "Персонаж" с x=0, а 5-px поле зоны слева от неё — уже за экраном).
LOCATE_EDGE_SLACK_PX = 12

_TAB_TMPL = cv2.imread(_TAB_TMPL_PATH, cv2.IMREAD_GRAYSCALE) if os.path.exists(_TAB_TMPL_PATH) else None


def to_reference(zone: "np.ndarray | None", scale: float) -> "np.ndarray | None":
    """Кадр зоны в масштабе `scale` -> 420x194 (как при 100%). ~0.1 мс на крошечной зоне."""
    if zone is None or zone.size == 0 or abs(scale - 1.0) < SCALE_EXACT_EPS:
        return zone
    # INTER_LINEAR при увеличении (панель меньше 100%), INTER_AREA при уменьшении — без "лесенки".
    interp = cv2.INTER_LINEAR if scale < 1.0 else cv2.INTER_AREA
    return cv2.resize(zone, (REF_W, REF_H), interpolation=interp)


def warp_to_reference(region: "np.ndarray | None", ox: float, oy: float, scale: float) -> "np.ndarray | None":
    """
    Кусок экрана, в котором зона списка начинается в точке (ox, oy) (дробные px) и имеет
    масштаб `scale` -> зона 420x194 как при 100%. Одна cv2.warpAffine (~0.1 мс): сдвиг на
    доли пикселя и масштаб за один проход — без потери полпикселя на округлении угла, из-за
    которой строки "уезжали" на 1-2 px и цифры читались неверно. 100% и целый угол -> срез.
    """
    if region is None or region.size == 0:
        return region
    if abs(scale - 1.0) < SCALE_EXACT_EPS and float(ox).is_integer() and float(oy).is_integer() \
            and ox >= 0 and oy >= 0 and oy + REF_H <= region.shape[0] and ox + REF_W <= region.shape[1]:
        x, y = int(ox), int(oy)
        return region[y:y + REF_H, x:x + REF_W]   # срез — только целиком внутри (у края экрана — warp с краем)
    m = np.float32([[scale, 0.0, ox], [0.0, scale, oy]])        # точка зоны (100%) -> пиксель куска
    return cv2.warpAffine(region, m, (REF_W, REF_H), flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
                          borderMode=cv2.BORDER_REPLICATE)


# Полоски под именами — самые чёткие линии панели: по ним уточняем угол и масштаб до долей
# пикселя (шапка вкладок даёт только +-2 px: шрифт при другом размере рисуется иначе).
# Замеры при 100% (кадр 2026-10-04): полоска столбца 1 начинается в x=42 зоны, длина 160,
# центры по y у строк 1-4: 65.5 / 101 / 136 / 171.5.
UL_X0, UL_LEN = 42, 160
UL_CY = (65.5, 101.0, 136.0, 171.5)


def _underline_segments(region: np.ndarray) -> "list[tuple[float, int, int]]":
    """Светло-серые горизонтальные полосы длиной 100-220 px: (центр по y, x начала, длина)."""
    z = region.astype(np.int16)
    lo, hi = z.min(axis=2), z.max(axis=2)
    mask = ((lo > 85) & ((hi - lo) < 50)).astype(np.int8)
    edges = np.diff(np.pad(mask, ((0, 0), (1, 1))), axis=1)      # +1 начало пробега, -1 конец
    groups: "list[list[int]]" = []                               # [y0, y1, x0, длина]
    for y in range(mask.shape[0]):
        starts = np.flatnonzero(edges[y] == 1)
        ends = np.flatnonzero(edges[y] == -1)
        for a, b in zip(starts, ends):
            ln = int(b - a)
            if not 100 <= ln <= 220:
                continue
            for g in groups:                                     # продолжение той же полосы строкой ниже
                if g[1] == y - 1 and abs(g[2] - a) <= 2 and abs(g[3] - ln) <= 3:
                    g[1] = y
                    break
            else:
                groups.append([y, y, int(a), ln])
    return [((g[0] + g[1]) / 2.0, g[2], g[3]) for g in groups if g[1] - g[0] <= 3]   # толще 4 px — не полоска


def refine_geometry(frame: np.ndarray, x: float, y: float, scale: float) -> "tuple[float, float, float] | None":
    """
    Уточнить угол зоны и масштаб по полоскам под именами: (x, y, масштаб) в координатах
    кадра или None — полосок нет (список пуст), оставляем грубую оценку по шапке.
    Каждая полоска сверяется с ожидаемым местом (столбец/строка) и длиной — чужие светлые
    линии (чат, мир) не пройдут. Масштаб = длина/160, угол — медиана по всем полоскам.
    """
    pad = 12
    x0, y0 = max(0, int(x) - pad), max(0, int(y) - pad)
    reg = frame[y0:int(y + REF_H * scale) + pad, x0:int(x + REF_W * scale) + pad]
    if reg.size == 0:
        return None
    found = []
    for cy, ax, ln in _underline_segments(reg):
        if abs(ln / UL_LEN - scale) > 0.05:
            continue
        gx, gy = x0 + ax, y0 + cy
        cols = [x + scale * (UL_X0 + cx - COL_X[0]) for cx in COL_X]
        rows = [y + scale * c for c in UL_CY]
        ci = int(np.argmin([abs(gx - c) for c in cols]))
        ri = int(np.argmin([abs(gy - r) for r in rows]))
        if abs(gx - cols[ci]) <= 8 and abs(gy - rows[ri]) <= 6:
            found.append((gx, gy, ln, ci, ri))
    if not found:
        return None
    sc = float(np.mean([f[2] for f in found])) / UL_LEN
    if abs(sc - 1.0) < SCALE_EXACT_EPS:
        sc = 1.0
    zx = float(np.median([f[0] - sc * (UL_X0 + COL_X[f[3]] - COL_X[0]) for f in found]))
    zy = float(np.median([f[1] - sc * UL_CY[f[4]] for f in found]))
    if sc == 1.0:                                  # 100%: угол целый — читаем срезом, пиксель-в-пиксель
        zx, zy = float(round(zx)), float(round(zy))
    return zx, zy, sc


def locate_list(frame: "np.ndarray | None") -> "tuple[float, float, float, float, bool] | None":
    """
    Где на кадре панель списка: (x, y, масштаб, совпадение, точно) левого верхнего угла ЗОНЫ
    (той же, что TARGET_LIST_* при 100%) или None — шапки "Персонаж" не нашли. x, y — дробные:
    "точно" = уточнили по полоскам строк (refine_geometry); False — только по шапке (+-2 px),
    список пуст — уточним позже, когда появятся строки.

    Два прохода (coarse-to-fine — индустриальный стандарт поиска шаблона в неизвестном
    масштабе): грубо на кадре в полразмера по 14 масштабам, потом точно в полном разрешении
    рядом с находкой с шагом 1%. Итог проверяем самим разборщиком (вкладка фиолетовая) —
    случайное совпадение с травой/чатом не пройдёт. ~40-90 мс на весь экран: звать НЕ из
    главного цикла (см. ListLocator в vision_manager — фоновый поток).
    Как проверить без игры: locate_list(cv2.imread("скрин.png")) на полном скриншоте.
    """
    if frame is None or frame.size == 0 or _TAB_TMPL is None:
        return None
    gray = frame if frame.ndim == 2 else cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    small = cv2.resize(gray, None, fx=LOCATE_COARSE, fy=LOCATE_COARSE, interpolation=cv2.INTER_AREA)
    best = (-1.0, 1.0, 0, 0)
    for sc in LOCATE_SCALES:
        k = sc * LOCATE_COARSE
        t = cv2.resize(_TAB_TMPL, None, fx=k, fy=k, interpolation=cv2.INTER_AREA)
        if t.shape[0] > small.shape[0] or t.shape[1] > small.shape[1]:
            continue
        _, mx, _, loc = cv2.minMaxLoc(cv2.matchTemplate(small, t, cv2.TM_CCOEFF_NORMED))
        if mx > best[0]:
            best = (mx, sc, int(loc[0] / LOCATE_COARSE), int(loc[1] / LOCATE_COARSE))
    _, sc0, x0, y0 = best
    fine = (-1.0, 1.0, 0, 0)
    pad = 12
    for i in range(-6, 7):
        sc = round(sc0 + 0.01 * i, 3)
        interp = cv2.INTER_AREA if sc < 1.0 else cv2.INTER_LINEAR
        t = _TAB_TMPL if abs(sc - 1.0) < 1e-6 else cv2.resize(_TAB_TMPL, None, fx=sc, fy=sc, interpolation=interp)
        ax, ay = max(0, x0 - pad), max(0, y0 - pad)
        reg = gray[ay:y0 + t.shape[0] + pad, ax:x0 + t.shape[1] + pad]
        if reg.shape[0] < t.shape[0] or reg.shape[1] < t.shape[1]:
            continue
        _, mx, _, loc = cv2.minMaxLoc(cv2.matchTemplate(reg, t, cv2.TM_CCOEFF_NORMED))
        if mx > fine[0]:
            fine = (mx, sc, ax + loc[0], ay + loc[1])
    score, sc, tx, ty = fine
    if score < LOCATE_MIN_SCORE:
        return None
    if abs(sc - 1.0) < SCALE_EXACT_EPS:
        sc = 1.0
    zx = float(round(tx - TAB_TMPL_ORIGIN[0] * sc))
    zy = float(round(ty - TAB_TMPL_ORIGIN[1] * sc))
    slack = LOCATE_EDGE_SLACK_PX
    if zx < -slack or zy < -slack or zy + REF_H * sc > frame.shape[0] + slack \
            or zx + REF_W * sc > frame.shape[1] + slack:
        return None
    if frame.ndim != 3:
        return zx, zy, float(sc), float(score), False
    precise = False
    fine_geo = refine_geometry(frame, zx, zy, sc)
    if fine_geo is not None:
        zx, zy, sc = fine_geo
        precise = True
    if not read_target_list(warp_to_reference(frame, zx, zy, sc), sc).visible:
        return None                        # шапка похожа, но вкладка не фиолетовая — не наша панель
    return zx, zy, float(sc), float(score), precise


def format_list(tl: TargetList) -> str:
    """Строка для лога: '1:12м 2:4м* 6:37м! 3:30м~ 4:3мX' (* выбран, ! агрессивный, ~ серый, X бьёт нас, ? не прочли)."""
    if not tl.visible:
        return "нет списка (%s)" % tl.reason
    if not tl.rows:
        return "пусто"
    return " ".join(
        "%d:%s%s%s%s%s%s" % (r.index, "?" if r.distance_m is None else "%dм" % r.distance_m,
                           "*" if r.selected else "", "!" if r.hostile else "", "~" if r.faded else "",
                           "X" if r.attacking else "",
                           "(%d%%)" % round(r.hp * 100) if r.hp is not None and r.hp < 0.95 else "")
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
