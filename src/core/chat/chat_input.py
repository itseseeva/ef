"""
Открыта ли СТРОКА ВВОДА чата (печатаем в чат) — по экрану, а не "по таймингу".

Зачем (2026-10-09, жалоба "после сообщения бот печатает скиллы в чат"): чат-бот жал Enter / Esc
вслепую, по паузам. Если строка ввода осталась открытой (Enter на пустой строке её не закрыл,
игра подвисла на кадр), бот возвращался к бою — и цифры скиллов, W/A/S/D уходили буквами в чат.
Если же жать Esc вслепую при уже закрытом чате — откроется меню игры.

Признак открытой строки ввода (скрины 2026-10-07): у поля ввода ЗОЛОТАЯ рамка (RGB ~246,206,138)
— длинная горизонтальная линия ~400 px внизу слева. Закрытый чат ("Enter Чат") и окно чата без
фокуса такой линии не имеют (максимум 3-7 золотых пикселей в ряду против 409).

Чистая функция + маленький помощник ожидания; клавиатуру модуль не трогает.
Как проверить без игры: chat_input_test.py (скрины игрока: открытая строка / закрытый чат).
"""
from __future__ import annotations

import time

import numpy as np

# Зона поля ввода в долях экрана (x0, y0, x1, y1): внизу слева, под окном чата.
INPUT_FRAC = (0.02, 0.925, 0.26, 0.985)
GOLD_LINE_FRAC = 0.08          # золотая линия длиннее 8% ширины экрана (~150 px при 1920) — строка открыта


def input_rect(left: int, top: int, width: int, height: int) -> dict:
    x0, y0, x1, y1 = INPUT_FRAC
    return {"left": left + int(width * x0), "top": top + int(height * y0),
            "width": int(width * (x1 - x0)), "height": int(height * (y1 - y0))}


def input_open(zone: np.ndarray, screen_w: int) -> bool:
    """zone — кадр зоны INPUT_FRAC (BGR). True — строка ввода чата открыта (в фокусе)."""
    f = zone[..., :3].astype(np.int16)
    b, g, r = f[..., 0], f[..., 1], f[..., 2]
    gold = (r >= 190) & (g >= 150) & (b >= 90) & (b <= 175) & (r - b >= 70)
    return int(gold.sum(axis=1).max(initial=0)) >= GOLD_LINE_FRAC * screen_w


class ChatInputWatch:
    """
    grab() -> кадр зоны поля ввода (BGR). wait(True/False) — дождаться нужного состояния.
    Опрос раз в 40 мс: строка открывается/закрывается за 1-3 кадра игры.
    """
    POLL_S = 0.04

    def __init__(self, grab, screen_w: int, clock=time.monotonic, sleep=time.sleep) -> None:
        self._grab = grab
        self._w = screen_w
        self._clock = clock
        self._sleep = sleep

    def is_open(self) -> bool:
        z = self._grab()
        return z is not None and input_open(z, self._w)

    def wait(self, want_open: bool, timeout_s: float = 1.0) -> bool:
        end = self._clock() + timeout_s
        while True:
            if self.is_open() == want_open:
                return True
            if self._clock() >= end:
                return False
            self._sleep(self.POLL_S)

    def ensure_closed(self, press_esc, attempts: int = 3) -> bool:
        """
        Закрыть строку ввода, если она открыта. Esc жмём ТОЛЬКО когда строка видна открытой:
        Esc при закрытом чате открывает меню игры. True — закрыта (бою можно возвращаться).
        """
        for _ in range(attempts):
            if self.wait(False, 0.25):
                return True
            press_esc()
        return self.wait(False, 0.5)
