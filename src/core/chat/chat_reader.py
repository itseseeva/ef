"""
Чтение чата с экрана: строки -> сообщения (личные и обычный чат). Только ЗРЕНИЕ и разбор,
без модели и без клавиатуры (одна задача на модуль, см. Claude.md "Архитектура кода").

Как выглядит чат T&L (скрины 2026-10-07):
  - заголовок сообщения: аватар, значок канала, ник. Личное (ЛС) — значок и ник ФИОЛЕТОВЫЕ,
    рядом треугольник "ответить"; обычный чат — значок и ник светло-серые;
  - строки текста: "Привет , тут ? 20:46" — текст цвета канала, в конце время (коричневатое);
  - несколько сообщений подряд от одного ника — заголовок один, дальше только строки текста.
  Закрытый чат показывает последние сообщения внизу слева, открытый — то же в окне.

Конвейер (быстрый, без нейросетей в главном потоке):
  1) зона чата = доли экрана (REGION_FRAC) — работает при любом разрешении;
  2) маски цвета: фиолетовый (ЛС), светло-серый (обычный), коричневатый (время);
  3) горизонтальная проекция масок -> полосы строк;
  4) каждая полоса -> чёрный текст на белом, x3 -> OCR (ocr_engine.py) -> строка;
  5) разбор: строка со значком канала слева — заголовок с ником, остальные — текст.
В ЗАКРЫТОМ чате у строк нет времени (скрин 1), поэтому заголовок (ник) узнаём по ЗНАЧКУ канала
слева (шаблоны src/config/chat_icon_*.png), а не по времени — работает и в открытом, и в закрытом.
Новые сообщения отличаем по ключу (канал, ник, текст, время): старые при старте не трогаем.

Как проверить без игры: chat_reader_test.py (скрины игрока), а OCR на ПК —
python tools/chat_ocr_check.py <скрин.png>
"""
from __future__ import annotations

import re
import time as _time
from dataclasses import dataclass

import cv2
import numpy as np

# Зона сообщений чата в долях экрана (x0, y0, x1, y1): от верха открытого окна до строки ввода.
# Замер по скринам 1920x1080 (2026-10-07): окно x 20-490, сообщения y ~600-1010.
REGION_FRAC = (0.008, 0.55, 0.26, 0.945)

AVATAR_FRAC = 0.095        # левая часть зоны — аватары (серые лица склеивали строки ника и текста)
RIGHT_CUT_FRAC = 0.07      # правый край — полоса прокрутки окна чата (тоже склеивала строки)
LOCAL_CONTRAST = 55       # буква ярче соседнего тёмного (обводка/фон) хотя бы на столько
LINE_MIN_H = 7            # px полосы строки: ниже — шум
LINE_GAP = 2              # строки разделяет хотя бы столько пустых пикселей
LINE_MIN_PIXELS = 25      # пикселей текста в полосе: меньше — не строка
OCR_SCALE = 2             # x2 (было x3): EasyOCR всё равно приводит строку к высоте 64 px — x3 был лишней работой

WHISPER, NORMAL = "whisper", "normal"

_TIME_RE = re.compile(r"(\d{1,2})\s*[:.;]\s*(\d{2})\s*$")
_TIME_LOOSE_RE = re.compile(r"\s(\d{2})[,.]?(\d{2})\s*$")
_DATE_RE = re.compile(r"^\s*\d{4}\s*-\s*\d{2}\s*-\s*\d{2}\s*$")
_NAME_RE = re.compile(r"[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё0-9_]{1,19}")


@dataclass(frozen=True)
class ChatLine:
    y0: int
    y1: int
    channel: str              # WHISPER / NORMAL — по цвету текста (у заголовка — по значку)
    image: np.ndarray         # подготовленная для OCR полоса (тёмный текст на белом)
    header: bool = False      # строка с ником: слева значок канала
    glyphs: "np.ndarray | None" = None   # маска "букв" полосы (без фона) — отпечаток для кэша OCR


@dataclass(frozen=True)
class ChatMessage:
    channel: str
    sender: "str | None"      # None — заголовок не попал в зону (ушёл вверх)
    text: str
    time: str                 # "20:46"
    header_y: "tuple[int, int] | None" = None   # полоса заголовка (ник + треугольник "ответить") в зоне

    @property
    def key(self) -> tuple:
        # Без времени: в закрытом чате его нет. Повтор того же текста отличаем по тому, что
        # старое сообщение успело уйти с экрана (см. ChatReader.SEEN_TTL_S).
        return (self.channel, self.sender, self.text)


# Значки каналов слева от ника (скрины 2026-10-07, 1080p): фиолетовый пузырь — ЛС, серый — обычный.
# Заголовок узнаём по значку, а не по времени: в ЗАКРЫТОМ чате времени у строк нет (скрин 1).
# Замер: у заголовков совпадение 0.92-1.0 (и в закрытом чате поверх мира), у строк текста <= 0.39.
_ICON_PATHS = {WHISPER: "chat_icon_whisper.png", NORMAL: "chat_icon_normal.png"}
ICON_MIN_SCORE = 0.75
# 0.75, а не 0.4 (2026-10-09, скрин игрока): у настоящих заголовков совпадение со значком 0.88-1.0,
# у обычных строк текста — до 0.55-0.65. При 0.4 строка "и по скольку часов ты фармиишь ?" стала
# "заголовком", ник = "фармиишь" — бот ответил несуществующему игроку и потерял контекст разговора.
ICON_SEARCH_FRAC = 0.14   # значок — в начале строки: ищем в первых 14% ширины (после аватаров)
_icons: "dict[str, np.ndarray] | None" = None


def _icon_templates() -> "dict[str, np.ndarray]":
    """Шаблоны значков (серые), грузятся один раз."""
    global _icons
    if _icons is None:
        import os
        base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "config")
        _icons = {}
        for ch, name in _ICON_PATHS.items():
            img = cv2.imread(os.path.join(base, name), cv2.IMREAD_GRAYSCALE)
            if img is not None:
                _icons[ch] = img
    return _icons


def header_channel(gray_band: np.ndarray, scale: float = 1.0) -> "str | None":
    """Канал по значку в начале строки или None — это не заголовок, а текст сообщения."""
    best, best_ch = 0.0, None
    for ch, t in _icon_templates().items():
        if scale != 1.0:
            t = cv2.resize(t, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        if gray_band.shape[0] < t.shape[0] or gray_band.shape[1] < t.shape[1]:
            continue
        sc = float(cv2.minMaxLoc(cv2.matchTemplate(gray_band, t, cv2.TM_CCOEFF_NORMED))[1])
        if sc > best:
            best, best_ch = sc, ch
    return best_ch if best >= ICON_MIN_SCORE else None


def region_rect(left: int, top: int, width: int, height: int) -> dict:
    """Зона чата в координатах экрана (как ROI для захвата)."""
    x0, y0, x1, y1 = REGION_FRAC
    return {"left": left + int(width * x0), "top": top + int(height * y0),
            "width": int(width * (x1 - x0)), "height": int(height * (y1 - y0))}


def color_masks(frame: np.ndarray) -> "tuple[np.ndarray, np.ndarray, np.ndarray]":
    """
    (фиолетовый, светло-серый, время) — булевы маски кадра BGR. Пороги — с запасом на закрытый
    чат: там текст тусклее (фиолетовый ~ (112,64,128) против (176,48,208) в открытом окне).
    int16 — чтобы разности каналов не "заворачивались" через 0, как у uint8.
    """
    f = frame[..., :3].astype(np.int16)
    b, g, r = f[..., 0], f[..., 1], f[..., 2]
    mx = np.maximum(np.maximum(b, g), r)
    mn = np.minimum(np.minimum(b, g), r)
    purple = (b - g >= 35) & (r - g >= 25) & (b >= 100)
    light = (mn >= 125) & (mx - mn <= 28)
    time_ = (r - b >= 18) & (r >= 95) & (g - b >= 4) & (r - g <= 40) & (mx - mn <= 70)
    # КОНТРАСТ (скрин 2026-10-07 21:38, закрытый чат над светлой брусчаткой): серый камень проходит
    # маску "светлый" и склеивал полосу высотой 200 px. У букв чата есть тёмная обводка/тень —
    # рядом (окно 5x5) всегда есть пиксель темнее на LOCAL_CONTRAST; у камня перепады мягкие.
    v = mx.astype(np.uint8)
    local_min = cv2.erode(v, np.ones((5, 5), np.uint8)).astype(np.int16)
    sharp = (mx - local_min) >= LOCAL_CONTRAST
    return purple & sharp, light & sharp, time_ & sharp


GLYPH_MAX_W, GLYPH_MAX_H, GLYPH_MIN_AREA = 34, 24, 4    # px при 1080p (крупный шрифт чата — до ~16 px)


def _keep_glyphs(mask: np.ndarray, scale: float = 1.0) -> np.ndarray:
    """
    Оставить только то, что похоже на буквы: связные пятна не шире/выше буквы и не точки.
    Края брусчатки, ступеней и цветов над светлым миром (скрин 2026-10-07 21:38) — длинные
    тонкие линии через полстроки: они склеивали все строки закрытого чата в одну полосу 200 px.
    """
    n, lab, st, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=8)
    mw, mh = GLYPH_MAX_W * scale, GLYPH_MAX_H * scale
    ok = (st[:, cv2.CC_STAT_WIDTH] <= mw) & (st[:, cv2.CC_STAT_HEIGHT] <= mh) & (st[:, cv2.CC_STAT_AREA] >= GLYPH_MIN_AREA)
    ok[0] = False
    return ok[lab]


LINE_MAX_H = 22           # px при 1080p: полоса выше — это несколько строк, склеенных шумом фона


def _split_band(rows: np.ndarray, y0: int, y1: int, scale: float) -> "list[tuple[int, int]]":
    """
    Слишком высокую полосу режем по самому "пустому" ряду внутри (рекурсивно). Над светлым миром
    между строками закрытого чата нет полностью пустых рядов — крапинки камня (3-20 px в ряду)
    против 100-170 px у строки текста: провал профиля всё равно виден, режем по нему.
    """
    if y1 - y0 <= LINE_MAX_H * scale:
        return [(y0, y1)]
    sm = np.convolve(rows[y0:y1].astype(np.float32), np.ones(3, np.float32) / 3, mode="same")
    edge = max(3, int(4 * scale))
    inner = sm[edge:len(sm) - edge]
    if inner.size == 0:
        return [(y0, y1)]
    cut = y0 + edge + int(np.argmin(inner))
    return _split_band(rows, y0, cut, scale) + _split_band(rows, cut + 1, y1, scale)


def segment_lines(frame: np.ndarray, scale: float = 1.0) -> "list[ChatLine]":
    """
    Полосы строк текста сверху вниз. Канал полосы — по тому, каких пикселей текста больше:
    фиолетовых (ЛС) или светлых (обычный чат). Время в канал не входит, но в OCR — входит.
    """
    x0 = int(frame.shape[1] * AVATAR_FRAC)
    x1 = int(frame.shape[1] * (1.0 - RIGHT_CUT_FRAC))
    frame_t = frame[:, x0:x1]
    purple, light, time_ = color_masks(frame_t)
    raw_text = purple | light | time_                 # для OCR: все пиксели цвета текста
    text = _keep_glyphs(raw_text, scale)              # для разметки строк: только "буквы"
    purple &= text
    light &= text
    rows = np.count_nonzero(text, axis=1)
    on = rows >= 3
    bands: "list[tuple[int, int]]" = []
    y, h = 0, len(on)
    while y < h:
        if not on[y]:
            y += 1
            continue
        y0 = y
        gap = 0
        while y < h and gap <= LINE_GAP:
            gap = 0 if on[y] else gap + 1
            y += 1
        bands.extend(_split_band(rows, y0, y - gap, scale))
    lines: "list[ChatLine]" = []
    for y0, y1 in bands:
        if y1 - y0 >= LINE_MIN_H and int(text[y0:y1].sum()) >= LINE_MIN_PIXELS:
            p, l = int(purple[y0:y1].sum()), int(light[y0:y1].sum())
            channel = WHISPER if p >= l else NORMAL
            # В OCR — не маска (она рвёт сглаженные края букв), а сама полоса: серая, инверсия
            # (тёмный текст на светлом — так OCR обучены), растяжка контраста, x3.
            # Закрытый чат лежит прямо на мире (трава, камень) — фон за буквами гасим: оставляем
            # только пиксели цвета текста с ободком 1 px (сглаженные края букв), остальное белое.
            ya, yb = max(0, y0 - 3), y1 + 3
            band = frame_t[ya:yb]
            # Ободок 2 px (окно 5x5) чтобы не съедало сглаживание шрифта на темном фоне
            keep = cv2.dilate(raw_text[ya:yb].astype(np.uint8), np.ones((5, 5), np.uint8)) > 0
            
            # Вместо BGR2GRAY берем максимальный канал (V). 
            # BGR2GRAY делает фиолетовый текст серым (105), а инверсия делает его светло-серым (150).
            # Max канал делает фиолетовый ярким (208), инверсия делает его почти черным (47), что идеально для OCR.
            b_ch, g_ch, r_ch = band[..., 0], band[..., 1], band[..., 2]
            v_channel = np.maximum(np.maximum(b_ch, g_ch), r_ch)
            
            gray = 255 - v_channel
            gray = cv2.normalize(gray, None, 0, 255, cv2.NORM_MINMAX)
            gray[~keep] = 255
            # Обрезаем полосу по ширине до самого текста (+запас): время OCR растёт с шириной
            # картинки, а короткое "ку" раньше распознавалось как строка во всю ширину окна.
            cols = np.flatnonzero(text[y0:y1].any(axis=0))
            if len(cols):
                gray = gray[:, max(0, cols[0] - 8):cols[-1] + 9]
            img = cv2.resize(gray, None, fx=OCR_SCALE, fy=OCR_SCALE, interpolation=cv2.INTER_CUBIC)
            # Значок канала — в начале строки (с запасом сверху/снизу: он выше букв).
            xs = int(frame.shape[1] * ICON_SEARCH_FRAC)
            pad = int(round(9 * scale))           # значок выше букв: ниже строки на 6-7 px при 1080p
            hb = frame[max(0, y0 - pad):y1 + pad, max(0, x0 - 12):x0 + xs]
            hch = header_channel(cv2.cvtColor(hb, cv2.COLOR_BGR2GRAY), scale)
            lines.append(ChatLine(y0, y1, hch or channel, img, header=hch is not None,
                                  glyphs=text[max(0, y0 - 1):y1 + 1]))
    return lines


def _looks_like_text(t: str) -> bool:
    """Строка — текст, а не мусор OCR с фона ("<a 7 4 “aa"): буквы/цифры — хотя бы половина знаков."""
    t = t.replace(" ", "")
    if len(t) == 0:
        return False
    good = sum(ch.isalnum() for ch in t)
    return good >= 1 and good >= 0.5 * len(t)


MERGE_GAP = 7             # px при 1080p: строки ближе — перенос одного сообщения, а не новое


def parse(lines: "list[tuple]", merge_gap: int = MERGE_GAP) -> "list[ChatMessage]":
    """
    [(канал, распознанный текст, y0, y1, заголовок?)] сверху вниз -> сообщения. Чистая функция.
    Заголовок (значок канала слева) — ник: самое длинное слово-имя (значки OCR читает обрывками
    вроде "@}" и "WF"). Остальные строки — текст сообщения от последнего ника этого канала;
    время в конце ("20:46", есть только в открытом чате) отрезаем.
    Без флага заголовка (старый формат: 2-4 поля) — заголовок там, где нет времени.
    """
    out: "list[ChatMessage]" = []
    sender: "dict[str, str | None]" = {WHISPER: None, NORMAL: None}
    header: "dict[str, tuple[int, int] | None]" = {WHISPER: None, NORMAL: None}
    prev_y1: "int | None" = None
    hs = sorted(int(it[3]) - int(it[2]) for it in lines if len(it) >= 4)
    med_h = hs[len(hs) // 2] if hs else 0
    for item in lines:
        channel, raw = item[0], item[1]
        yy = (int(item[2]), int(item[3])) if len(item) >= 4 else None
        s = " ".join(raw.split())
        if not s or _DATE_RE.match(s):
            continue
        m = _TIME_RE.search(s)
        if m is None:
            # Время мелким шрифтом OCR читает без двоеточия ("123 2050", "ау ау 2127"): 4 цифры
            # ЧЧММ в конце после пробела — время, если перед ним есть текст и часы/минуты валидны.
            m2 = _TIME_LOOSE_RE.search(s)
            if m2 and m2.start() > 0 and int(m2.group(1)) < 24 and int(m2.group(2)) < 60:
                m = m2
        is_header = bool(item[4]) if len(item) >= 5 else m is None
        if is_header:
            names = _NAME_RE.findall(s)
            if names:
                nick = max(names, key=len)
                # ">Shinka" — НАШ исходящий шёпот игроку Shinka (скрин 2026-10-09), а не его
                # сообщение: строки под таким заголовком — наши, отвечать на них нельзя.
                outgoing = any(ch in s[:s.find(nick)] for ch in ">›»")
                sender[channel] = None if outgoing else nick
                header[channel] = yy
            prev_y1 = None                           # после заголовка — всегда новое сообщение
            continue
        body = (s[:m.start()] if m else s).strip(" .,:;-")
        if body and not _looks_like_text(body):
            prev_y1 = yy[1] if yy is not None else None   # мусор (блик/трава прочитались) — пропуск
            continue
        # Без ника выше в зоне — не сообщение (это другие строки интерфейса над окном чата:
        # имена группы и т.п.) либо заголовок уехал вверх. Отвечать некому — пропускаем.
        if body and sender[channel] is not None:
            tm = "%02d:%s" % (int(m.group(1)), m.group(2)) if m else ""
            # ПЕРЕНОС длинного сообщения (скрин 2026-10-07, SenseiGG — 3 строки): строки одного
            # сообщения идут плотно (зазор ~5 px), разные сообщения — с зазором 9-14 px и чертой.
            # Плотную строку без своего времени клеим к предыдущему сообщению того же ника.
            prev = out[-1] if out else None
            gap_ok = False
            if yy is not None and prev_y1 is not None:
                # Порог — от высоты строки: крупный шрифт чата (2026-10-07) — перенос 8-11 px,
                # новое сообщение 18+ px; мелкий — 5 и 9-14 px. 0.8 высоты строки делит оба.
                # Высота строки — МЕДИАНА по всем строкам кадра: над светлым миром отдельные полосы
                # раздувает шум, и порог от них склеивал "ау ау" с "Ты тут ?".
                gap_ok = yy[0] - prev_y1 <= max(merge_gap, 0.8 * med_h)
            if (prev is not None and gap_ok and prev.channel == channel
                    and prev.sender == sender[channel] and not prev.time):
                out[-1] = ChatMessage(channel, prev.sender, prev.text + " " + body, tm, prev.header_y)
            else:
                out.append(ChatMessage(channel, sender[channel], body, tm, header[channel]))
        prev_y1 = yy[1] if yy is not None else None
    return out


def _norm(t: str) -> str:
    return " ".join(t.lower().split())


def similar(a: str, b: str) -> float:
    """Похожесть строк 0..1 (difflib): одно и то же сообщение OCR читает чуть по-разному кадр от кадра."""
    import difflib
    return difflib.SequenceMatcher(None, _norm(a), _norm(b)).ratio()


class _Track:
    """Одно сообщение, которое видим несколько кадров подряд: все прочтения + когда видели."""
    __slots__ = ("channel", "sender", "header_y", "reads", "first", "last", "emitted", "polls")

    def __init__(self, m: ChatMessage, now: float) -> None:
        self.channel, self.sender, self.header_y = m.channel, m.sender, m.header_y
        self.reads: "list[str]" = [m.text]
        self.first = self.last = now
        self.emitted = False
        self.polls = 1                       # в скольких опросах видели (картинка могла не меняться)

    def best(self) -> str:
        """
        Голосование по кадрам (стандарт для OCR видео): самое частое прочтение, при равенстве —
        самое длинное. Строка, которая гаснет, читается всё хуже ("Ты тут ?" -> "Ты у ?") —
        одно плохое прочтение не перебивает несколько хороших.
        """
        from collections import Counter
        cnt = Counter(self.reads)
        return max(cnt, key=lambda t: (cnt[t], len(t)))


class ChatReader:
    """
    Новые сообщения: каждое — ОДИН раз и в лучшем прочтении. ocr — функция (картинка -> строка).

    Почему не "по точному тексту" (лог 2026-10-07): одно сообщение OCR читает по-разному в разных
    кадрах ("Ты у ?", "Ты и ?", "Ты тут ?"), и каждое прочтение уходило как новое сообщение. Теперь:
      - прочтения одного ника и канала, похожие на уже известное сообщение (similar >= SAME_SIM,
        длины соизмеримы, порядок ленты сохранён) — это оно же: копятся в голосование;
      - сообщение отдаём, когда оно продержалось STABLE_S (2+ кадра) — лучшим прочтением;
      - закрытый чат гаснет и при новом сообщении снова показывает несколько старых: если разом
        появилось несколько незнакомых сообщений — новое только НИЖНЕЕ, остальные — старые;
      - при старте всё, что уже видно, — старое (на него не отвечаем).
    Ушедшее с экрана сообщение помним SEEN_TTL_S: тот же текст позже — новое.
    """
    SAME_SIM = 0.6
    LEN_RATIO = 0.6
    # Отдаём сообщение с ПЕРВОГО прочтения (2026-10-09, "долгие ответы"): ждать второго опроса —
    # это ещё один круг OCR (~2-3 с на CPU). Повторные/кривые прочтения того же сообщения
    # отсекает чат-бот (already_replied), старые при старте — "праймирование" ниже.
    STABLE_S = 0.0
    MIN_POLLS = 1
    SEEN_TTL_S = 90.0
    MAX_OCR_LINES = 4             # больше строк за опрос не распознаём (EasyOCR ~0.3 с на строку на CPU)

    def __init__(self, ocr, scale: float = 1.0, clock=None) -> None:
        import time as _t
        self._ocr = ocr
        self._scale = scale
        self._clock = clock or _t.monotonic
        self._tracks: "list[_Track]" = []
        self._primed = False
        self._last_sig: "bytes | None" = None
        self._last_msgs: "list[ChatMessage]" = []
        # Кэш OCR по отпечатку картинки строки (2026-10-09, "быстрее"): при новом сообщении
        # меняется одна строка, а раньше EasyOCR заново читал ВСЕ (~150-200 мс каждая).
        # Словарь, а не lru_cache: ключ — байты, чистим сами, когда разросся.
        self._ocr_cache: "list[tuple]" = []   # (профиль, начало, конец, текст)
        self.last_stats = (0, 0, 0.0)     # (строк, прочитано OCR, секунд) — для диагностики

    def _match(self, m: ChatMessage, start: int) -> "int | None":
        """
        Индекс уже известного сообщения, которое это прочтение и есть, или None — новое.
        Чат — лента: порядок сообщений не меняется, новые снизу. Поэтому ищем только ПОСЛЕ
        предыдущего совпадения (start) — иначе короткое "Ты тут ?" прилипало к старому
        "Привет , тут ?" (похожесть 0.64) и новое сообщение терялось. Плюс длины должны быть
        соизмеримы: "Привет" и "Привет , тут ?" — разные сообщения.
        """
        best, best_s = None, 0.0
        for i in range(start, len(self._tracks)):
            t = self._tracks[i]
            if t.channel != m.channel or t.sender != m.sender:
                continue
            ref = t.best()
            la, lb = len(_norm(m.text)), len(_norm(ref))
            if min(la, lb) < self.LEN_RATIO * max(la, lb):
                continue
            sc = max(similar(m.text, r) for r in t.reads[-5:])
            if sc > best_s:
                best, best_s = i, sc
        return best if best_s >= self.SAME_SIM else None

    def poll(self, frame: np.ndarray) -> "list[ChatMessage]":
        now = self._clock()
        purple, light, time_ = color_masks(frame)
        sig = np.packbits((purple | light)[::2, ::2]).tobytes()
        if sig == self._last_sig:
            msgs = self._last_msgs                # картинка та же — те же сообщения, OCR не зовём
        else:
            self._last_sig = sig
            lines = segment_lines(frame, self._scale)
            msgs = parse(self._read_lines(lines),
                         int(round(MERGE_GAP * self._scale)))
            self._last_msgs = msgs
        return self.update(msgs, sig, now)

    # Отпечаток строки для кэша OCR — "профиль букв по столбцам": сколько пикселей букв в
    # каждом столбце полосы (160 корзин). Почему так (проверено на скринах + шуме фона):
    #   - от фона не зависит: берём маску "букв" после фильтра формы глифов;
    #   - не зависит от сдвига полосы на 1-3 px по вертикали (сумма по столбцу);
    #   - разные строки различаются расположением слов и пробелов.
    # Похожесть — косинус (скалярное произведение нормированных профилей) + совпадение краёв.
    FP_BINS = 160
    FP_SAME = 0.94                # своя строка под шумом фона ~0.96+, чужие строки <= ~0.90
    FP_EDGE_TOL = 0.04            # начало/конец текста не дальше 4% ширины
    CACHE_MAX = 80

    def _fingerprint(self, ln: "ChatLine") -> "tuple[np.ndarray, float, float]":
        g = ln.glyphs if ln.glyphs is not None else (ln.image < 128)
        col = g.sum(axis=0).astype(np.float32)
        w = max(1, len(col))
        nz = np.flatnonzero(col)
        x0, x1 = (nz[0] / w, nz[-1] / w) if len(nz) else (0.0, 0.0)
        prof = cv2.resize(col[None, :], (self.FP_BINS, 1), interpolation=cv2.INTER_AREA)[0]
        prof = np.convolve(prof, (1.0, 2.0, 1.0), "same")      # сгладить: сдвиг на 1 корзину не важен
        prof /= float(np.linalg.norm(prof)) + 1e-6
        return prof, x0, x1

    def _cache_get(self, fp) -> "str | None":
        # Перебор до 80 векторов по 160 чисел — доли миллисекунды против ~0.3-1 с OCR строки.
        prof, x0, x1 = fp
        for cprof, cx0, cx1, txt in reversed(self._ocr_cache):
            if abs(cx0 - x0) > self.FP_EDGE_TOL or abs(cx1 - x1) > self.FP_EDGE_TOL:
                continue
            if float(prof @ cprof) >= self.FP_SAME:
                return txt
        return None

    DUMP_DIR = "logs/chat_ocr_lines"   # первые DUMP_MAX строк, ушедших в OCR, — картинками
    DUMP_MAX = 40                       # (смотреть, ЧТО читает OCR, если чтение медленное)

    def _dump(self, img: np.ndarray, txt: str) -> None:
        n = getattr(self, "_dumped", 0)
        if n >= self.DUMP_MAX:
            return
        self._dumped = n + 1
        try:
            import os
            os.makedirs(self.DUMP_DIR, exist_ok=True)
            safe = "".join(ch for ch in txt[:30] if ch.isalnum() or ch == " ").strip() or "_"
            cv2.imencode(".png", img)[1].tofile(os.path.join(self.DUMP_DIR, "%02d_%s.png" % (n, safe)))
        except Exception:
            pass

    def _read_lines(self, lines: "list[ChatLine]") -> "list[tuple]":
        """
        Текст строк с экономией OCR (2026-10-09, лог: "чтение чата заняло 8-15 с"):
          - кэш по отпечатку "где буквы" с допуском (IoU): закрытый чат лежит на мире, фон за
            буквами меняется каждый кадр — точный хэш картинки промахивался ВСЕГДА, и EasyOCR
            перечитывал весь чат на каждом опросе;
          - OCR не больше MAX_OCR_LINES строк за опрос, СНИЗУ (новые сообщения появляются снизу);
            верхние незнакомые строки пропускаем — это старьё, оно нам не нужно.
        """
        out: "list[tuple]" = []
        misses = 0
        t0 = _time.perf_counter()
        for ln in reversed(lines):
            fp = self._fingerprint(ln)
            txt = self._cache_get(fp)
            if txt is None:
                if misses >= self.MAX_OCR_LINES:
                    continue
                misses += 1
                txt = self._ocr(ln.image)
                self._dump(ln.image, txt)
                self._ocr_cache.append((*fp, txt))
                del self._ocr_cache[:-self.CACHE_MAX]
            out.append((ln.channel, txt, ln.y0, ln.y1, ln.header))
        out.reverse()
        self.last_stats = (len(lines), misses, _time.perf_counter() - t0)
        return out

    def update(self, msgs: "list[ChatMessage]", sig: object, now: float) -> "list[ChatMessage]":
        """
        Логика без зрения (тестируется отдельно): прочитанные сейчас сообщения -> новые.
        sig — отпечаток картинки: голос в прочтение добавляем, только если картинка новая.
        """
        self._tracks = [t for t in self._tracks if now - t.last <= self.SEEN_TTL_S]
        fresh: "list[_Track]" = []
        pos = 0
        for m in msgs:
            i = self._match(m, pos)
            if i is None:
                t = _Track(m, now)
                self._tracks.append(t)
                fresh.append(t)
                pos = len(self._tracks)
            else:
                t = self._tracks[i]
                pos = i + 1
                if sig != getattr(self, "_voted_sig", None):
                    t.reads.append(m.text)        # голос — только от новой картинки
                t.last = now
                t.polls += 1
                t.header_y = m.header_y
        self._voted_sig = sig
        if not self._primed:
            # Старт: всё, что уже видно в чате, — старое, на него НЕ отвечаем (2026-10-09: без
            # этого бот при запуске отвечал на висящие в чате сообщения — часть "двойных" ответов).
            self._primed = True
            for t in self._tracks:
                t.emitted = True
            return []
        if len(fresh) >= 2:
            for t in fresh[:-1]:
                t.emitted = True                  # закрытый чат "вспомнил" старые — новое только нижнее
        out = []
        for t in self._tracks:
            if not t.emitted and now - t.first >= self.STABLE_S and t.polls >= self.MIN_POLLS:
                t.emitted = True
                out.append(ChatMessage(t.channel, t.sender, t.best(), "", t.header_y))
        return out
