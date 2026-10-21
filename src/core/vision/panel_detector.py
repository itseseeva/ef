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

import contextvars
import math
import threading
import types
from dataclasses import dataclass

import cv2
import numpy as np

from src.config import panel_template as _T_BASE
from src.config import vision_config
from src.core.vision import hp_text

# --- Масштаб интерфейса (2026-10-11) ------------------------------------------------------------
# Все числа рамки (зазор 11 px, длина ~142 px, окантовка 2 строки...) сняты с интерфейса автора
# (1920x1080, список целей 92.5%). У друга 2560x1440 и интерфейс в ~1.28 раза крупнее: рамка
# 270 px "вне диапазона", панель не узнавалась, бот не входил в бой (лог друга 2026-10-11: 77 раз
# "клик не выбрал цель"). Уменьшать картинку до эталона пробовали — тонкие линии рамки после
# пересчёта пикселей размываются и проверки ломаются (симуляция: 1 удача из 12). Поэтому наоборот:
# картинку не трогаем, а ПИКСЕЛЬНЫЕ числа шаблона умножаем на масштаб.
#
# Как без правки 40 мест: T — "прокси", который отдаёт константы текущего масштаба. Масштаб
# хранится в contextvars — у каждого потока свой (главный читает панель, фоновый PanelLocator
# ищет её по экрану — они не мешают друг другу). Масштаб 1.0 — ровно прежние числа.
_PX = ("FRAME_LINE_MAX_GAP", "PANEL_LINE_X_IN_ROI", "PANEL_LINE_Y_IN_ROI", "FRAME_LINE_GAP", "FRAME_EDGE_RUN_MIN",
       "BEIGE_ROWS", "PLATE_DX_FROM", "PLATE_DY_FROM",
       "FILL_MIN_ROWS", "PLATE_MIN_COLUMNS")                       # длины: округляем
# Отступы заливки от торцов — вверх: при дробном масштабе крайний столбец смешивается со светлым
# торцом рамки и выглядит "жёлтым" — полоска красного моба читалась как жёлтая на 1 столбец (HP 0.6%).
_PX_UP = ("FRAME_EDGE_SEARCH_PX", "FILL_MAX_GAP", "PLATE_DX_TO", "PLATE_DY_TO",
          "FRAME_SPAN_MAX", "FILL_LEFT_PAD", "FILL_RIGHT_PAD")      # допуски/верхние границы: вверх
_PX_DOWN = ("FRAME_SPAN_MIN",)                                      # нижние границы: вниз
# Чёрная линия рамки толщиной 1 px при дробном масштабе игра рисует сглаженной: строка уже не
# "чисто чёрная", а тёмно-серая. Порог тёмного при масштабе — выше (эталон 24/45).
_SCALED_DARK_MAX = None
_SCALED_DIM_MAX = None
_T_CUR = contextvars.ContextVar("panel_template", default=_T_BASE)
_T_CACHE: "dict[float, types.SimpleNamespace]" = {}
_T_LOCK = threading.Lock()


class _TemplateProxy:
    """T.X -> константа шаблона ТЕКУЩЕГО масштаба (contextvars). Доступ ~0.1 мкс — на кадр незаметно."""
    __slots__ = ()

    def __getattr__(self, name):
        return getattr(_T_CUR.get(), name)


T = _TemplateProxy()


def scaled_template(scale: float) -> "types.SimpleNamespace":
    """Копия panel_template с пиксельными числами x scale (кэш по масштабу с шагом 0.01)."""
    key = round(float(scale), 2)
    ns = _T_CACHE.get(key)
    if ns is not None:
        return ns
    with _T_LOCK:
        base = {k: getattr(_T_BASE, k) for k in dir(_T_BASE) if k.isupper()}
        for k in _PX:
            base[k] = max(1, int(round(base[k] * key)))
        for k in _PX_UP:
            base[k] = int(math.ceil(base[k] * key))
        for k in _PX_DOWN:
            base[k] = int(math.floor(base[k] * key))
        # Площадь крестика на кнопке X растёт как квадрат масштаба.
        base["PLATE_GLYPH_MIN_PIXELS"] = max(1, int(round(_T_BASE.PLATE_GLYPH_MIN_PIXELS * key * key)))
        if abs(key - 1.0) >= 0.05:
            # Зазор между линиями при дробном масштабе "гуляет" на 1 px сильнее (11 -> 14 или 15).
            base["FRAME_LINE_GAP_TOL"] = _T_BASE.FRAME_LINE_GAP_TOL + 1
            if _SCALED_DARK_MAX is not None:
                base["FRAME_DARK_MAX"] = _SCALED_DARK_MAX
            if _SCALED_DIM_MAX is not None:
                base["FRAME_LINE_DIM_MAX"] = _SCALED_DIM_MAX
        ns = _T_CACHE[key] = types.SimpleNamespace(**base)
    return ns


def _with_scale(fn, scale: float, *args):
    """Вызвать fn с шаблоном масштаба scale в ЭТОМ потоке; 1.0 — без переключения (как раньше)."""
    if abs(scale - 1.0) < 0.005:
        return fn(*args)
    token = _T_CUR.set(scaled_template(scale))
    try:
        return fn(*args)
    finally:
        _T_CUR.reset(token)

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
    # СЕРАЯ полоска HP (blackbox 2026-10-09 21:37, "Дворф-егерь" 9796/13957): моба бьёт ДРУГОЙ
    # игрок (под панелью "цель цели" — его ник). Заливка не жёлтая и не красная, а нейтрально-серая;
    # раньше её читали как HP 0% -> "моб мёртв" -> бот бросал живого моба с 70%.
    foreign: bool = False
    # СВЕТЛО-СЕРАЯ (почти белая) полоска — это не моб, а ИГРОК (blackbox 2026-10-09 22:01:
    # "Shinka" 33 442/33 442, гильдия PUZO). Бот выбрал игрока из списка и считал его "убитым".
    player: bool = False




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
    yellow = np.count_nonzero(cv2.inRange(hsv, _YELLOW[0], _YELLOW[1]), axis=0) >= T.FILL_MIN_ROWS
    # Красная ПУСТАЯ часть (2026-10-06 01:48, "Дворф-рубака" 2128/13957): потерянное HP
    # закрашено тёмно-красным (V 60-80), и красный диапазон принимал его за заливку —
    # HP 15% читалось как 100%, бот бросал недобитую цель. Если полоска начинается жёлтым,
    # заливка — только жёлтое; красный учитываем лишь у полосок, где жёлтого слева нет.
    if yellow[:T.FILL_MAX_GAP + 1].any():
        return yellow
    red = cv2.inRange(hsv, _RED_1[0], _RED_1[1]) | cv2.inRange(hsv, _RED_2[0], _RED_2[1])
    return yellow | (np.count_nonzero(red, axis=0) >= T.FILL_MIN_ROWS)


# Серая заливка: почти без цвета (S <= 40) и средней яркости (V 70-160). Белый текст HP
# (V > 200), тёмная пустая часть (V ~25) и красная пустая часть (S высокая) сюда не попадают.
_GRAY_S_MAX = 40
_GRAY_V = (70, 160)
_GRAY_MIN_ROWS = 3
# Полоска игрока: бесцветная (S <= 18) и светлая (V 150-200); текст HP ещё светлее (V > 200)
# и стоит в середине полоски, а заливка игрока начинается от левого края.
_PLAYER_S_MAX = 18
_PLAYER_V = (150, 200)
_PLAYER_MIN_FILL = 10.0   # светлый блик рамки у левого края (1-2 колонки) — не полоска игрока


def _player_fill_columns(hsv: np.ndarray) -> np.ndarray:
    g = (hsv[..., 1] <= _PLAYER_S_MAX) & (hsv[..., 2] >= _PLAYER_V[0]) & (hsv[..., 2] <= _PLAYER_V[1])
    return np.count_nonzero(g, axis=0) >= _GRAY_MIN_ROWS


def _gray_fill_columns(hsv: np.ndarray) -> np.ndarray:
    g = (hsv[..., 1] <= _GRAY_S_MAX) & (hsv[..., 2] >= _GRAY_V[0]) & (hsv[..., 2] <= _GRAY_V[1])
    return np.count_nonzero(g, axis=0) >= _GRAY_MIN_ROWS


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


# Сколько пар линий проверяем при подсказке expect_y. Без подсказки (как всегда у автора) — без
# ограничения. Зачем: у друга (тёмная пещера, интерфейс x1.28) "тёмных" строк в зоне десятки, и
# read_panel перебирал 270 пар — 16 мс на кадр, весь бюджет тика. С подсказкой сначала проверяем
# строки у ожидаемого места рамки — там она и находится с первых же попыток.
EXPECT_MAX_CHECKS = 20


class _Budget(Exception):
    pass


def read_panel(frame: np.ndarray, scale: float = 1.0, expect_y: "int | None" = None) -> PanelResult:
    """read_panel для интерфейса в scale раз крупнее эталона (1.0 — как всегда). См. scaled_template.
    expect_y — где в кадре ожидается верхняя линия рамки: пары проверяются от неё, не больше
    EXPECT_MAX_CHECKS (быстрый путь для зоны панели, где рамка стоит на известном месте)."""
    return _with_scale(_read_panel, scale, frame, expect_y)


def _read_panel(frame: np.ndarray, expect_y: "int | None" = None) -> PanelResult:
    if expect_y is None:
        return _read_panel_impl(frame, None)
    try:
        return _read_panel_impl(frame, expect_y)
    except _Budget:
        return _fail("рамка не у ожидаемого места (проверено %d пар)" % EXPECT_MAX_CHECKS)


def _read_panel_impl(frame: np.ndarray, expect_y: "int | None") -> PanelResult:
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
    mx = frame.max(axis=2)
    dark = mx <= T.FRAME_DARK_MAX
    need = T.FRAME_SPAN_MIN * T.FRAME_LINE_FILL_MIN
    cand_rows = np.flatnonzero(np.count_nonzero(dark, axis=1) >= need)
    # Красная полоска (2026-10-06, "Дворф-рубака" 2118/13957): у части мобов потерянное HP
    # закрашено КРАСНЫМ, и рамка поверх него — (25, 23, 104): по max-каналу не тёмная, а по
    # B и G — тёмная. Такие строки ищем по max(B, G); пара из них проходит все остальные
    # проверки (зазор 11 px, правый край, бежевая окантовка, кнопка X) — они вне красного.
    bg = np.maximum(frame[..., 0], frame[..., 1])
    dark_bg = bg <= T.FRAME_LINE_DIM_MAX
    red_rows = np.flatnonzero(np.count_nonzero(dark_bg, axis=1) >= need)
    if cand_rows.size == 0 and red_rows.size < 2:
        return _fail("нет чёрных линий рамки")
    # "Тусклые" строки (2026-10-05 21:29): поверх панели легли полупрозрачные таблички других
    # игроков ("Wooly", "DAD") — нижняя линия рамки под ними 25-40 вместо 11, и чёрных
    # пикселей в ней 98 из нужных 99. Такая строка годится в пару, только если вторая линия —
    # настоящая чёрная; остальные проверки (зазор 11 px, правая граница, окантовка, кнопка X)
    # те же, поэтому трава/тень панелью от этого не станут.
    dim = mx <= T.FRAME_LINE_DIM_MAX
    dim_rows = np.flatnonzero(np.count_nonzero(dim, axis=1) >= need)
    strict = set(cand_rows.tolist())
    check = _check_candidate
    if expect_y is not None:
        # Строки — от ожидаемого места рамки (argsort по расстоянию: векторно, без цикла Python),
        # и не больше EXPECT_MAX_CHECKS проверок на кадр.
        def _near(rows):
            return rows[np.argsort(np.abs(rows - expect_y), kind="stable")] if rows.size else rows
        cand_rows, dim_rows, red_rows = _near(cand_rows), _near(dim_rows), _near(red_rows)
        left = [EXPECT_MAX_CHECKS]

        def check(*a, **k):
            left[0] -= 1
            if left[0] < 0:
                raise _Budget()
            return _check_candidate(*a, **k)

    gap_lo = T.FRAME_LINE_GAP - T.FRAME_LINE_GAP_TOL
    gap_hi = T.FRAME_LINE_GAP + T.FRAME_LINE_GAP_TOL

    def pairs(top_rows, bottom_rows, mixed):
        for yt in top_rows:
            ybs = bottom_rows[(bottom_rows >= yt + gap_lo) & (bottom_rows <= yt + gap_hi)]
            # Сначала пара ровно на FRAME_LINE_GAP, потом соседние: при низком HP пустая
            # часть полоски тёмная, и строка ВНУТРИ рамки тоже проходит как "чёрная линия".
            for yb in sorted(ybs.tolist(), key=lambda v: abs(v - yt - T.FRAME_LINE_GAP)):
                yt_i, yb_i = int(yt), int(yb)
                if mixed and ((yt_i in strict) == (yb_i in strict)):
                    continue                     # обе чёрные — уже проверены; обе тусклые — не берём
                yield yt_i, yb_i

    last_reason = "нет пары линий на расстоянии %d px" % T.FRAME_LINE_GAP
    # Сначала как раньше (обе линии чёрные), и только потом пары "чёрная + тусклая".
    for mixed, tops, bottoms in ((False, cand_rows, cand_rows), (True, dim_rows, dim_rows)):
        if tops.size == 0:
            continue
        for yt, yb in pairs(tops, bottoms, mixed):
            if not mixed:
                res = check(frame, dark[yt], dark[yb], yt, yb, w, h)
            else:
                # Длину и концы рамки меряем по ЧЁРНОЙ линии; тусклая должна покрыть её почти
                # всю (FRAME_DIM_COVER_MIN), но может прерываться: между табличками "Wooly" и
                # "DAD" в линии светлый просвет 8 px.
                main, other = (dark[yt], dim[yb]) if yt in strict else (dark[yb], dim[yt])
                res = check(frame, main, other, yt, yb, w, h, other_cover_min=T.FRAME_DIM_COVER_MIN)
            if res.ok:
                return res
            last_reason = res.reason
    # Третий проход — красная полоска (см. red_rows выше): обе линии тёмные по B, G.
    for yt, yb in pairs(red_rows, red_rows, False):
        if yt in strict and yb in strict:
            continue                             # обычная чёрная пара — уже проверена
        res = check(frame, dark_bg[yt], dark_bg[yb], yt, yb, w, h, edge_bg=True)
        if res.ok:
            return res
        last_reason = res.reason
    # Четвёртый проход — концы рамки по бежевой окантовке (лог 2026-10-06 01:48, "Дворф-рубака"
    # 2128/13957 в тёмной пещере): ночной фон тоже "тёмный", линии рамки сливаются с ним и
    # тянутся за торцы (178-264 px вместо ~142) — длина не проходит. Окантовка НАД верхней
    # линией бежевая, а фон бежевым не бывает: линии меряем только в её пределах.
    beige = _beige_mask(frame)
    red_set = set(red_rows.tolist())
    for yt in red_rows.tolist():
        if yt < T.BEIGE_ROWS:
            continue
        top_band = beige[yt - T.BEIGE_ROWS:yt].any(axis=0)
        if np.count_nonzero(top_band) < T.FRAME_SPAN_MIN - 10:
            continue                             # над строкой нет окантовки — дальше не считаем
        for yb in (yt + T.FRAME_LINE_GAP, yt + T.FRAME_LINE_GAP - 1, yt + T.FRAME_LINE_GAP + 1):
            if yb not in red_set or yb + T.BEIGE_ROWS >= h:
                continue
            # Окантовка должна быть И сверху, И снизу: сверху справа к ней примыкает рамка
            # кнопки X (на кадре 01:48 бежевое до x=205 при торце рамки 189), снизу — нет.
            band = top_band & beige[yb + 1:yb + 1 + T.BEIGE_ROWS].any(axis=0)
            grp = _longest_group(np.flatnonzero(band), T.FRAME_LINE_MAX_GAP)
            if grp.size < T.FRAME_SPAN_MIN - 10:
                continue
            span = np.zeros(w, dtype=bool)
            span[max(0, int(grp[0]) - 1):min(w, int(grp[-1]) + 1)] = True
            res = check(frame, dark_bg[yt] & span, dark_bg[yb] & span, yt, int(yb), w, h, edge_bg=True)
            if res.ok:
                return res
            last_reason = res.reason
    return _fail(last_reason)


def _check_candidate(frame: np.ndarray, line_a: np.ndarray, line_b: np.ndarray,
                     yt: int, yb: int, w: int, h: int,
                     other_cover_min: "float | None" = None, edge_bg: bool = False) -> PanelResult:
    """
    Проверяет ОДНУ пару чёрных линий (yt — верхняя, yb — нижняя). line_a/line_b — маски
    тёмного этих строк. other_cover_min=None: общий участок обеих линий (как всегда).
    Иначе line_a — настоящая чёрная (по ней концы рамки), line_b — тусклая, ей достаточно
    покрыть other_cover_min длины (см. read_panel, таблички других игроков поверх панели).
    """
    both = line_a & line_b if other_cover_min is None else line_a
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
    if other_cover_min is not None and line_b[x_left:x_right + 1].mean() < other_cover_min:
        return _fail("вторая линия рамки закрыта больше чем на %d%%" % round((1 - other_cover_min) * 100))

    # --- 2) Правая вертикальная граница рамки ----------------------------
    # Ищем самый тёмный столбец в 0..FRAME_EDGE_SEARCH_PX правее конца линий: у новой
    # панели (2026-10-04) граница на 1 px дальше и светлее порога линий (29-34 против 24).
    edge_best = 0.0
    for c in range(x_right, min(w, x_right + T.FRAME_EDGE_SEARCH_PX + 1)):
        px = frame[yt:yb + 1, c]
        col = (px[:, :2].max(axis=1) if edge_bg else px.max(axis=1)) <= T.FRAME_EDGE_DARK_MAX
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
    bottom_frac = _beige_band_fraction(bottom)
    # Низ частично закрыт строкой "цель цели" (имя другого игрока) — допускаем, но тогда
    # кнопка X обязана найтись (см. BEIGE_BOTTOM_FRACTION_MIN).
    weak_bottom = bottom_frac < T.BEIGE_FRACTION_MIN
    if bottom_frac < getattr(T, "BEIGE_BOTTOM_FRACTION_MIN", T.BEIGE_FRACTION_MIN):
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
    elif weak_bottom:
        return _fail("нет бежевой окантовки снизу")    # низ закрыт, а кнопку X не проверить — не рискуем

    # --- 5) Всё совпало: читаем HP ----------------------------------------
    fx0 = x_left + T.FILL_LEFT_PAD
    fx1 = x_right - T.FILL_RIGHT_PAD + 1          # срез правая граница не включает
    interior = frame[yt + 1: yb, fx0:fx1]
    # Один HSV-проход на два чтения: длина заливки (HP в процентах) и текст HP.
    hsv = cv2.cvtColor(interior, cv2.COLOR_BGR2HSV)
    hp = _hp_from_fill(_fill_columns(interior, hsv))
    foreign = player = False
    if hp <= 0.0:
        # Ни жёлтой, ни красной заливки слева: либо моб мёртв (пустая полоска), либо полоска
        # СЕРАЯ — моб чужой (его бьёт другой игрок), либо СВЕТЛАЯ — это вообще игрок.
        p_hp = _hp_from_fill(_player_fill_columns(hsv))
        if p_hp >= _PLAYER_MIN_FILL:
            hp, player = p_hp, True
        else:
            g_hp = _hp_from_fill(_gray_fill_columns(hsv))
            if g_hp > 0.0:
                hp, foreign = g_hp, True
    return PanelResult(
        ok=True, reason=("ok (игрок)" if player else "ok (серая полоска — чужой моб)" if foreign else "ok"),
        hp_percent=hp,
        bar=(fx0, yt + 1, fx1 - fx0, yb - yt - 1),
        plate_checked=plate_checked,
        text=hp_text.extract(hsv),
        foreign=foreign,
        player=player,
    )


def locate_panel(frame: np.ndarray, roi_w: int = 190, roi_h: int = 90,
                 scale: float = 1.0) -> "tuple[int, int] | None":
    """locate_panel с масштабом интерфейса: roi_w/roi_h — уже в пикселях ЭТОГО экрана."""
    return _with_scale(_locate_panel, scale, frame, roi_w, roi_h)


def _locate_panel(frame: np.ndarray, roi_w: int = 190, roi_h: int = 90) -> "tuple[int, int] | None":
    """
    Где на ВСЁМ кадре панель цели: (left, top) зоны roi_w x roi_h, в которой рамка HP лежит
    ровно как в настроенной зоне (PANEL_LINE_*_IN_ROI), или None — панели нет.

    Зачем (скрин 2026-10-05 21:40): игрок перетащил панель цели в левый верхний угол — в
    старой зоне её нет, бот не видел выбранного моба и не начинал бой. Теперь панель ищется
    сама, как список целей.

    Как: тёмные (<= FRAME_DARK_MAX) горизонтальные отрезки длиной с рамку, над которыми не
    темно (верхняя линия: над ней бежевая окантовка) или под которыми не темно (нижняя) —
    морфология OpenCV (closing склеивает насечку в линии, opening оставляет только длинные
    отрезки), ~5-10 мс на 1920x1080. Каждого кандидата проверяет сам read_panel на вырезке
    зоны — со всеми строгими проверками (зазор 11 px, правый край, окантовка, кнопка X),
    так что трава/тени/другие полоски HUD панелью не станут.
    Как проверить без игры: locate_panel(cv2.imread("полный_скриншот.png")).
    """
    if frame is None or frame.ndim != 3 or frame.shape[0] < roi_h or frame.shape[1] < roi_w:
        return None
    b, g, r = cv2.split(np.ascontiguousarray(frame[..., :3]))
    mx = cv2.max(cv2.max(b, g), r)
    dark = (mx <= T.FRAME_DARK_MAX).astype(np.uint8)
    h, w = dark.shape
    edge_top = np.zeros_like(dark)
    edge_top[2:] = dark[2:] & (1 - dark[:-2])           # верхняя линия: двумя строками выше — не темно
    edge_bot = np.zeros_like(dark)
    edge_bot[:-2] = dark[:-2] & (1 - dark[2:])          # нижняя: двумя строками ниже — не темно
    close_k = cv2.getStructuringElement(cv2.MORPH_RECT, (T.FRAME_LINE_MAX_GAP + 1, 1))
    open_k = cv2.getStructuringElement(cv2.MORPH_RECT, (T.FRAME_SPAN_MIN - 10, 1))
    lx, ly = T.PANEL_LINE_X_IN_ROI, T.PANEL_LINE_Y_IN_ROI
    pad = max(roi_w, roi_h)
    padded = None
    seen = set()
    checked = 0
    for mask, dy in ((edge_top, 0), (edge_bot, -T.FRAME_LINE_GAP)):
        runs = cv2.morphologyEx(cv2.morphologyEx(mask, cv2.MORPH_CLOSE, close_k), cv2.MORPH_OPEN, open_k)
        n, _, stats, _ = cv2.connectedComponentsWithStats(runs, connectivity=8)
        comps = [stats[i] for i in range(1, n)
                 if T.FRAME_SPAN_MIN - 10 <= stats[i][2] <= T.FRAME_SPAN_MAX + 6 and stats[i][3] <= 3]
        for x, y, cw, ch, _ in comps:
            yt = int(y) + dy
            key = (int(x) // 4, yt // 4)
            if key in seen:
                continue
            seen.add(key)
            checked += 1
            if checked > T.LOCATE_MAX_CANDIDATES:
                return None
            left, top = int(x) - lx, yt - ly
            if padded is None:                          # панель у края экрана: зона может выйти за него
                padded = cv2.copyMakeBorder(frame[..., :3], pad, pad, pad, pad, cv2.BORDER_REPLICATE)
            crop = padded[top + pad:top + pad + roi_h, left + pad:left + pad + roi_w]
            res = read_panel(crop)
            if not res.ok:
                continue
            # Подгоняем зону так, чтобы найденная рамка легла ровно в (lx, ly).
            fx = res.bar[0] - T.FILL_LEFT_PAD - lx
            fy = res.bar[1] - 1 - ly
            return left + fx, top + fy
    return None


# --- Панель под цветной вспышкой экрана (лог 2026-10-05 23:01:12) -------------------------
# Игра заливает экран красным (урон по персонажу и т.п.): чёрные линии рамки становятся
# (34, 31, 139) — по max-каналу уже не "чёрные", бежевая окантовка и кнопка X тоже уезжают.
# read_panel честно говорит "панели нет", бот через 0.45 с решал "убита" и бросал моба с 11% HP.
# Вспышка красит в основном R: каналы B и G у рамки остаются тёмными. Поэтому, если панель
# только что была на этом месте, проверяем ТЕ ЖЕ две строки рамки по B,G — без цветовых
# проверок (их и не пройти под вспышкой). Место известно точно, так что трава панелью не станет.
TINT_LINE_BG_MAX = 50          # рамка под вспышкой: max(B, G) <= этого
TINT_LINE_FILL_MIN = 0.85      # доля тёмных пикселей обеих линий по длине полоски
TINT_YELLOW_G_MIN = 100        # заливка HP: G высокий...
TINT_YELLOW_GB_MIN = 40        # ...и заметно выше B (розовый "след урона" — B тоже высокий)
TINT_RED_EXCESS_MIN = 15       # линии рамки под вспышкой: R выше max(B,G) в среднем хотя бы на столько


def read_panel_tinted(frame: np.ndarray, bar: "tuple[int, int, int, int]", scale: float = 1.0) -> PanelResult:
    """read_panel_tinted с масштабом интерфейса (см. read_panel)."""
    return _with_scale(_read_panel_tinted, scale, frame, bar)


def _read_panel_tinted(frame: np.ndarray, bar: "tuple[int, int, int, int]") -> PanelResult:
    """
    Панель там же, где была (bar = PanelResult.bar прошлой находки), но под цветной вспышкой.
    HP — по самому правому столбцу жёлтой заливки (текст HP посередине белый, его пропускаем).
    Как проверить без игры: read_panel_tinted(cv2.imread("кадр_со_вспышкой.png"), (28, 49, 139, 10)).
    """
    if frame is None or frame.ndim != 3:
        return _fail("нет кадра")
    x, y, w, h = bar
    yt, yb = y - 1, y + h
    if yt < 0 or yb >= frame.shape[0] or x < 0 or x + w > frame.shape[1] or w < 20:
        return _fail("рамка вне кадра")
    f = frame[..., :3].astype(np.int16)
    bg = np.maximum(f[..., 0], f[..., 1])
    if (bg[yt, x:x + w] <= TINT_LINE_BG_MAX).mean() < TINT_LINE_FILL_MIN or \
            (bg[yb, x:x + w] <= TINT_LINE_BG_MAX).mean() < TINT_LINE_FILL_MIN:
        return _fail("под вспышкой: нет рамки на прежнем месте")
    # Это правда вспышка: линии рамки "красно-тёмные" (R заметно выше B,G). Тёмный фон ночью
    # (R ~ G ~ B) на месте исчезнувшей панели так не выглядит — панелью его не посчитаем.
    lines = np.concatenate([f[yt, x:x + w], f[yb, x:x + w]])
    if float(np.mean(lines[:, 2] - np.maximum(lines[:, 0], lines[:, 1]))) < TINT_RED_EXCESS_MIN:
        return _fail("под вспышкой: рамка не красная — вспышки нет")
    inner = f[y + 1:y + h - 1, x:x + w]
    b, g = inner[..., 0].mean(axis=0), inner[..., 1].mean(axis=0)
    yellow = (g >= TINT_YELLOW_G_MIN) & (g - b >= TINT_YELLOW_GB_MIN)
    run, last = 0, -1
    for i in range(w - 1, -1, -1):              # справа налево: первая "настоящая" жёлтая (3 столбца подряд)
        run = run + 1 if yellow[i] else 0
        if run >= 3:
            last = i + 2
            break
    hp = 0.0 if last < 0 else round(min(100.0, (last + 1) * 100.0 / w), 2)
    return PanelResult(ok=True, reason="ok (под вспышкой)", hp_percent=hp, bar=bar, plate_checked=False, text=None)
