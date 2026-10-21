"""
OCR (распознавание текста с картинки) для чата: одна функция "картинка -> строка".

Порядок: Windows OCR -> EasyOCR -> Tesseract (первый, что поднялся).
Основной движок — встроенный в Windows 10/11 (Windows.Media.Ocr) через пакет winsdk:
бесплатный, без интернета и без GPU, ~10-30 мс на строку, русский — если в Windows стоит русский
язык (у тебя стоит: интерфейс игры русский). Запасной — Tesseract (pytesseract), если установлен.
Нет ни того, ни другого -> make_ocr() вернёт None, и чат просто не читается (бот не падает).

Как проверить на ПК: python tools/chat_ocr_check.py <скрин.png>
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

import cv2
import numpy as np

logger = logging.getLogger(__name__)


class WinOcr:
    """Windows.Media.Ocr. Создавать и звать — из одного фонового потока (у WinRT свой COM)."""

    def __init__(self, lang: str = "ru") -> None:
        from winsdk.windows.globalization import Language                  # noqa: WPS433
        from winsdk.windows.media.ocr import OcrEngine                     # noqa: WPS433
        # Берём язык из СПИСКА установленных распознавателей (Language.OCR~~~ru-RU и т.п.):
        # короткий тег "ru" Windows может не сопоставить с "ru-RU", а "языки профиля
        # пользователя" — это языки ввода, они к OCR отношения не имеют (2026-10-07: OCR ru-RU
        # установлен, а try_create_from_user_profile_languages вернул None).
        avail = [l.language_tag for l in OcrEngine.available_recognizer_languages]
        pick = next((t for t in avail if t.lower().startswith(lang)), None) or \
            next((t for t in avail if t.lower().startswith("en")), None) or (avail[0] if avail else None)
        eng = OcrEngine.try_create_from_language(Language(pick)) if pick else None
        if eng is None:
            eng = OcrEngine.try_create_from_user_profile_languages()
        if eng is None:
            raise RuntimeError("Windows OCR: движок не создался (распознаватели в системе: %s)" % (avail or "нет"))
        self._eng = eng
        self.name = "Windows OCR (%s; есть: %s)" % (eng.recognizer_language.language_tag, ", ".join(avail))

    def __call__(self, img: np.ndarray) -> str:
        from winsdk.windows.graphics.imaging import BitmapAlphaMode, BitmapPixelFormat, SoftwareBitmap
        from winsdk.windows.storage.streams import DataWriter
        bgra = cv2.cvtColor(img, cv2.COLOR_GRAY2BGRA if img.ndim == 2 else cv2.COLOR_BGR2BGRA)
        h, w = bgra.shape[:2]
        dw = DataWriter()
        dw.write_bytes(bgra.tobytes())
        bmp = SoftwareBitmap.create_copy_from_buffer(dw.detach_buffer(), BitmapPixelFormat.BGRA8, w, h,
                                                     BitmapAlphaMode.PREMULTIPLIED)
        res = asyncio.run(self._recognize(bmp))
        return " ".join(line.text for line in res.lines)

    async def _recognize(self, bmp):
        return await self._eng.recognize_async(bmp)


def easyocr_models_dir() -> "str | None":
    """
    Модели EasyOCR внутри собранного exe (2026-10-11): PyInstaller кладёт их в _internal/easyocr_models
    (EZF.spec). У нового человека интернета до серверов моделей может не быть — качать нельзя.
    Не exe (запуск из python) — None: EasyOCR берёт свою папку ~/.EasyOCR как раньше.
    """
    base = getattr(sys, "_MEIPASS", None)
    d = os.path.join(base, "easyocr_models") if base else None
    return d if d and os.path.isdir(d) else None


def easyocr_reader(langs: "tuple[str, ...]" = ("ru", "en")):
    """easyocr.Reader с моделями из exe (если собраны) — один способ для чата и для табличек панели."""
    import easyocr
    d = easyocr_models_dir()
    kw = {"model_storage_directory": d, "download_enabled": False} if d else {}
    return easyocr.Reader(list(langs), gpu=False, verbose=False, **kw)


class EasyOcr:
    """
    EasyOCR (pip install easyocr): своя нейросеть, от настроек Windows не зависит (2026-10-07: у
    Windows OCR на ПК игрока "распознаватели в системе: нет", хотя пакеты ru/en стоят). Медленнее
    (~100-200 мс на строку на CPU), но чат читается в фоне и только новые строки — хватает.
    Первый запуск качает модели (~100 МБ) — один раз.
    """

    def __init__(self, langs: "tuple[str, ...]" = ("ru", "en")) -> None:
        import collections
        self._r = easyocr_reader(langs)
        self.name = "EasyOCR (%s, CPU)" % "+".join(langs)
        self._cache = collections.OrderedDict()

    def __call__(self, img: np.ndarray) -> str:
        h = hash(img.tobytes())
        if h in self._cache:
            self._cache.move_to_end(h)
            return self._cache[h]
            
        # recognize(), а не readtext(): readtext сначала гоняет нейросеть-ДЕТЕКТОР (CRAFT) — ищет,
        # где на картинке текст. Это самая дорогая часть (~0.3-1 с на CPU на строку). А строку
        # чата мы уже вырезали сами (segment_lines) — детектор не нужен, сразу распознаём всю
        # картинку как одну строку. Быстрее в разы (2026-10-09, "ответ ждём 30 с").
        try:
            res = " ".join(self._r.recognize(img, detail=0, paragraph=True)).strip()
        except Exception:
            res = " ".join(self._r.readtext(img, detail=0, paragraph=True)).strip()
        
        self._cache[h] = res
        if len(self._cache) > 200:
            self._cache.popitem(last=False)
        return res


class TesseractOcr:
    def __init__(self, lang: str = "rus+eng") -> None:
        import pytesseract                                                  # noqa: WPS433
        pytesseract.get_tesseract_version()
        self._t = pytesseract
        self._lang = lang
        self.name = "Tesseract (%s)" % lang

    def __call__(self, img: np.ndarray) -> str:
        return self._t.image_to_string(img, lang=self._lang, config="--psm 7").strip()


LAST_ERRORS: "list[str]" = []        # почему движки не поднялись (для tools/chat_ocr_check.py)


def make_ocr():
    """Лучший доступный движок или None (причины — в лог и в LAST_ERRORS)."""
    LAST_ERRORS.clear()
    for cls in (WinOcr, EasyOcr, TesseractOcr):
        try:
            eng = cls()
            logger.info("Чат: OCR — %s", eng.name)
            return eng
        except Exception as e:
            msg = "%s: %s: %s" % (cls.__name__, type(e).__name__, e)
            LAST_ERRORS.append(msg)
            logger.info("Чат: OCR недоступен — %s", msg)
    return None
