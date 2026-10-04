"""
src/core/vision/hp_text.py

Читатель ТЕКСТА HP на панели цели ("1 647/1 647").

Зачем он нужен. Полоска HP на панели узкая (139 px), и её длина "дрожит" на
1-3 п.п. даже у мобa, которого никто не бьёт (в логе: 100 -> 98 -> 100 -> 98
на цели, до которой ещё бежать 19 секунд). Порог "HP упало" по полоске поэтому
срабатывает впустую. А число рядом с полоской — точное целое: пока HP моба не
изменилось, текст пиксель в пиксель тот же, а при ЛЮБОМ уроне он меняется.

Что делает модуль. Два слоя, второй пока не включён:
  1) ОТПЕЧАТОК ТЕКСТА (сделано): бинарная маска светлых малонасыщенных пикселей
     внутри полоски. Это "картинка числа". Совпала с эталоном — HP то же;
     отличается — оно другое (какое именно, не важно: у моба в бою HP только
     падает). Для шлагбаума "мы уже бьём моба" и для охранника цели этого
     достаточно, расшифровка цифр не нужна.
  2) РАСШИФРОВКА цифр шаблонами (НЕ сделано): нужны картинки всех десяти цифр
     0-9, а на имеющихся скриншотах есть только 1, 4, 6, 7, 9. Недостающие соберёт
     режим сбора (blackbox.HpSampleCollector), после чего decode() добавим сюда.

Чистые функции над numpy — без mss, без состояния. Тест без игры:

    python hp_text_test.py        # прогон на реальных кадрах панели

Идиома: frozen dataclass с eq=False — запись неизменяема (нельзя испортить в
середине тика), а сравнение по умолчанию идёт по идентичности: у numpy-массива
"==" возвращает массив, и автоматический __eq__ dataclass-а упал бы с ошибкой.
Сравнивать два текста нужно явно через diff_px().
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass

import cv2
import numpy as np

from src.config import panel_template as T

# Границы порога готовим ОДИН раз при импорте, а не на каждый кадр: np.array()
# на 60 FPS цикле — лишняя аллокация.
_LO = np.array((0, 0, T.HPTEXT_MIN_VAL), np.uint8)
_HI = np.array((180, T.HPTEXT_MAX_SAT, 255), np.uint8)


@dataclass(frozen=True, eq=False)
class HpText:
    """Отпечаток текста HP: маска (bool, высота x ширина зоны), число светлых пикселей, хеш."""
    mask: np.ndarray
    ink: int
    sig: int

    @classmethod
    def from_mask(cls, mask: np.ndarray) -> "HpText":
        """
        Собрать отпечаток из готовой bool-маски (без cv2: так его можно строить в
        тестах на фейковых данных). Маска блокируется от записи — отпечаток
        остаётся тем, чем был в момент чтения.
        """
        m = np.ascontiguousarray(mask, dtype=bool)
        m.flags.writeable = False
        # crc32 по упакованным битам: ~110 байт, доли микросекунды. Хеш нужен
        # для быстрого "одинаково?" и для имени файла в режиме сбора.
        sig = zlib.crc32(np.packbits(m).tobytes())
        return cls(mask=m, ink=int(np.count_nonzero(m)), sig=sig)

    def __repr__(self) -> str:
        return "HpText(sig=%08x, ink=%d, %dx%d)" % (self.sig, self.ink, *self.mask.shape)


def extract(hsv_interior: np.ndarray) -> "HpText | None":
    """
    Отпечаток текста из HSV-куска ВНУТРЕННОСТИ полоски (высота x ширина x 3).
    None — текст прочитать нельзя (кусок слишком мал или светлых пикселей меньше
    HPTEXT_MIN_INK): вызывающий обязан понимать это как "нет данных", а НЕ как
    "HP изменилось".

    Маска = "мало насыщенный И яркий": текст почти белый, а заливка полоски —
    насыщенная золотая/красная, пустая часть — тёмная; мир за панелью на маску не
    влияет (полоска непрозрачна).
    """
    if hsv_interior is None or hsv_interior.ndim != 3:
        return None
    h, w = hsv_interior.shape[:2]
    if w < 20 or h < 6:
        return None
    x0 = int(w * T.HPTEXT_ZONE_FRAC[0])
    x1 = int(w * T.HPTEXT_ZONE_FRAC[1])
    mask = cv2.inRange(hsv_interior[:, x0:x1], _LO, _HI) > 0
    if int(np.count_nonzero(mask)) < T.HPTEXT_MIN_INK:
        return None
    return HpText.from_mask(mask)


def diff_px(a: HpText, b: HpText) -> int:
    """
    Сколько пикселей маски различаются. 0 — тот же текст. Хеши равны -> 0 без
    сравнения массивов (самый частый случай: HP не менялось). Размеры зон разные
    (сменилась геометрия панели) -> считаем отличие максимальным, чтобы не
    принять разные картинки за одинаковые.
    """
    if a.sig == b.sig:
        return 0
    if a.mask.shape != b.mask.shape:
        return max(a.ink, b.ink)
    return int(np.count_nonzero(a.mask ^ b.mask))


def changed(a: "HpText | None", b: "HpText | None",
            min_px: int = T.HPTEXT_CHANGE_MIN_PX) -> bool:
    """
    Другое ли число. Если хотя бы одно чтение недоступно (None) — False: нет
    данных не равно "изменилось", иначе мигание текста запускало бы ложные тревоги.
    """
    if a is None or b is None:
        return False
    return diff_px(a, b) >= min_px
