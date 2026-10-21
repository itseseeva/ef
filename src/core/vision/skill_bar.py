"""
Панель скиллов (хотбар) внизу экрана: где она и как сейчас выглядит каждый слот.

Задача модуля одна — ЗРЕНИЕ: найти панель и раз в ~65 мс снимать "яркость" каждого слота.
Решение "скилл нажался или нет" — в src/core/skill_check.py (чистая логика, без экрана).

Как узнаём откат (2026-10-06, скрин игрока): у скилла на откате иконка темнеет, поверх —
белые цифры ("5 сек."). Цифры НЕ читаем (OCR медленный и путается на мелком шрифте) —
нам нужен только факт "откат начался", а для этого хватает средней яркости слота (V из HSV)
и числа белых пикселей, сравнённых с тем же слотом ДО нажатия.

Где панель: шаблон строки подписей клавиш "1 2 3 ... =" (src/config/skill_bar_labels.png).
Подписи не меняются ни от отката, ни от фона — в отличие от самих иконок. Ищем один раз
(и повторно, если потеряли) по всему монитору, в нескольких масштабах: интерфейс игры
масштабируется с разрешением, координаты слотов храним в долях шаблона, а не в пикселях экрана.

Как проверить без игры:
    from src.core.vision.skill_bar import locate_bar, slot_stats
    import cv2; scr = cv2.imread("полный_скриншот.png")
    bar = locate_bar(scr); print(bar)                    # BarGeometry или None
    print(slot_stats(scr, bar))                          # {'1': (V, белых px), ...}
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass

import cv2
import numpy as np

logger = logging.getLogger(__name__)

_TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "config", "skill_bar_labels.png")
# Снимки "скилл готов" (2026-10-10): делает tools/capture_ready_slots.py, бот только читает.
SNAP_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "config", "skill_ready"))

# Иконки слотов относительно левого верхнего угла шаблона подписей, px при масштабе 1.0
# (замерено по скрину 2026-10-06: шаг слотов 51 px, разрыв на месте слота Y). Берём
# внутреннюю часть иконки с отступом 4 px: рамка слота и фиолетовая подсветка "в очереди"
# не должны менять яркость. Кортеж: (x0, y0, ширина, высота).
_SLOT_W, _SLOT_H = 37, 34
_BOTTOM_X = {"1": 15, "2": 66, "3": 117, "4": 168, "5": 219, "6": 270,
             "7": 372, "8": 423, "9": 474, "0": 525, "-": 576, "=": 627}
_BOTTOM_Y = -38
# Верхний ряд: Q и C (E — клавиша атаки по цели, её бот жмёт сам и не проверяет).
_TOP_X = {"q": 0, "c": 99}
_TOP_Y = -95
_TOP_W, _TOP_H = 38, 32

SLOTS: "dict[str, tuple[int, int, int, int]]" = {
    **{k: (x, _BOTTOM_Y, _SLOT_W, _SLOT_H) for k, x in _BOTTOM_X.items()},
    **{k: (x, _TOP_Y, _TOP_W, _TOP_H) for k, x in _TOP_X.items()},
}

LOCATE_MIN_SCORE = 0.55                    # TM_CCOEFF_NORMED: замер 2026-10-06 — наша панель 0.60-0.85 (и с 2 переназначенными клавишами), чужая/нет панели <= 0.33
LOCATE_SCALES = (1.0, 0.9, 1.1, 0.8, 1.2, 1.25, 1.33, 0.75, 1.5)   # сначала самые вероятные
WHITE_MIN = 200                            # белый пиксель цифр отката: все каналы >= этого


@dataclass(frozen=True)
class BarGeometry:
    """Где панель на кадре: левый верх шаблона подписей (px кадра) и масштаб интерфейса."""
    x: int
    y: int
    scale: float
    score: float

    def slot_rect(self, key: str) -> "tuple[int, int, int, int]":
        x0, y0, w, h = SLOTS[key]
        s = self.scale
        return (self.x + round(x0 * s), self.y + round(y0 * s), max(4, round(w * s)), max(4, round(h * s)))

    def bounds(self) -> "tuple[int, int, int, int]":
        """Рамка, которая покрывает все слоты: (x0, y0, x1, y1) в px кадра."""
        rects = [self.slot_rect(k) for k in SLOTS]
        return (min(r[0] for r in rects), min(r[1] for r in rects),
                max(r[0] + r[2] for r in rects), max(r[1] + r[3] for r in rects))

    # Общий интерфейс с LayoutGeometry: какие слоты есть, как подписаны, в каком ряду.
    @property
    def keys(self) -> "tuple[str, ...]":
        return tuple(SLOTS)

    def label(self, key: str) -> str:
        return key

    def row(self, key: str) -> str:
        return "top" if key in _TOP_X else "bottom"


class LayoutGeometry:
    """
    Панель, найденная по форме (vision/bar_layout.py), с тем же интерфейсом, что BarGeometry:
    slot_rect / bounds / keys / scale. Ключи — имена клавиш ввода ("1", "x", "minus"), а не позиции:
    раскладка и бинды у каждого аккаунта свои. Координаты — экранные.
    """

    def __init__(self, layout) -> None:
        self.layout = layout
        x0, y0, _, _ = layout.bounds()
        self.x, self.y = x0, y0
        self.scale = layout.scale
        self.score = 1.0

    @property
    def keys(self) -> "tuple[str, ...]":
        return tuple(self.layout.rects)

    def slot_rect(self, key: str) -> "tuple[int, int, int, int]":
        return self.layout.rects[key]

    def bounds(self) -> "tuple[int, int, int, int]":
        return self.layout.bounds()

    def label(self, key: str) -> str:
        return self.layout.labels.get(key, key)

    def row(self, key: str) -> str:
        return self.layout.rows.get(key, "bottom")

    def __repr__(self) -> str:
        return "LayoutGeometry(%s, масштаб %.2f)" % (" ".join(self.label(k) for k in self.keys), self.scale)


_template_gray: "np.ndarray | None" = None


def _template() -> "np.ndarray | None":
    """Шаблон грузится один раз (ленивая инициализация): диск не трогаем на каждом поиске."""
    global _template_gray
    if _template_gray is None:
        img = cv2.imread(os.path.normpath(_TEMPLATE_PATH))
        if img is None:
            logger.error("skill_bar: нет шаблона %s", _TEMPLATE_PATH)
            return None
        _template_gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return _template_gray


def locate_bar(frame: np.ndarray) -> "BarGeometry | None":
    """
    Найти панель скиллов на ВСЁМ кадре монитора (BGR). Ищем только в нижней половине —
    хотбар всегда внизу, а поиск по вдвое меньшей площади вдвое быстрее (~30-80 мс на
    масштаб для 1920x1080). Первый масштаб с хорошим совпадением — ответ: дальше не ищем.
    Зовётся редко (старт и потеря панели), и всегда из фонового потока.
    """
    tpl = _template()
    if tpl is None or frame is None or frame.ndim != 3:
        return None
    h = frame.shape[0]
    y_off = h // 2
    gray = cv2.cvtColor(np.ascontiguousarray(frame[y_off:, :, :3]), cv2.COLOR_BGR2GRAY)
    best: "BarGeometry | None" = None
    for s in LOCATE_SCALES:
        t = tpl if s == 1.0 else cv2.resize(tpl, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
        if t.shape[0] > gray.shape[0] or t.shape[1] > gray.shape[1]:
            continue
        res = cv2.matchTemplate(gray, t, cv2.TM_CCOEFF_NORMED)
        _, score, _, loc = cv2.minMaxLoc(res)
        if best is None or score > best.score:
            best = BarGeometry(int(loc[0]), int(loc[1]) + y_off, s, float(score))
        if score >= 0.85:
            break                                # уверенное совпадение — остальные масштабы не нужны
    if best is None or best.score < LOCATE_MIN_SCORE:
        return None
    x0, y0, x1, y1 = best.bounds()
    if x0 < 0 or y0 < 0 or x1 > frame.shape[1] or y1 > frame.shape[0]:
        return None                              # слоты вышли бы за кадр — совпадение ложное
    return best


def slot_stats(frame: np.ndarray, bar: BarGeometry, ox: int = 0, oy: int = 0) -> "dict[str, tuple[float, int]]":
    """
    {клавиша: (средняя яркость V 0..255, белых пикселей)} для всех слотов.
    ox, oy — где на экране левый верх frame (кадр может быть вырезкой только панели).
    Яркость = max(B, G, R) — это и есть V в HSV, но без cvtColor на весь кусок: быстрее.
    """
    out: "dict[str, tuple[float, int]]" = {}
    for key in bar.keys:
        x, y, w, h = bar.slot_rect(key)
        roi = frame[y - oy:y - oy + h, x - ox:x - ox + w, :3]
        if roi.size == 0:
            continue
        mx = roi.max(axis=2)
        white = int(np.count_nonzero(roi.min(axis=2) >= WHITE_MIN))
        out[key] = (float(mx.mean()), white)
    return out


# ---------------- снимки "скилл готов" ----------------
# Зачем (2026-10-10): раньше бот УЧИЛ "готовую" яркость слота в бою, по своим же нажатиям — пока
# не выучил, слот для него "неизвестен", и выученное могла испортить вспышка нажатия. Теперь
# готовый вид каждого слота снимается ЗАРАНЕЕ (tools/capture_ready_slots.py, вне боя, все скиллы
# готовы) и бот с первой секунды сравнивает слот со снимком. Сменил скилл — перезапусти скрипт.
#
# Сравниваем по двум числам, у каждого своя работа:
#   ratio — яркость сейчас / яркость на снимке. Откат ТЕМНИТ иконку (замер: 0.47-0.52), готовая — 1.0;
#   ncc   — нормированная корреляция серых картинок (TM_CCOEFF_NORMED, -1..1): "та же ли это картинка".
#           Она не замечает равномерного затемнения (по определению нечувствительна к яркости), зато
#           падает от цифр отката (замер 0.55) и от чужой иконки (другие иконки панели: 0.2, максимум 0.63).
# Вместе: тёмный -> откат; яркий и похож -> готов; яркий и не похож -> скилл сменили (снимок устарел).
_SNAP_NAME = {"-": "minus", "=": "equal"}     # имена файлов без знаков, которые путают людей и шеллы


def snap_filename(key: str) -> str:
    return "slot_%s.png" % _SNAP_NAME.get(key, key)


def _norm_gray(bgr: np.ndarray) -> "tuple[np.ndarray, float]":
    """Серая картинка -> (вектор с нулевым средним и единичной длиной, длина до нормировки).
    Так ncc двух картинок = одно скалярное произведение (np.dot) — без matchTemplate на каждый кадр."""
    g = cv2.cvtColor(np.ascontiguousarray(bgr[..., :3]), cv2.COLOR_BGR2GRAY).astype(np.float32).ravel()
    g -= g.mean()
    n = float(np.linalg.norm(g))
    return (g / n if n > 1e-6 else g), n


class ReadySnapshots:
    """
    Снимки готовых слотов: {подпись слота: картинка BGR при масштабе scale}. Подготовка (серый
    нормированный вектор) — один раз (кэш по масштабу), сравнение на кадр — ~50 мкс на слот.

    Как проверить без игры: skill_snap_test.py (снимки режутся из скриншота, сравниваются с другим).
    """

    def __init__(self, crops: "dict[str, np.ndarray]", scale: float = 1.0, meta: "dict | None" = None) -> None:
        self.crops = dict(crops)
        self.scale = float(scale)
        self.meta = meta or {}
        self._prep_scale: "tuple | None" = None
        self._prep: "dict[str, tuple[np.ndarray, float, tuple[int, int]]]" = {}

    # --- диск ---
    @classmethod
    def load(cls, folder: str = SNAP_DIR) -> "ReadySnapshots | None":
        try:
            with open(os.path.join(folder, "meta.json"), encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            return None                              # снимков нет — бот работает по-старому (яркость)
        crops = {}
        for key in meta.get("slots") or list(SLOTS):   # снимки по раскладке: ключи — клавиши ("x", "minus")
            img = cv2.imread(os.path.join(folder, snap_filename(key)))
            if img is not None:
                crops[key] = img
        return cls(crops, float(meta.get("scale", 1.0)), meta) if crops else None

    def save(self, folder: str = SNAP_DIR) -> None:
        os.makedirs(folder, exist_ok=True)
        for key, img in self.crops.items():
            cv2.imwrite(os.path.join(folder, snap_filename(key)), img)
        meta = dict(self.meta, scale=self.scale, slots=sorted(self.crops))
        with open(os.path.join(folder, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=1)

    # --- сравнение ---
    def _prepared(self, bar: BarGeometry) -> "dict[str, tuple[np.ndarray, float, tuple[int, int]]]":
        """Снимки под текущий масштаб: (норм. вектор, яркость V, (w, h)). Пересчёт только при смене масштаба."""
        if self._prep_scale == (id(bar), bar.scale):
            return self._prep
        prep = {}
        if abs(bar.scale - self.scale) > 0.01:
            # Масштаб интерфейса другой, чем при снимке. Подогнать снимок ресайзом можно, но мелкие
            # детали иконок сдвигаются на полпикселя, и похожесть готового слота падает до 0.65-0.8
            # (замер на скрине x0.8-x1.33) — на грани "откат". Размытие это лечит, но тогда и
            # ЧУЖИЕ иконки становятся похожи (до 0.81-0.89). Честнее не гадать: снимки на этот масштаб
            # не используем (бот по яркости, как раньше) и просим переснять — это 10 секунд.
            logger.warning("SkillBar: масштаб панели %.2f, а снимки сняты при %.2f — снимки не использую, "
                           "перезапусти tools/capture_ready_slots.py", bar.scale, self.scale)
        else:
            keys = set(bar.keys)
            for key, img in self.crops.items():
                _, _, w, h = bar.slot_rect(key) if key in keys else (0, 0, -1, -1)
                if img.shape[1] != w or img.shape[0] != h:
                    continue                         # не тот размер (чужой файл/старая разметка) — слот без снимка
                vec, _ = _norm_gray(img)
                prep[key] = (vec, float(img[..., :3].max(axis=2).mean()), (w, h))
        self._prep, self._prep_scale = prep, (id(bar), bar.scale)
        return prep

    def scores(self, frame: np.ndarray, bar: BarGeometry, ox: int = 0, oy: int = 0) -> "dict[str, tuple[float, float]]":
        """{слот: (ncc со снимком, яркость сейчас / яркость снимка)} для слотов, у которых есть снимок."""
        out: "dict[str, tuple[float, float]]" = {}
        for key, (vec, v_snap, (w, h)) in self._prepared(bar).items():
            x, y, _, _ = bar.slot_rect(key)
            roi = frame[y - oy:y - oy + h, x - ox:x - ox + w, :3]
            if roi.shape[0] != h or roi.shape[1] != w:
                continue                             # слот за краем вырезки — не судим
            cur, n = _norm_gray(roi)
            ncc = float(np.dot(cur, vec)) if n > 1e-6 else 0.0   # однотонный слот: корреляции нет
            out[key] = (ncc, float(roi.max(axis=2).mean()) / max(1.0, v_snap))
        return out


class SkillBarWatcher:
    """
    Фоновый поток: находит панель и раз в SAMPLE_PERIOD_S снимает слоты в кольцевой буфер.
    Свой экземпляр mss (mss не потокобезопасен, см. WorldWorker). Поток бота только читает
    буфер (samples_since) — захват экрана не стоит в его 60 FPS цикле.
    Почему поток, а не asyncio: mss.grab и numpy — блокирующие вызовы без await; в отдельном
    потоке они не держат главный цикл, а GIL numpy отпускает на время счёта.

    Где панель (2026-10-11), по порядку:
      1. раскладка из снимка скиллов (meta.json -> "layout"): клавиши уже прочитаны, искать нечего;
      2. старый поиск по шаблону подписей "1 2 3 ... =" (мой первый аккаунт);
      3. поиск по форме (bar_layout.detect_layout) — любой аккаунт, любые бинды.
    Снимков для этой панели нет (первый запуск у нового человека / сменил бинды) — один раз за
    "Старт" снимаем их сами (AUTO_CAPTURE): 4 с смотрим на панель и сохраняем, как кнопка "Снять скиллы".
    """
    SAMPLE_PERIOD_S = 0.065                 # ~15 Гц: откат виден за 1-2 кадра, нагрузка ~1-2 мс/кадр
    RELOCATE_PERIOD_S = 5.0                 # панели нет — искать снова не чаще этого
    RECHECK_PERIOD_S = 3.0                  # раз в столько сверяем подписи: панель на месте?
    LOST_AFTER_MISSES = 2                   # столько сверок подряд мимо -> панель потеряна, искать заново
    AUTO_CAPTURE = True                     # нет снимков для этой панели -> снять самим при старте

    def __init__(self, monitor: dict, clock=time.monotonic) -> None:
        self._mon = dict(monitor)
        self._clock = clock
        self.bar = None                     # BarGeometry | LayoutGeometry | None
        self._buf: "deque[tuple[float, dict]]" = deque(maxlen=64)
        self._stop = threading.Event()
        self._thread: "threading.Thread | None" = None
        self._next_locate = 0.0
        self._next_recheck = 0.0
        self._misses = 0
        self.located_logged = False
        self.snaps: "ReadySnapshots | None" = None
        self._saved_layout = None            # раскладка из снимка (экранные координаты)
        self._auto_tried = False
        self.on_event = None                 # колбэк (текст) — бот пишет в свой лог
        # Последнее сравнение со снимками: (время кадра, {слот: (ncc, ratio)}). Пишет поток панели,
        # читает поток бота; кортеж заменяется целиком одной операцией — под GIL это атомарно, лок не нужен.
        self.snap_scores: "tuple[float, dict] | None" = None

    def _say(self, msg: str) -> None:
        logger.info("SkillBar: %s", msg)
        cb = self.on_event
        if cb is not None:
            try:
                cb(msg)
            except Exception:
                pass

    def load_snapshots(self) -> None:
        """Перечитать снимки с диска: зовётся на каждом "Старт" — переснял скиллы, нажал Старт, готово."""
        try:
            self.snaps = ReadySnapshots.load()
        except Exception as e:                       # битый файл не должен ронять бота
            logger.error("SkillBar: снимки готовых скиллов не прочитались: %s", e)
            self.snaps = None
        self.snap_scores = None
        self._saved_layout = None
        if self.snaps is None:
            logger.info("SkillBar: снимков готовых скиллов нет — сниму сам, когда найду панель")
            return
        from src.core.vision.bar_layout import layout_from_meta
        lay = layout_from_meta(self.snaps.meta)
        if lay is not None:
            # Раскладка снята в координатах кадра монитора -> экранные (как в _locate).
            self._saved_layout = lay.shifted(self._mon.get("left", 0), self._mon.get("top", 0))
            self.bar = LayoutGeometry(self._saved_layout)
            self._next_recheck = 0.0
        logger.info("SkillBar: снимки готовых скиллов: %s (сняты %s)",
                    " ".join(self.snaps.meta.get("labels_order") or sorted(self.snaps.crops)),
                    self.snaps.meta.get("captured", "?"))

    def start(self) -> None:
        self._auto_tried = False                     # автоснимок — не больше раза за "Старт"
        self.load_snapshots()
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="SkillBar", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def samples_since(self, t: float) -> "list[tuple[float, dict]]":
        """Кадры с отметкой времени > t (копия: deque читаем без лока, list() атомарен под GIL)."""
        return [s for s in list(self._buf) if s[0] > t]

    def _run(self) -> None:
        import mss                                   # лениво: модуль (и его тесты) работают без mss
        sct = mss.mss()
        while not self._stop.is_set():
            now = self._clock()
            try:
                if self.bar is None:
                    if now >= self._next_locate:
                        self._next_locate = now + self.RELOCATE_PERIOD_S
                        self._locate(sct)
                    self._stop.wait(0.2)
                    continue
                if self._need_auto_capture():
                    self._auto_capture()
                    continue
                self._sample(sct, now)
            except Exception as e:                       # поток зрения не должен падать насовсем
                logger.error("SkillBar: %s", e)
                self.bar = None
            self._stop.wait(self.SAMPLE_PERIOD_S)

    # --- где панель ---
    def _locate(self, sct) -> None:
        if self._saved_layout is not None:
            self.bar = LayoutGeometry(self._saved_layout)
            return
        frame = np.array(sct.grab(self._mon))[..., :3]
        bar = locate_bar(frame)
        if bar is not None:
            # Переводим в координаты экрана (mss отдаёт кадр монитора с его left/top).
            self.bar = BarGeometry(bar.x + self._mon["left"], bar.y + self._mon["top"], bar.scale, bar.score)
            self.located_logged = False
            logger.info("SkillBar: панель найдена x=%d y=%d масштаб %.2f (совпадение %.2f)",
                        self.bar.x, self.bar.y, bar.scale, bar.score)
            return
        from src.core.vision.bar_layout import detect_layout
        lay = detect_layout(frame, use_ocr=False)    # в бою без OCR: только образцы табличек, быстро
        if lay is not None:
            self.bar = LayoutGeometry(lay.shifted(self._mon["left"], self._mon["top"]))
            self.located_logged = False
            self._say("панель найдена по форме: %r" % self.bar)
            return
        if not self.located_logged:
            self._say("панель скиллов не найдена — проверка отката выключена, скиллы по таймерам")
            self.located_logged = True

    def _need_auto_capture(self) -> bool:
        """Для найденной панели нет снимков (ни одного общего слота) — снять самим, раз за старт."""
        if not self.AUTO_CAPTURE or self._auto_tried or self.bar is None:
            return False
        snaps = self.snaps
        if snaps is None or not (set(snaps.crops) & set(self.bar.keys)):
            return True
        # Снимки старого формата (без раскладки — с другого аккаунта/до 2026-10-11), а панель нашлась
        # по форме: имена "1", "q" могут совпасть, но это ДРУГИЕ скиллы — переснимаем.
        return isinstance(self.bar, LayoutGeometry) and "layout" not in snaps.meta

    def _auto_capture(self) -> None:
        self._auto_tried = True
        self._say("снимков скиллов для этой панели нет — снимаю сам (4 с)...")
        from src.core.vision.ready_capture import build_snapshots, grab_frames
        frames, geom, origin = grab_frames(4.0, use_ocr=True)
        res = build_snapshots(frames, source="автоснимок при старте", bar=geom, origin=origin)
        if not res["ok"]:
            self._say("автоснимок не удался: %s" % res["message"])
            return
        bad = [x["key"] for x in res["slots"] if not x["ok"]]
        self._say("скиллы сняты автоматически: %s%s" % (" ".join(x["key"] for x in res["slots"]),
                  ("; проверь (пересними кнопкой): %s" % " ".join(bad)) if bad else ""))
        self.bar = None
        self.load_snapshots()
        if self.bar is None:
            self._next_locate = 0.0

    def _recheck(self, sct) -> None:
        """Панель всё ещё там же? Игрок мог передвинуть панель или сменить разрешение."""
        b = self.bar
        if b is None:
            return
        if isinstance(b, LayoutGeometry):
            boxes = b.layout.boxes
            if not boxes:
                return
            from src.core.vision.bar_layout import boxes_present
            xs = [v[0] for v in boxes.values()] + [v[0] + v[2] for v in boxes.values()]
            ys = [v[1] for v in boxes.values()] + [v[1] + v[3] for v in boxes.values()]
            roi = {"left": min(xs) - 2, "top": min(ys) - 2, "width": max(xs) - min(xs) + 4, "height": max(ys) - min(ys) + 4}
            gray = cv2.cvtColor(np.array(sct.grab(roi))[..., :3], cv2.COLOR_BGR2GRAY).astype(np.float32)
            score = boxes_present(gray, boxes, roi["left"], roi["top"])
            ok = score >= 0.5
        else:
            tpl = _template()
            if tpl is None:
                return
            t = tpl if b.scale == 1.0 else cv2.resize(tpl, None, fx=b.scale, fy=b.scale, interpolation=cv2.INTER_AREA)
            pad = 3                                    # +-3 px: дрожание округления масштаба
            roi = {"left": b.x - pad, "top": b.y - pad, "width": t.shape[1] + 2 * pad, "height": t.shape[0] + 2 * pad}
            gray = cv2.cvtColor(np.array(sct.grab(roi))[..., :3], cv2.COLOR_BGR2GRAY)
            score = float(cv2.minMaxLoc(cv2.matchTemplate(gray, t, cv2.TM_CCOEFF_NORMED))[1])
            ok = score >= LOCATE_MIN_SCORE - 0.1
        self._misses = 0 if ok else self._misses + 1
        if self._misses >= self.LOST_AFTER_MISSES:
            self._say("панель скиллов пропала с места (%.2f) — ищу заново" % score)
            if isinstance(b, LayoutGeometry) and b.layout is self._saved_layout:
                self._saved_layout = None            # снятая раскладка устарела — дальше поиск по форме
            self.bar, self._misses, self._next_locate = None, 0, 0.0
            self._buf.clear()

    def _sample(self, sct, now: float) -> None:
        if now >= self._next_recheck:
            self._next_recheck = now + self.RECHECK_PERIOD_S
            self._recheck(sct)
            if self.bar is None:
                return
        x0, y0, x1, y1 = self.bar.bounds()
        frame = np.array(sct.grab({"left": x0, "top": y0, "width": x1 - x0, "height": y1 - y0}))
        self._buf.append((now, slot_stats(frame, self.bar, x0, y0)))
        snaps = self.snaps
        if snaps is not None:
            self.snap_scores = (now, snaps.scores(frame, self.bar, x0, y0))
