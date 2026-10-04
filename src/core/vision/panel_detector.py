"""
src/core/vision/panel_detector.py

Распознавание ПАНЕЛИ ЦЕЛИ по её устройству, а не по цвету.

Что было не так раньше: панелью считался любой жёлтый/красный прямоугольник
в зоне панели. В зоне панели иногда просто трава (жёлтые цветы, сухая
земля, красная шерсть моба рядом) -> детектор "находил цель" там, где её
нет, FSM входил в COMBAT и бил воздух.

Как теперь (см. раскладку и числа в src/config/panel_template.py):
  1) ищем РАМКУ полоски: две чёрные горизонтальные линии на расстоянии
     ровно 11 px, длиной ~142 px;
  2) сверху и снизу от них — бежевая окантовка;
  3) справа от рамки — вертикальная чёрная граница и тёмно-малиновая
     кнопка X с серым крестиком;
  и только если ВСЁ совпало — читаем HP из длины жёлтой/красной заливки
  внутри рамки.

Модуль — чистые функции над numpy-кадром (без mss, без состояния): их
можно гонять на PNG-скриншоте прямо в песочнице, не запуская игру:

    import cv2
    from src.core.vision.panel_detector import read_panel
    r = read_panel(cv2.imread("panel.png"))
    print(r.ok, r.reason, r.hp_percent)

Идиома: frozen dataclass для результата — неизменяемая запись, её нельзя
случайно поменять в середине тика, и она хешируемая/сравнимая в тестах.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from src.config import panel_template as T
from src.config import vision_config
from src.core.vision import hp_text

# Цветовые диапазоны заливки берём из vision_config (их ты уже
# откалибровал в песочнице) и готовим массивы ОДИН раз при импорте, а не
# на каждый кадр — np.array(...) на 60 FPS был бы лишней аллокацией.
_RED_1 = (np.array(vision_config.RED_LOWER_1, np.uint8), np.array(vision_config.RED_UPPER_1, np.uint8))
_RED_2 = (np.array(vision_config.RED_LOWER_2, np.uint8), np.array(vision_config.RED_UPPER_2, np.uint8))
_YELLOW = (np.array(vision_config.YELLOW_LOWER, np.uint8), np.array(vision_config.YELLOW_UPPER, np.uint8))


@dataclass(frozen=True)
class PanelResult:
    ok: bool                 # панель цели на экране (рамка + окантовка + кнопка X)
    reason: str              # почему нет / "ok" — для отладочного окна и логов
    hp_percent: float = 0.0  # 0..100, только если ok
    # Прямоугольник ВНУТРЕННОСТИ полоски в координатах кадра (x, y, w, h);
    # (0, 0, 0, 0), если ok == False. Нужен отладочному окну и offset-ам.
    bar: "tuple[int, int, int, int]" = (0, 0, 0, 0)
    plate_checked: bool = True  # False — ROI обрезал кнопку X, проверка слабее
    # Отпечаток ТЕКСТА HP ("1 647/1 647", см. hp_text.py) или None, если текст
    # прочитать нельзя. Точный сигнал "HP цели изменилось", в отличие от длины
    # полоски, которая дрожит на 1-3 п.п.
    text: "hp_text.HpText | None" = None




def _fail(reason: str) -> PanelResult:
    return PanelResult(ok=False, reason=reason)


def _beige_mask(rows: np.ndarray) -> np.ndarray:
    """
    Маска "бежевых" пикселей (R > G > B, R - B >= порога) в куске кадра.
    Приводим к int16: у uint8 разность r - b "заворачивается" через 0
    (3 - 5 = 254), и проверка молча врала бы. Это классическая ловушка
    numpy — арифметику над каналами изображений делают в знаковом типе.
    """
    b = rows[..., 0].astype(np.int16)
    g = rows[..., 1].astype(np.int16)
    r = rows[..., 2].astype(np.int16)
    return (
        (r > g) & (g > b)
        & (r - b >= T.BEIGE_MIN_R_MINUS_B)
        & (r >= T.BEIGE_MIN_R) & (r <= T.BEIGE_MAX_R)
    )


def _beige_fraction(rows: np.ndarray) -> float:
    """Доля бежевых пикселей в куске кадра (для диагностики и тестов)."""
    m = _beige_mask(rows)
    return float(m.mean()) if m.size else 0.0


def _beige_band_fraction(band: np.ndarray) -> float:
    """
    Окантовка с одной стороны рамки = полоса из BEIGE_ROWS строк. Столбец считается
    "бежевым", если бежевая ХОТЯ БЫ ОДНА его строка; возвращаем долю таких столбцов.
    Почему не "каждая строка": обе строки окантовки полупрозрачные, и мир за панелью
    ломает то одну, то другую — на светлом синем фоне внешнюю (R > G > B нарушается),
    на тёмном камне внутреннюю (R падает ниже 70: кадры чёрного ящика 2026-10-03 12:29,
    доля 0.36 у внутренней при 1.0 у внешней). Обе сразу в одном столбце на реальных
    кадрах не ломались. .any(axis=0) — векторно по столбцам, без цикла Python.
    """
    if band.shape[0] == 0 or band.shape[1] == 0:
        return 0.0
    return float(_beige_mask(band).any(axis=0).mean())


def _longest_group(xs: np.ndarray, max_gap: int) -> np.ndarray:
    """
    Из отсортированных индексов xs берёт самую длинную группу, где соседи
    отстоят не дальше max_gap+1. Так мелкий разрыв-насечка внутри чёрной
    линии не рвёт её на два куска, а случайные тёмные пиксели далеко в
    стороне (чёрный шестиугольник уровня слева) не растягивают границы.
    """
    if xs.size == 0:
        return xs
    breaks = np.flatnonzero(np.diff(xs) > max_gap + 1) + 1
    groups = np.split(xs, breaks)
    return max(groups, key=len)


def _trim_edge_runs(xs: np.ndarray, min_run: int) -> np.ndarray:
    """
    Отрезает с КРАЁВ группы короткие (< min_run px) куски, отделённые разрывом.
    Зачем: справа от конца линий рамки идут 2 светлых px торца, а за ними — мир.
    Если мир там тёмный (камень, ночь), _longest_group "перепрыгивает" торец и
    приклеивает к рамке 1-2 тёмных px фона: правый край уезжал на 3 px (168 -> 171
    на кадрах чёрного ящика 2026-10-03), HP читалось 97.9% вместо 100%, а ширина
    зоны текста HP менялась 98 <-> 99 — шлагбаум принял бы это за "HP изменилось".
    Насечки ВНУТРИ линии не трогаем: режем только с краёв.
    """
    if xs.size == 0:
        return xs
    runs = np.split(xs, np.flatnonzero(np.diff(xs) > 1) + 1)
    while len(runs) > 1 and len(runs[-1]) < min_run:
        runs.pop()
    while len(runs) > 1 and len(runs[0]) < min_run:
        runs.pop(0)
    return np.concatenate(runs)


def _fill_columns(region: np.ndarray, hsv: "np.ndarray | None" = None) -> np.ndarray:
    """
    Булев вектор по колонкам: True там, где в колонке есть заливка HP.
    hsv — уже посчитанный HSV того же куска: его же берёт читатель текста, и
    конвертировать один и тот же кусок дважды за кадр незачем.
    """
    if hsv is None:
        hsv = cv2.cvtColor(region, cv2.COLOR_BGR2HSV)
    mask = (
        cv2.inRange(hsv, _YELLOW[0], _YELLOW[1])
        | cv2.inRange(hsv, _RED_1[0], _RED_1[1])
        | cv2.inRange(hsv, _RED_2[0], _RED_2[1])
    )
    return np.count_nonzero(mask, axis=0) >= T.FILL_MIN_ROWS


def _hp_from_fill(flags: np.ndarray) -> float:
    """
    HP% по колонкам заливки. Заливка растёт слева направо, поэтому берём
    ПЕРВУЮ непрерывную полосу (разрывы <= FILL_MAX_GAP — текст "1 647/1 647"
    и насечка не считаются концом) и её длину делим на полную ширину.
    Ничего правее первого большого разрыва не учитываем: это уже не
    заливка, а шум (блик, чужой жёлтый пиксель в пустой части полоски).
    """
    idx = np.flatnonzero(flags)
    if idx.size == 0 or idx[0] > T.FILL_MAX_GAP:
        return 0.0
    end = int(idx[0])
    for i in idx[1:]:
        if i - end - 1 > T.FILL_MAX_GAP:
            break
        end = int(i)
    return min(100.0, round((end + 1) / flags.size * 100.0, 2))


def read_panel(frame: np.ndarray) -> PanelResult:
    """
    Главная функция: кадр зоны панели (BGR, как отдаёт mss) -> PanelResult.

    Порядок проверок от дешёвых к дорогим: если уже "чёрных линий нет" (а
    это ~всегда так на пустой траве), остальное не считаем вообще — на
    60 FPS цикле основное время и должно уходить на "ничего не найдено".

    Как тестировать без игры: read_panel(cv2.imread("скрин_панели.png")) —
    на скриншоте ровно зоны панели (190x90 px) должно быть ok=True; на
    любом куске травы/неба/мира — ok=False с понятным reason.
    """
    if frame is None or frame.ndim != 3 or frame.shape[2] < 3:
        return _fail("нет кадра")
    frame = frame[..., :3]
    h, w = frame.shape[:2]

    # --- 1) Чёрные линии рамки -------------------------------------------
    # max по каналам <= порога: тёмным считаем пиксель, где ВСЕ каналы малы
    # (а не серую яркость) — иначе тёмно-синее/тёмно-красное тоже сошло бы.
    dark = frame.max(axis=2) <= T.FRAME_DARK_MAX
    row_counts = np.count_nonzero(dark, axis=1)
    cand_rows = np.flatnonzero(row_counts >= T.FRAME_SPAN_MIN * T.FRAME_LINE_FILL_MIN)
    if cand_rows.size < 2:
        return _fail("нет чёрных линий рамки")

    gap_lo = T.FRAME_LINE_GAP - T.FRAME_LINE_GAP_TOL
    gap_hi = T.FRAME_LINE_GAP + T.FRAME_LINE_GAP_TOL

    last_reason = "нет пары линий на расстоянии %d px" % T.FRAME_LINE_GAP
    for yt in cand_rows:
        ybs = cand_rows[(cand_rows >= yt + gap_lo) & (cand_rows <= yt + gap_hi)]
        # Сначала пара ровно на FRAME_LINE_GAP, потом соседние: при низком HP пустая
        # часть полоски тёмная, и строка ВНУТРИ рамки тоже проходит как "чёрная линия".
        # sorted(key=...) — пара "верхняя линия + тёмная строка полоски" (зазор 10)
        # проверяется только если настоящая (зазор 11) не прошла.
        for yb in sorted(ybs.tolist(), key=lambda v: abs(v - yt - T.FRAME_LINE_GAP)):
            res = _check_candidate(frame, dark, int(yt), int(yb), w, h)
            if res.ok:
                return res
            last_reason = res.reason
    return _fail(last_reason)


def _check_candidate(frame: np.ndarray, dark: np.ndarray,
                     yt: int, yb: int, w: int, h: int) -> PanelResult:
    """Проверяет ОДНУ пару чёрных линий (yt — верхняя, yb — нижняя)."""
    both = dark[yt] & dark[yb]
    group = _trim_edge_runs(_longest_group(np.flatnonzero(both), T.FRAME_LINE_MAX_GAP),
                            T.FRAME_EDGE_RUN_MIN)
    if group.size == 0:
        return _fail("у линий нет общего участка")

    x_left, x_right = int(group[0]), int(group[-1])
    span = x_right - x_left + 1
    if not (T.FRAME_SPAN_MIN <= span <= T.FRAME_SPAN_MAX):
        return _fail("длина линий %d px вне диапазона" % span)
    if group.size < span * T.FRAME_LINE_FILL_MIN:
        return _fail("линии рамки с большими разрывами")

    # --- 2) Правая вертикальная граница рамки ----------------------------
    # Ищем самый тёмный столбец в 0..FRAME_EDGE_SEARCH_PX правее конца линий: у новой
    # панели (2026-10-04) граница на 1 px дальше и светлее порога линий (29-34 против 24).
    edge_best = 0.0
    for c in range(x_right, min(w, x_right + T.FRAME_EDGE_SEARCH_PX + 1)):
        col = frame[yt:yb + 1, c].max(axis=1) <= T.FRAME_EDGE_DARK_MAX
        edge_best = max(edge_best, float(col.mean()))
    if edge_best < T.FRAME_RIGHT_EDGE_MIN:
        return _fail("нет правой границы рамки")

    # --- 3) Бежевая окантовка сверху и снизу ------------------------------
    # Полоса из BEIGE_ROWS строк над верхней линией и под нижней. Окантовка
    # полупрозрачная, поэтому требуем не "каждую строку", а "в каждом столбце хоть
    # одна строка бежевая" (см. _beige_band_fraction): иначе на тёмном фоне панель
    # выбранного моба не узнавалась вовсе и бот не входил в бой (лог 2026-10-03
    # 12:29, 15 раз подряд "нет бежевой окантовки снизу"). Защиту от ложных панелей
    # держат остальные проверки: две чёрные линии ровно в 11 px, правая граница и
    # малиновая кнопка X с крестиком.
    xa, xb = x_left + 2, x_right - 1
    if yt - 1 < 0 or yb + 1 >= h:
        return _fail("окантовка рамки вне ROI")
    top = frame[max(0, yt - T.BEIGE_ROWS):yt, xa:xb]
    bottom = frame[yb + 1:min(h, yb + 1 + T.BEIGE_ROWS), xa:xb]
    if _beige_band_fraction(top) < T.BEIGE_FRACTION_MIN:
        return _fail("нет бежевой окантовки сверху")
    if _beige_band_fraction(bottom) < T.BEIGE_FRACTION_MIN:
        return _fail("нет бежевой окантовки снизу")

    # --- 4) Кнопка X ------------------------------------------------------
    px0 = x_right + T.PLATE_DX_FROM
    px1 = min(x_right + T.PLATE_DX_TO + 1, w)
    plate_checked = (px1 - px0) >= T.PLATE_MIN_COLUMNS
    if plate_checked:
        plate = frame[yt + T.PLATE_DY_FROM: yt + T.PLATE_DY_TO + 1, px0:px1].astype(np.int16)
        b, g, r = plate[..., 0], plate[..., 1], plate[..., 2]
        gb = np.maximum(g, b)
        crimson = (
            (r >= T.PLATE_R_MIN) & (r <= T.PLATE_R_MAX)
            & (gb <= T.PLATE_GB_MAX) & (r >= gb * T.PLATE_R_OVER_GB)
        )
        if crimson.mean() < T.PLATE_CRIMSON_MIN:
            return _fail("нет малиновой плашки кнопки X")
        glyph = np.minimum(np.minimum(b, g), r) >= T.PLATE_GLYPH_MIN_CHANNEL
        if np.count_nonzero(glyph) < T.PLATE_GLYPH_MIN_PIXELS:
            return _fail("нет крестика на кнопке X")

    # --- 5) Всё совпало: читаем HP ----------------------------------------
    fx0 = x_left + T.FILL_LEFT_PAD
    fx1 = x_right - T.FILL_RIGHT_PAD + 1          # срез правая граница не включает
    interior = frame[yt + 1: yb, fx0:fx1]
    # Один HSV-проход на два чтения: длина заливки (HP в процентах) и текст HP.
    hsv = cv2.cvtColor(interior, cv2.COLOR_BGR2HSV)
    hp = _hp_from_fill(_fill_columns(interior, hsv))
    return PanelResult(
        ok=True, reason="ok", hp_percent=hp,
        bar=(fx0, yt + 1, fx1 - fx0, yb - yt - 1),
        plate_checked=plate_checked,
        text=hp_text.extract(hsv),
    )
