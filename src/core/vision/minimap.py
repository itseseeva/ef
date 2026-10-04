"""
src/core/vision/minimap.py — "одометр" по миникарте (2026-10-04): где персонаж и
движется ли он, без чтения памяти игры.

Миникарта в TL смотрит на север (надписи всегда горизонтальны), персонаж — в её
центре, а карта ПРОКРУЧИВАЕТСЯ под ним. Значит, на сколько пикселей сдвинулась
картинка карты — на столько (в обратную сторону) сдвинулся персонаж. Сдвиг между
двумя кадрами меряем фазовой корреляцией (cv2.phaseCorrelate): сравнение кадров в
частотной области, точность — доли пикселя, ~1.5 мс на кадр 228x162.

Мешают неподвижные относительно рамки вещи: стрелка персонажа и оранжевый конус обзора
(они в центре и не едут с картой — тянули бы ответ к "сдвига нет"). Их вырезаем
мягкой "дыркой" радиусом ~40 px в центре; края кадра гасим окном Ханна (без него края
дают ложный пик). Мелкие значки (квесты, мобы) на результат почти не влияют.

Дрейф: сдвиг меряем не от прошлого кадра, а от "опорного" (keyframe) и меняем
опорный, только когда уехали на KEYFRAME_SHIFT_PX. Так ошибки не копятся на каждом
кадре, а только на смене опорного (раз в ~10 px пути).

Единицы — пиксели миникарты (x на восток, y на юг). В метры переводит navigation.py
(масштаб выучивает сам: по списку целей, пока бежим к мобу).

Как проверить без игры: python -m src.core.vision.minimap путь_к_скриншоту.png —
сдвигает карту на скриншоте на известные (dx, dy) и печатает, что намерил одометр.
"""

from __future__ import annotations

import math
from collections import deque

import cv2
import numpy as np

# Зона миникарты внутри рамки — без шапки с названием зоны, шестерёнки и значков по
# краям (они неподвижны). Замер по скриншоту 1920x1080: карта (1571..1894, 29..225),
# персонаж в (1732, 127). Координаты — в vision_config (MINIMAP_*), здесь центр в зоне.
CENTER_IN_ROI = (120, 79)
HOLE_R_PX = 40.0             # вырезаем стрелку и конус обзора
HOLE_SOFT_PX = 12.0          # мягкий край дырки: резкая граница сама дала бы пик "на месте"
HIGHPASS_SIGMA = 3.0         # убираем плавные перепады яркости, оставляем рисунок (дороги, скалы)
KEYFRAME_SHIFT_PX = 10.0     # уехали дальше от опорного кадра — он становится новым опорным
MIN_RESPONSE = 0.25          # ниже — кадр не похож на опорный (меню, карта закрыта, телепорт)
LOST_REKEY = 5               # столько непохожих кадров подряд — начинаем с нового опорного


class MinimapOdometry:
    """Копит позицию персонажа в пикселях миникарты от точки, где одометр запустили."""

    def __init__(self, center: "tuple[float, float]" = CENTER_IN_ROI, history_s: float = 6.0) -> None:
        self._center = center
        self._win: "np.ndarray | None" = None
        self._key: "np.ndarray | None" = None
        self._key_pos = np.zeros(2)
        self.pos = np.zeros(2)                 # x восток, y юг (px карты)
        self.ok = False                        # последний кадр измерен уверенно
        self.response = 0.0
        self._lost = 0
        self.history: "deque[tuple[float, float, float]]" = deque()   # (t, x, y)
        self._history_s = history_s

    def _window(self, h: int, w: int) -> np.ndarray:
        if self._win is None or self._win.shape != (h, w):
            yy, xx = np.mgrid[0:h, 0:w]
            rr = np.hypot(xx - self._center[0], yy - self._center[1])
            hole = np.clip((rr - HOLE_R_PX) / HOLE_SOFT_PX, 0.0, 1.0)
            self._win = (cv2.createHanningWindow((w, h), cv2.CV_32F) * hole).astype(np.float32)
        return self._win

    def _prepare(self, frame_bgr: np.ndarray) -> np.ndarray:
        g = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY).astype(np.float32)
        g -= cv2.GaussianBlur(g, (0, 0), HIGHPASS_SIGMA)
        g -= g.mean()
        return g * self._window(*g.shape)

    def update(self, frame_bgr: "np.ndarray | None", now: float) -> bool:
        """Новый кадр миникарты. True — позиция обновлена уверенно."""
        if frame_bgr is None or frame_bgr.size == 0:
            self.ok = False
            return False
        cur = self._prepare(frame_bgr)
        if self._key is None:
            self._key, self._key_pos = cur, self.pos.copy()
            self.ok = True
            self._remember(now)
            return True
        (sx, sy), resp = cv2.phaseCorrelate(self._key, cur)
        self.response = float(resp)
        if resp < MIN_RESPONSE:
            self.ok = False
            self._lost += 1
            if self._lost >= LOST_REKEY:
                # Карта долго "не та" (открыто меню, загрузка). Позицию не трогаем —
                # продолжаем от последней известной с новым опорным кадром.
                self._key, self._key_pos, self._lost = cur, self.pos.copy(), 0
            return False
        self._lost = 0
        # Карта сдвинулась на (sx, sy) -> персонаж на (-sx, -sy).
        self.pos = self._key_pos - np.array([sx, sy])
        if math.hypot(sx, sy) >= KEYFRAME_SHIFT_PX:
            self._key, self._key_pos = cur, self.pos.copy()
        self.ok = True
        self._remember(now)
        return True

    def _remember(self, now: float) -> None:
        self.history.append((now, float(self.pos[0]), float(self.pos[1])))
        while self.history and now - self.history[0][0] > self._history_s:
            self.history.popleft()

    def displacement(self, window_s: float, now: float) -> "tuple[float, float, float] | None":
        """Сдвиг (dx, dy) за последние window_s секунд и фактическое окно; None — мало данных."""
        if len(self.history) < 2:
            return None
        t_now, x_now, y_now = self.history[-1]
        for t, x, y in self.history:
            if t_now - t <= window_s:
                if t_now - t < window_s * 0.6:
                    return None                  # история короче окна — рано судить
                return x_now - x, y_now - y, t_now - t
        return None


def bearing_deg(dx: float, dy: float) -> float:
    """Направление вектора на карте: 0 — север, 90 — восток (по часовой), y карты — на юг."""
    return math.degrees(math.atan2(dx, -dy)) % 360.0


if __name__ == "__main__":
    import sys
    import time

    path = sys.argv[1] if len(sys.argv) > 1 else None
    if not path:
        raise SystemExit("python -m src.core.vision.minimap скриншот_1920x1080.png")
    img = cv2.imread(path)
    x0, y0, w, h = 1612, 48, 228, 162               # как MINIMAP_* в vision_config при 1920x1080
    odo = MinimapOdometry()
    odo.update(img[y0:y0 + h, x0:x0 + w], 0.0)
    for i, (dx, dy) in enumerate(((3, 0), (6, 2), (9, 4), (12, 6), (9, 9), (4, 12))):
        frame = img[y0 + dy:y0 + dy + h, x0 + dx:x0 + dx + w]
        t0 = time.perf_counter()
        odo.update(frame, float(i + 1))
        print("персонаж сдвинут на (%3d,%3d) -> одометр (%6.2f,%6.2f), уверенность %.2f, %.1f мс"
              % (dx, dy, odo.pos[0], odo.pos[1], odo.response, (time.perf_counter() - t0) * 1000))
