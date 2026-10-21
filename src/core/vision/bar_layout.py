"""
Раскладка панели скиллов БЕЗ шаблона подписей (2026-10-11): где слоты и какая клавиша на каком.

Зачем. Старый поиск (skill_bar.locate_bar) искал картинку строки подписей "1 2 3 ... =" с моего
аккаунта. На другом аккаунте другие бинды (1 2 3 4, мышь, X, Ctrl, R, Z, C), другая раскладка
(нет ромба смены оружия посередине) и другой масштаб интерфейса — картинка не совпала, панель
"не найдена". Здесь панель находится по ФОРМЕ, которая у всех одинаковая, а клавиши ЧИТАЮТСЯ.

Как (всё на одном кадре, ~0.5-1 с — зовётся только при снимке скиллов, не в бою):
  1. Ряд слотов — это ПЕРИОДИЧНАЯ картинка: тёмная щель между квадратами повторяется с шагом P.
     Ищем полосу внизу экрана с самой сильной автокорреляцией (сходство строки с самой собой,
     сдвинутой на P) — так находим ряд и шаг, не зная ни масштаба, ни раскладки.
  2. "Гребёнка": подбираем шаг P (с точностью 0.1 px) и сдвиг так, чтобы как можно больше подряд
     зубьев x = сдвиг + k*P попали в тёмные щели. Щели = границы слотов.
  3. Верх слотов — где внутри клеток резко светлее, чем на щелях (контраст по строкам).
  4. Под каждым слотом — табличка с клавишей. Читаем её: сравнение с образцами табличек
     (нормированная корреляция — не зависит от яркости; образцы — src/config/key_labels.npz,
     сняты с двух разных аккаунтов), не похоже ни на что — распознавание текста (EasyOCR), если есть.
  5. Верхний ряд (Q E ...) — та же гребёнка в полосе над нижним рядом.

Масштаб интерфейса = P / 51 (51 px — шаг слотов при масштабе 1.0). Иконка слота внутри клетки
берётся с теми же отступами, что у старого поиска (сверено на скрине: расхождение <= 1 px).

Как проверить без игры: python bar_layout_test.py (скрины двух аккаунтов, старый поиск как эталон).
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field

import cv2
import numpy as np

logger = logging.getLogger(__name__)

REF_PITCH = 51.0                     # шаг слотов при масштабе интерфейса 1.0 (замер 2026-10-06)
PITCH_RANGE = (36.0, 80.0)           # масштабы ~0.7-1.55
_LABELS_PATH = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "config", "key_labels.npz"))

# Иконка внутри клетки [щель, щель+P] — доли шага (сверено со старым поиском: слот "1" 636,1009,37x34).
_ICON_X0, _ICON_W = 0.16, 0.72
_ICON_Y0, _ICON_H = 0.12, 0.67
_TOP_ICON_W, _TOP_ICON_H = 0.77, 0.65   # верхний ряд: иконки чуть шире и ниже (q: 38x32 при шаге 49.5)
TOP_ROW = False                      # верхний ряд (Q E V ...) не берём: бот жмёт только общую панель (просьба 2026-10-11)
LABEL_MIN_SCORE = 0.90               # та же надпись с другого аккаунта: 0.93-0.96; чужая (R против 8): <= 0.85
OCR_MIN_CONF = 0.6                   # уверенность EasyOCR для таблички, которой нет среди образцов
_KEY_RE = re.compile(r"^(?:[A-Z0-9]|F(?:[1-9]|1[0-2])|NUM[0-9]|-|=|CTRL|SHIFT|ALT|LMB|RMB|MMB)$", re.I)   # что бот умеет нажимать
# Кнопки мыши на панели — не табличка с буквой, а значок мышки с подсвеченной кнопкой (2026-10-11).
# Имя образца в key_labels.npz: "MOUSE_LMB" и т.д.; клавиша ввода — "lmb"; подпись для человека — "ЛКМ".
MOUSE_NAMES = {"LMB": "ЛКМ", "RMB": "ПКМ", "MMB": "Колесо"}
MOUSE_MIN_SCORE = 0.90               # тот же значок с другого снимка: 0.98-0.99; соседний значок/табличка: <= 0.77


@dataclass
class Layout:
    """Слоты по клавишам: {клавиша ввода ("1", "x", "minus", "f1"): (x, y, w, h) иконки в px кадра}."""
    rects: "dict[str, tuple[int, int, int, int]]"
    pitch: float
    rows: "dict[str, str]" = field(default_factory=dict)        # клавиша -> "top" / "bottom"
    labels: "dict[str, str]" = field(default_factory=dict)      # клавиша -> как написано на панели ("-", "Z")
    unread: int = 0                                             # слотов без читаемой клавиши (мышь, Ctrl+...)
    boxes: "dict[str, tuple[int, int, int, int]]" = field(default_factory=dict)   # таблички — "панель на месте?"

    @property
    def scale(self) -> float:
        return self.pitch / REF_PITCH

    def slot_rect(self, key: str) -> "tuple[int, int, int, int]":
        return self.rects[key]

    def bounds(self) -> "tuple[int, int, int, int]":
        r = list(self.rects.values())
        return (min(a[0] for a in r), min(a[1] for a in r),
                max(a[0] + a[2] for a in r), max(a[1] + a[3] for a in r))

    def shifted(self, dx: int, dy: int) -> "Layout":
        """Та же раскладка в координатах экрана (кадр снимали с монитора со сдвигом left/top)."""
        return Layout({k: (x + dx, y + dy, w, h) for k, (x, y, w, h) in self.rects.items()},
                      self.pitch, dict(self.rows), dict(self.labels), self.unread,
                      {k: (x + dx, y + dy, w, h) for k, (x, y, w, h) in self.boxes.items()})

    def to_json(self) -> dict:
        return {"pitch": self.pitch, "rects": {k: list(v) for k, v in self.rects.items()},
                "rows": self.rows, "labels": self.labels, "unread": self.unread,
                "boxes": {k: list(v) for k, v in self.boxes.items()}}

    @classmethod
    def from_json(cls, d: dict) -> "Layout":
        return cls({k: tuple(int(a) for a in v) for k, v in d["rects"].items()}, float(d["pitch"]),
                   dict(d.get("rows", {})), dict(d.get("labels", {})), int(d.get("unread", 0)),
                   {k: tuple(int(a) for a in v) for k, v in d.get("boxes", {}).items()})


# ---------------- 1-3: ряд слотов по форме ----------------

def _row_periodicity(gray: np.ndarray, y_from: int, y_to: int, x0: int, x1: int,
                     p_lo: float, p_hi: float, band: int = 20) -> "list[tuple[float, int, int]]":
    """Для каждой строки y: (сила периодичности 0..1, y, шаг). Автокорреляция профиля полосы band px."""
    out = []
    lo, hi = int(p_lo), int(p_hi) + 1
    h2 = band // 2
    for yc in range(max(y_from, h2), min(y_to, gray.shape[0] - h2), 2):
        m = gray[yc - h2:yc + h2, x0:x1].mean(axis=0)
        z = m - cv2.blur(m.reshape(1, -1), (81, 1)).ravel()     # убрать плавный фон: остаются щели
        ac = np.correlate(z, z, "full")[len(z) - 1:]
        if ac[0] <= 0 or len(ac) <= hi:
            continue
        sc = ac[lo:hi] / ac[0]
        i = int(np.argmax(sc))
        out.append((float(sc[i]), yc, lo + i))
    return out


def _comb(gray: np.ndarray, y0: int, y1: int, x0: int, x1: int, p0: float, spread: float = 2.5
          ) -> "tuple[float, list[float]] | None":
    """
    Гребёнка: шаг P около p0 и сдвиг, при которых больше всего зубьев подряд попадают в тёмные щели.
    Одна пропущенная щель подряд допускается (ромб смены оружия посередине старой раскладки).
    Возвращает (P, x щелей слева направо) или None.
    """
    m = gray[y0:y1, x0:x1].mean(axis=0)
    loc = cv2.blur(m.reshape(1, -1), (int(p0) | 1, 1)).ravel()
    d = np.maximum(0.0, loc - m)                                 # насколько колонка темнее соседей
    dm = np.maximum.reduce([np.roll(d, s) for s in (-1, 0, 1)])  # щель +-1 px: шаг дробный
    thr = max(6.0, 0.35 * float(np.percentile(dm, 99)))
    best = None
    for P in np.arange(p0 - spread, p0 + spread, 0.1):
        for ph in np.arange(0.0, P, 0.5):
            xs = np.arange(ph, len(d) - 1, P).astype(int)
            ok = dm[xs] > thr
            run = best_run = start = best_start = miss = 0
            for i, o in enumerate(ok):
                if o:
                    if run == 0:
                        start = i
                    run += 1
                    miss = 0
                elif run:
                    miss += 1
                    if miss > 1:
                        run = miss = 0
                        continue
                if run > best_run:
                    best_run, best_start = run, start
            strength = float(dm[xs][ok].sum())
            if best is None or (best_run, strength) > (best[0], best[3]):
                best = (best_run, float(P), float(ph), strength, best_start)
    if best is None or best[0] < 3:
        return None
    n, P, ph, _, s0 = best
    return P, [x0 + ph + (s0 + k) * P for k in range(n)]


def _row_top(gray: np.ndarray, seps: "list[float]", P: float, y_lo: int, y_hi: int) -> "int | None":
    """Верх слотов: первая строка, где внутри клеток намного светлее, чем на щелях."""
    ys, cs = [], []
    for y in range(max(0, y_lo), min(gray.shape[0], y_hi)):
        row = gray[y]
        sep = np.mean([row[int(round(s)) - 1:int(round(s)) + 2].mean() for s in seps])
        inn = np.mean([row[int(s + 0.2 * P):int(s + 0.8 * P)].mean() for s in seps[:-1]])
        ys.append(y)
        cs.append(inn - sep)
    if not cs:
        return None
    cs = np.array(cs)
    on = np.nonzero(cs > 0.5 * cs.max())[0]
    return int(ys[on[0]]) if len(on) and cs.max() > 20 else None


def _find_row(gray: np.ndarray, y_from: int, y_to: int, p_lo: float, p_hi: float, min_cells: int,
              x_range: "tuple[int, int] | None" = None):
    """Самая периодичная полоса в [y_from, y_to) -> (P, щели, верх слотов) или None."""
    w = gray.shape[1]
    x0, x1 = x_range if x_range is not None else (int(w * 0.15), int(w * 0.85))
    x0, x1 = max(0, x0), min(w, x1)
    rows = _row_periodicity(gray, y_from, y_to, x0, x1, p_lo, p_hi)
    if not rows:
        return None
    s_best, y_best, p_best = max(rows)
    if s_best < 0.3:
        return None
    # Полоса = строки подряд вокруг лучшей с той же периодичностью (слоты + таблички под ними).
    by_y = {y: (s, p) for s, y, p in rows}
    ya = yb = y_best
    while ya - 2 in by_y and by_y[ya - 2][0] >= 0.35 and abs(by_y[ya - 2][1] - p_best) <= 3:
        ya -= 2
    while yb + 2 in by_y and by_y[yb + 2][0] >= 0.35 and abs(by_y[yb + 2][1] - p_best) <= 3:
        yb += 2
    c = _comb(gray, ya - 10, yb + 10, x0, x1, float(p_best))
    if c is None or len(c[1]) < min_cells + 1:
        return None
    P, seps = c
    top = _row_top(gray, seps, P, ya - int(P), yb + 10)
    return (P, seps, top) if top is not None else None


# ---------------- 4: таблички с клавишами ----------------

_label_bank: "dict[str, list[tuple[np.ndarray, float]]] | None" = None


def _bank() -> "dict[str, list[tuple[np.ndarray, float]]]":
    """
    Образцы табличек {текст: [(картинка как в игре, шаг панели, с которой снята)]} — грузятся один раз.
    Имя массива в npz: "<текст>__<n>__<шаг*10>". Храним БЕЗ пересчёта масштаба: сравнивать честнее,
    уменьшая крупное до мелкого (см. _match_bank).
    """
    global _label_bank
    if _label_bank is None:
        _label_bank = {}
        try:
            z = np.load(_LABELS_PATH, allow_pickle=False)
            for name in z.files:
                parts = name.split("__")
                p_src = float(parts[2]) / 10.0 if len(parts) >= 3 else REF_PITCH
                _label_bank.setdefault(parts[0], []).append((z[name], p_src))
        except (OSError, ValueError):
            logger.warning("bar_layout: нет образцов табличек %s — клавиши только через OCR", _LABELS_PATH)
    return _label_bank


class _BoxScorer:
    """
    "Похоже ли на табличку" для любого прямоугольника за O(1): рамка (1 px по краю) светлая, полоса
    сразу внутри неё — тёмная. Суммы по прямоугольникам — из интегрального изображения (сумма всех
    пикселей левее-выше точки), поэтому перебор тысяч кандидатов — доли секунды. Что снаружи рамки
    (иконка над табличкой, линия края панели) и текст в середине на оценку не влияют.
    """

    def __init__(self, g: np.ndarray) -> None:
        self.ii = cv2.integral(g.astype(np.float32), sdepth=cv2.CV_64F)
        self.H, self.W = g.shape[:2]

    def _sum(self, x, y, w, h):
        ii = self.ii
        return ii[y + h, x + w] - ii[y, x + w] - ii[y + h, x] + ii[y, x]

    def score(self, x, y, w, h) -> np.ndarray:
        """
        x, y, w, h — массивы numpy одинаковой длины; оценка 0..1, -1 — мимо.
        Рамка = САМАЯ ТЁМНАЯ из 4 сторон (у таблички светлые все четыре; у случайной текстуры
        иконки — одна-две). Оценка = (рамка - полоса внутри) / (рамка + 10).
        """
        ok = (x >= 0) & (y >= 0) & (x + w < self.W) & (y + h < self.H) & (w >= 7) & (h >= 7)
        x, y = np.where(ok, x, 0), np.where(ok, y, 0)
        w, h = np.where(ok, w, 7), np.where(ok, h, 7)
        top = self._sum(x, y, w, 1) / w
        bot = self._sum(x, y + h - 1, w, 1) / w
        lef = self._sum(x, y + 1, 1, h - 2) / (h - 2)
        rig = self._sum(x + w - 1, y + 1, 1, h - 2) / (h - 2)
        ring = np.minimum(np.minimum(top, bot), np.minimum(lef, rig))
        # Полоса внутри — САМАЯ СВЕТЛАЯ из 4 сторон: у настоящей таблички тёмные все, а "табличка",
        # которую сложили штрихи буквы (у "Num5" цифра 5 — почти квадрат), задевает штрих хоть одной
        # стороной. Глубина полосы 1 или 2 px — берём лучшую: у верхнего ряда рамка со скосом в 2 px
        # (85 -> 43 -> 28), а широкая буква (Q с хвостиком) почти касается рамки — там нужен 1 px.
        sc = np.full(x.shape, -1.0)
        for d in (1, 2):
            btop = self._sum(x + d, y + d, w - 2 * d, 1) / (w - 2 * d)
            bbot = self._sum(x + d, y + h - 1 - d, w - 2 * d, 1) / (w - 2 * d)
            blef = self._sum(x + d, y + d + 1, 1, h - 2 * d - 2) / (h - 2 * d - 2)
            brig = self._sum(x + w - 1 - d, y + d + 1, 1, h - 2 * d - 2) / (h - 2 * d - 2)
            band = np.maximum(np.maximum(btop, bbot), np.maximum(blef, brig))
            sc = np.maximum(sc, (ring - band) / (ring + 10.0))
        return np.where(ok & (ring > 30), sc, -1.0)


BOX_MIN_SCORE = 0.30                 # подобрано по скринам двух аккаунтов (bar_layout_test.py)


def _box_size(P: float) -> int:
    return int(round(0.257 * P))     # табличка с одним символом: 13 px при шаге 51, 14 при 54.5


def row_label_y(sc: _BoxScorer, centers: "list[float]", y_lo: int, y_hi: int, P: float) -> "int | None":
    """Высота табличек ряда — одна на весь ряд: строка, где сумма лучших оценок по всем слотам максимальна."""
    s0 = _box_size(P)
    best_y, best_v = None, 0.0
    for y in range(y_lo, y_hi):
        tot = 0.0
        for cx in centers:
            xs = np.round(cx - s0 / 2.0 + np.arange(-3, 4)).astype(int)
            v = sc.score(xs, np.full(7, y), np.full(7, s0), np.full(7, s0))
            tot += max(0.0, float(v.max()))
        if tot > best_v:
            best_y, best_v = y, tot
    return best_y


def box_at(sc: _BoxScorer, cx: float, y_lo: int, y_hi: int, P: float) -> "tuple[int, int, int, int] | None":
    """Лучшая табличка с центром ~cx (+-3 px) и верхом в [y_lo, y_hi): любой ширины до 1.3 P ("Ctrl", "Num5")."""
    s0 = _box_size(P)
    ys = np.arange(y_lo, y_hi)
    hs = np.array([s0 - 1, s0, s0 + 1])
    ws = np.arange(s0 - 1, int(1.3 * P) + 1)
    dx = np.arange(-3, 4)
    Y, Hh, Ww, D = np.meshgrid(ys, hs, ws, dx, indexing="ij")
    X = np.round(cx - Ww / 2.0 + D).astype(int)
    v = sc.score(X.ravel(), Y.ravel(), Ww.ravel(), Hh.ravel())
    i = int(np.argmax(v))
    if v[i] < BOX_MIN_SCORE:
        return None
    return int(X.ravel()[i]), int(Y.ravel()[i]), int(Ww.ravel()[i]), int(Hh.ravel()[i])


def boxes_in(sc: _BoxScorer, x0: int, x1: int, y0: int, y1: int, P: float) -> "list[tuple[int, int, int, int]]":
    """Все таблички с одним символом в области (локальные максимумы оценки, без наложений)."""
    s0 = _box_size(P)
    xs, ys = np.arange(max(0, x0), x1), np.arange(max(0, y0), y1)
    if len(xs) == 0 or len(ys) == 0:
        return []
    Y, X = np.meshgrid(ys, xs, indexing="ij")
    best = np.full(Y.size, -1.0)
    best_h = np.full(Y.size, s0)
    for h in (s0 - 1, s0, s0 + 1):
        v = sc.score(X.ravel(), Y.ravel(), np.full(X.size, s0), np.full(X.size, h))
        better = v > best
        best, best_h = np.where(better, v, best), np.where(better, h, best_h)
    out = []
    for i in np.argsort(best)[::-1]:
        if best[i] < BOX_MIN_SCORE:
            break
        x, y = int(X.ravel()[i]), int(Y.ravel()[i])
        if all(abs(x - b[0]) > s0 or abs(y - b[1]) > s0 for b in out):
            out.append((x, y, s0, int(best_h[i])))
    return out


def _norm(img: np.ndarray, P: float) -> np.ndarray:
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    f = REF_PITCH / P
    if abs(f - 1.0) > 0.01:
        g = cv2.resize(g, (max(4, round(g.shape[1] * f)), max(4, round(g.shape[0] * f))), interpolation=cv2.INTER_AREA)
    return g.astype(np.float32)


def _ncc(a: np.ndarray, b: np.ndarray) -> float:
    a = a.astype(np.float32).ravel() - a.mean()
    b = b.astype(np.float32).ravel() - b.mean()
    n = float(np.linalg.norm(a) * np.linalg.norm(b))
    return float(np.dot(a, b) / n) if n > 1e-6 else 0.0


def _shrink(img: np.ndarray, f: float) -> np.ndarray:
    if abs(f - 1.0) <= 0.01:
        return img
    return cv2.resize(img, (max(5, round(img.shape[1] * f)), max(5, round(img.shape[0] * f))), interpolation=cv2.INTER_AREA)


def _match_bank(lab: np.ndarray, P: float = REF_PITCH) -> "tuple[str | None, float]":
    """
    Лучший образец таблички: (текст, похожесть). lab — табличка как есть (серая, шаг панели P).
    Образец и табличку приводим к МЕНЬШЕМУ из двух масштабов, только уменьшая (INTER_AREA): увеличение
    "придумывает" пиксели и размывает по-другому, чем игра рисует мелкий шрифт. Рамка найдена с
    точностью +-1 px, поэтому два сравнения, берём лучшее: подгонка под размер образца и сдвиг +-2 px.
    """
    lab = lab.astype(np.float32)
    best: "tuple[str | None, float]" = (None, -1.0)
    for name, items in _bank().items():
        if name.startswith("MOUSE_"):
            continue                                           # значки мыши — отдельно (read_mouse)
        for t0, p_src in items:
            t = t0.astype(np.float32)
            if P < p_src:
                t, a = _shrink(t, P / p_src), lab
            else:
                t, a = t, _shrink(lab, p_src / P)
            if abs(t.shape[1] - a.shape[1]) > 3 or abs(t.shape[0] - a.shape[0]) > 3:
                continue                                       # другая ширина таблички ("Ctrl" против "1")
            sc = _ncc(cv2.resize(a, (t.shape[1], t.shape[0]), interpolation=cv2.INTER_AREA), t)
            big = cv2.copyMakeBorder(a, 2, 2, 2, 2, cv2.BORDER_REPLICATE)
            tt = t[1:-1, 1:-1]
            if tt.shape[0] <= big.shape[0] and tt.shape[1] <= big.shape[1]:
                sc = max(sc, float(cv2.minMaxLoc(cv2.matchTemplate(big, tt, cv2.TM_CCOEFF_NORMED))[1]))
            if sc > best[1]:
                best = (name, sc)
    return best


_ocr = None


def _ocr_label(lab_gray: np.ndarray) -> "str | None":
    """Табличка не похожа на образцы — читаем текстом (EasyOCR, если установлен). Только то, что бот умеет жать."""
    global _ocr
    try:
        if _ocr is None:
            # тяжёлый: только по нужде. ru+en — те же модели, что у чата (в exe они вшиты, качать не надо)
            from src.core.chat.ocr_engine import easyocr_reader
            _ocr = easyocr_reader(("ru", "en"))
        big = cv2.resize(lab_gray.astype(np.uint8), None, fx=4, fy=4, interpolation=cv2.INTER_CUBIC)
        big = cv2.copyMakeBorder(big, 12, 12, 12, 12, cv2.BORDER_REPLICATE)
        res = _ocr.readtext(big, detail=1, allowlist="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-=um")
    except Exception as e:
        logger.debug("bar_layout: OCR таблички недоступен: %s", e)
        return None
    if len(res) != 1 or float(res[0][2]) < OCR_MIN_CONF:
        return None                                  # не уверен — лучше "не знаю", чем чужая клавиша
    txt = str(res[0][1]).strip().upper()
    return txt if _KEY_RE.match(txt) else None


def read_label(img: np.ndarray, box: "tuple[int, int, int, int]", P: float, use_ocr: bool = True) -> "tuple[str | None, float]":
    """Текст таблички ("1", "Z", "-", "F1") и уверенность; None — не клавиша (мышь, Ctrl+..., не прочиталось)."""
    x, y, w, h = box
    crop = img[y:y + h, x:x + w]
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
    name, sc = _match_bank(gray, P)
    lab = _norm(crop, P)                                       # для OCR — в масштабе образцов
    if name is not None and sc >= LABEL_MIN_SCORE:
        return (name if _KEY_RE.match(name.upper()) else None), sc
    if use_ocr:
        t = _ocr_label(lab)
        if t is not None:
            return t, 0.0
    return None, sc


def mouse_region(gray: np.ndarray, cx: float, yb: int, P: float) -> np.ndarray:
    """Место значка мыши под слотом: там, где у других слотов табличка (значок чуть выше и выше ростом)."""
    x0, x1 = int(round(cx - 0.2 * P)), int(round(cx + 0.2 * P))
    y0, y1 = int(round(yb - 0.13 * P)), int(round(yb + 0.35 * P))
    return gray[max(0, y0):y1, max(0, x0):x1]


def read_mouse(gray: np.ndarray, cx: float, yb: int, P: float) -> "str | None":
    """Значок мыши под слотом -> "LMB" / "RMB" / "MMB", или None. Сравнение как у табличек (к меньшему масштабу)."""
    reg = mouse_region(gray, cx, yb, P).astype(np.float32)
    best, best_sc = None, -1.0
    for name, items in _bank().items():
        if not name.startswith("MOUSE_"):
            continue
        for t0, p_src in items:
            t = t0.astype(np.float32)
            a = reg
            if P < p_src:
                t = _shrink(t, P / p_src)
            else:
                a = _shrink(reg, p_src / P)
            big = cv2.copyMakeBorder(a, 3, 3, 3, 3, cv2.BORDER_REPLICATE)
            tt = t[2:-2, 2:-2]
            if tt.shape[0] > big.shape[0] or tt.shape[1] > big.shape[1] or tt.size == 0:
                continue
            sc = float(cv2.minMaxLoc(cv2.matchTemplate(big, tt, cv2.TM_CCOEFF_NORMED))[1])
            if sc > best_sc:
                best, best_sc = name[6:], sc
    return best if best_sc >= MOUSE_MIN_SCORE else None


def key_name(label: str) -> "str | None":
    """Надпись на панели -> имя клавиши ввода (как в skills_config / InputManager)."""
    try:
        from src.core.input_manager import InputManager
        return InputManager.canonical_key(label)
    except Exception:
        n = label.strip().lower()
        return {"-": "minus", "=": "equal"}.get(n, n)


# ---------------- ряды табличек (общие для бота и tools/build_key_labels.py) ----------------

def extend_by_labels(sc: _BoxScorer, seps: "list[float]", top: int, P: float, most: int = 2) -> "list[float]":
    """
    Гребёнка могла не увидеть щель у края панели (курсор, вспышка нажатия на крайнем слоте) — тогда
    крайний слот выпадает. Табличка с клавишей — надёжный признак слота: есть табличка под местом,
    где был бы следующий слот, — добавляем его (до most с каждого края). Замок слева и стрелки
    страниц справа табличек не имеют — дальше них не уйдём.
    """
    seps = list(seps)
    yb = row_label_y(sc, [a + P / 2 for a in seps[:-1]], int(top + 0.75 * P), int(top + 1.25 * P), P)
    if yb is None:
        return seps
    s0 = _box_size(P)

    def single(cx: float) -> bool:
        b = box_at(sc, cx, yb - 1, yb + 2, P)
        return b is not None and abs(b[0] + b[2] / 2 - cx) <= 3
    for _ in range(most):
        if single(seps[-1] + P / 2):
            seps.append(seps[-1] + P)
        else:
            break
    for _ in range(most):
        if seps[0] - P >= 0 and single(seps[0] - P / 2):
            seps.insert(0, seps[0] - P)
        else:
            break
    del s0
    return seps


def bottom_boxes(sc: _BoxScorer, seps: "list[float]", top: int, P: float) -> "list[tuple[int, int, int, int] | None]":
    """Таблички нижнего ряда — по одной под каждой клеткой (None — нет: мышь, ромб смены оружия)."""
    yb = row_label_y(sc, [a + P / 2 for a in seps[:-1]], int(top + 0.75 * P), int(top + 1.25 * P), P)
    if yb is None:
        return [None] * (len(seps) - 1)
    return [box_at(sc, a + P / 2, yb - 1, yb + 2, P) for a in seps[:-1]]


def top_boxes(sc: _BoxScorer, seps: "list[float]", top: int, P: float) -> "tuple[list[tuple[int, int, int, int]], float]":
    """
    Таблички верхнего ряда (Q E ...) слева направо и его шаг. Ищем только левый блок над нижним
    рядом (правее у разных раскладок смена оружия, зелья F1-F4). Слоты идут подряд с шагом ~P:
    одиночные находки между ними (кусок иконки) цепочку не рвут и в неё не попадают.
    """
    up = boxes_in(sc, int(seps[0] - 2.0 * P), int(seps[0] + 5.0 * P), int(top - 1.0 * P), int(top - 0.05 * P), P)
    if not up:
        return [], P
    # Самая частая высота — с весом оценки: надёжные таблички, а не шум.
    y_mode = max((b[1] for b in up), key=lambda y: sum(abs(y - b[1]) <= 1 for b in up))
    up = sorted((b for b in up if abs(b[1] - y_mode) <= 1), key=lambda b: b[0])
    chain = [up[0]]
    for b in up[1:]:
        d = (b[0] + b[2] / 2) - (chain[-1][0] + chain[-1][2] / 2)
        if 0.85 * P <= d <= 1.2 * P:
            chain.append(b)
        elif d > 1.2 * P:
            break
    if len(chain) < 2:
        return [], P
    P2 = float(np.median(np.diff([b[0] + b[2] / 2 for b in chain])))
    while True:                                      # дальше могут быть широкие таблички ("Num5")
        nb = box_at(sc, chain[-1][0] + chain[-1][2] / 2 + P2, y_mode - 1, y_mode + 2, P)
        if nb is None or nb[2] <= _box_size(P) + 1:
            break
        chain.append(nb)
    return chain, P2


# ---------------- всё вместе ----------------

def detect_layout(frame: np.ndarray, use_ocr: bool = True) -> "Layout | None":
    """Панель на кадре монитора (BGR) -> Layout в px кадра, или None."""
    if frame is None or frame.ndim != 3:
        return None
    img = np.ascontiguousarray(frame[..., :3])
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)
    h = gray.shape[0]
    bottom = _find_row(gray, int(h * 0.70), h, *PITCH_RANGE, min_cells=8)
    if bottom is None:
        return None
    P, seps, top = bottom
    rects: "dict[str, tuple[int, int, int, int]]" = {}
    rows: "dict[str, str]" = {}
    labels: "dict[str, str]" = {}
    unread = 0

    sc = _BoxScorer(gray)

    boxes: "dict[str, tuple[int, int, int, int]]" = {}

    def add(key_text: "str | None", rect, row_name: str, box) -> None:
        nonlocal unread
        key = key_name(key_text) if key_text else None
        if key is None or key in rects:
            unread += 1
            return
        shown = MOUSE_NAMES.get(key_text.upper(), key_text)
        rects[key], rows[key], labels[key] = rect, row_name, shown
        if box is not None:
            boxes[key] = box

    # Нижний ряд: клетки — по гребёнке (+ крайние по табличкам), табличка — под центром клетки.
    seps = extend_by_labels(sc, seps, top, P)
    yb = row_label_y(sc, [a + P / 2 for a in seps[:-1]], int(top + 0.75 * P), int(top + 1.25 * P), P)
    for a, box in zip(seps[:-1], bottom_boxes(sc, seps, top, P)):
        if box is not None:
            text = read_label(img, box, P, use_ocr)[0]
        else:                                       # таблички нет — может, значок кнопки мыши
            text = read_mouse(gray, a + P / 2, yb, P) if yb is not None else None
        add(text, (int(round(a + _ICON_X0 * P)), int(round(top + _ICON_Y0 * P)),
                   max(4, int(round(_ICON_W * P))), max(4, int(round(_ICON_H * P)))), "bottom", box)

    # Верхний ряд (Q E ...): по табличкам — они надёжнее гребёнки на 3-4 слотах.
    chain, P2 = top_boxes(sc, seps, top, P) if TOP_ROW else ([], P)
    for box in chain:
        cx = box[0] + box[2] / 2
        w, h = max(4, int(round(_TOP_ICON_W * P2))), max(4, int(round(_TOP_ICON_H * P2)))
        # Иконка над табличкой: низ иконки на ~0.06 шага выше рамки таблички (3 px при 49.5).
        y_bot = box[1] - int(round(0.06 * P2))
        add(read_label(img, box, P, use_ocr)[0], (int(round(cx - w / 2)), y_bot - h, w, h), "top", box)

    if not rects:
        return None
    return Layout(rects, float(P), rows, labels, unread, boxes)


def boxes_present(gray: np.ndarray, boxes: "dict[str, tuple[int, int, int, int]]", ox: int = 0, oy: int = 0) -> float:
    """Доля табличек, которые на месте (кадр gray с левым верхом в ox, oy). Панель сдвинули — около 0."""
    if not boxes:
        return 0.0
    sc = _BoxScorer(gray)
    b = np.array(list(boxes.values()))
    v = sc.score(b[:, 0] - ox, b[:, 1] - oy, b[:, 2], b[:, 3])
    return float(np.mean(v >= BOX_MIN_SCORE))


def save_label_bank(samples: "dict[str, list[tuple[np.ndarray, float]]]", path: str = _LABELS_PATH) -> None:
    """Для tools/build_key_labels.py: образцы табличек в npz (имя массива "<текст>__<n>__<шаг*10>")."""
    arrs = {"%s__%d__%d" % (k, i, round(p * 10)): v.astype(np.uint8)
            for k, vs in samples.items() for i, (v, p) in enumerate(vs)}
    np.savez_compressed(path, **arrs)
    global _label_bank
    _label_bank = None


def layout_from_meta(meta: dict) -> "Layout | None":
    d = meta.get("layout") if isinstance(meta, dict) else None
    try:
        return Layout.from_json(d) if d else None
    except (KeyError, TypeError, ValueError):
        return None


def dumps(layout: Layout) -> str:
    return json.dumps(layout.to_json(), ensure_ascii=False)
