"""
src/core/navigation.py — "где я и куда идти" для подфазы ROAM (2026-10-04).

Источник правды о месте — одометр миникарты (src/core/vision/minimap.py): карта смотрит на
север и прокручивается под персонажем, по её сдвигу мы знаем, куда и насколько ушли.
Ни памяти игры, ни калибровки чувствительности мыши. Карта любой локации годится: одометр
сравнивает миникарту саму с собой, нужно только, чтобы она стояла на том же месте экрана.

Что здесь решается:
    - ЯКОРЬ: место последнего убийства (2026-10-04: "дома" и разворота назад больше нет —
      мобы спавнятся часто, споты разные). Прогулка кружит рядом с якорем — там, где мобы
      реально были, — а сам якорь переезжает с каждым убитым мобом;
    - ПРОГУЛКА: в списке целей пусто — случайная точка в радиусе explore_m от якоря, бежим к ней;
    - КУРС: куда смотрит камера, узнаём по ДВИЖЕНИЮ на карте за последнюю секунду с
      поправкой на то, какие клавиши были зажаты (стрейф вправо = камера смотрит на 90°
      левее направления бега). Повернуть камеру на нужный угол: угол x счётчиков_на_градус;
      этот коэффициент бот ВЫУЧИВАЕТ сам по каждому повороту (было/стало);
    - ЗАСТРЯЛ: клавиши движения зажаты 1.5 с, а карта почти не сдвинулась;
    - МАСШТАБ: px карты в метре выучиваем по списку целей — пока персонаж бежит к мобу,
      расстояние в списке падает на N метров, а карта сдвигается на M px.

Единицы: позиция в px миникарты (x — восток, y — юг), курс — градусы от севера по часовой.

Как проверить без игры: navigation_test.py (искусственная карта: персонаж бегает по
плоскости, одометр подменён точными координатами).
"""

from __future__ import annotations

import logging
import math
import random
from collections import deque
from dataclasses import dataclass

from src.core.vision.minimap import MinimapOdometry, bearing_deg

gamepad_debug_logger = logging.getLogger("input_debug")   # тот же файл, что у бота (input_debug.log)


def wrap180(a: float) -> float:
    """Угол в (-180, 180]: разница курсов без скачка через 0/360."""
    a = (a + 180.0) % 360.0 - 180.0
    return 180.0 if a == -180.0 else a


@dataclass
class NavTuning:
    explore_m: float = 30.0          # точки прогулки — не дальше от якоря (место последнего убийства)
    waypoint_min_m: float = 10.0     # и не ближе к нам (иначе "прогулка" на месте)
    arrive_m: float = 4.0
    px_per_m_guess: float = 1.5      # до первого замера масштаба (по списку целей)
    heading_window_s: float = 1.0    # курс — по движению за последнюю секунду
    heading_min_px: float = 2.5      # сдвинулись меньше — курс не судим
    turn_tol_deg: float = 20.0       # ошибка курса меньше — не поворачиваем (человек тоже не идеален)
    turn_cooldown_s: float = 1.2     # между поворотами: дать курсу "устояться" и измерить результат
    counts_per_deg0: float = 6000.0 / 360.0   # стартовая оценка (ScanTuning.full_turn_px / 360)
    counts_per_deg_range: "tuple[float, float]" = (4.0, 80.0)
    learn_min_deg: float = 35.0      # учимся только на заметных поворотах: на мелких шум курса больше самого поворота
    learn_keep: int = 7              # итог — медиана последних замеров (один кривой замер не портит)
    stuck_window_s: float = 1.5
    stuck_min_m: float = 0.6         # за 1.5 с бега сдвинулись меньше — застряли
    stuck_min_body_s: float = 0.9    # ...при том что клавиши "толкали" хотя бы на 0.9 с бега (A+D разом,
                                     # W+S или прыжок на месте — не застревание: мы и не шли)
    stuck_cooldown_s: float = 3.0
    scale_min_m: float = 6.0         # масштаб меряем на отрезке подхода не короче этого
    odo_every_s: float = 0.2         # одометр 5 раз/с: дёшево (~2 мс) и хватает для курса
    rot_tol_deg: float = 4.0         # камера повернулась за окно замера больше — курс по дуге, не судим
    cam_ref_max_age_s: float = 90.0  # курс камеры "по счётчикам" верим столько после последнего замера
    motion_window_s: float = 1.0     # направление бега (без клавиш: персонаж бежит к цели сам)
    motion_min_px: float = 2.0       # ~3.5 м при масштабе ~0.6 px/м (замер 2026-10-04)


class Navigator:
    """Позиция/дом/прогулка/курс. Чистая логика: время и кадры передаёт бот (тесты — фальшивые)."""

    def __init__(self, odometry: "MinimapOdometry | None" = None, tuning: "NavTuning | None" = None,
                 rng: "random.Random | None" = None) -> None:
        self.odo = odometry or MinimapOdometry()
        self.t = tuning or NavTuning()
        self._rng = rng or random.Random()
        self.anchor = (0.0, 0.0)
        self._anchor_pending = True                # якорь = первая уверенно измеренная точка
        self.counts_per_deg = self.t.counts_per_deg0
        self._scale: "deque[float]" = deque(maxlen=9)
        self._approach: "tuple[float, float, float] | None" = None     # (d0, x0, y0)
        self.waypoint: "tuple[float, float] | None" = None
        self._last_turn_at = -1e9
        self._pending_learn: "tuple[float, float, float] | None" = None  # (курс до, счётчики, когда)
        self._stuck_at = -1e9
        self.last_ok_at = -1e9
        self.heading: "float | None" = None
        self.turns_learned = 0
        self._k_samples: "deque[float]" = deque(maxlen=self.t.learn_keep)
        # Счётчики поворота камеры (InputManager.camera_yaw_counts) в моменты замеров одометра
        # и последний уверенный курс камеры вместе со счётчиками в тот момент.
        self._yaw_hist: "deque[tuple[float, int]]" = deque(maxlen=64)
        self._cam_ref: "tuple[float, int, float] | None" = None      # (курс, счётчики, когда)
        self._was_turning = False
        self._turn_end_at = -1e9

    # --- одометр ---
    def update(self, frame, now: float, yaw: "int | None" = None) -> bool:
        if yaw is not None:
            self._yaw_hist.append((now, int(yaw)))
        ok = self.odo.update(frame, now)
        if ok:
            self.last_ok_at = now
            if self._anchor_pending:
                self._anchor_pending = False
                self.anchor = self.pos
                gamepad_debug_logger.debug("NAV     якорь прогулки — здесь (%.0f, %.0f) px карты", *self.anchor)
        return ok

    def set_anchor(self) -> None:
        """Убили моба / запустили бота: прогулка дальше кружит вокруг этого места."""
        self._anchor_pending = True

    def available(self, now: float) -> bool:
        """Одометр живой (карта читалась последние 2 с) — иначе прогулка без него опасна."""
        return now - self.last_ok_at <= 2.0

    @property
    def pos(self) -> "tuple[float, float]":
        return float(self.odo.pos[0]), float(self.odo.pos[1])

    # --- масштаб ---
    @property
    def px_per_m(self) -> float:
        if not self._scale:
            return self.t.px_per_m_guess
        s = sorted(self._scale)
        return s[len(s) // 2]                      # медиана: один кривой замер (обход препятствия) не портит

    @property
    def scale_known(self) -> bool:
        return bool(self._scale)

    def reset_approach(self) -> None:
        self._approach = None

    def observe_target_distance(self, d_m: "float | None") -> None:
        """
        Подход к цели из списка (персонаж бежит сам): расстояние в списке и сдвиг карты
        дают масштаб. Отрезок — от первого замера, пока цель не стала ближе на scale_min_m.
        """
        if d_m is None:
            return
        x, y = self.pos
        if self._approach is None or d_m > self._approach[0]:
            self._approach = (float(d_m), x, y)
            return
        d0, x0, y0 = self._approach
        if d0 - d_m >= self.t.scale_min_m:
            px = math.hypot(x - x0, y - y0)
            ratio = px / (d0 - d_m)
            if 0.2 <= ratio <= 20.0:
                self._scale.append(ratio)
                gamepad_debug_logger.debug("NAV     масштаб карты: %.2f px/м (замер %.2f, %d замеров)",
                                           self.px_per_m, ratio, len(self._scale))
            self._approach = (float(d_m), x, y)

    # --- якорь и прогулка ---
    def dist_anchor_m(self) -> float:
        x, y = self.pos
        return math.hypot(x - self.anchor[0], y - self.anchor[1]) / self.px_per_m

    def pick_waypoint(self) -> "tuple[float, float]":
        """Случайная точка прогулки: в круге explore_m от якоря, не ближе waypoint_min_m к нам."""
        ppm = self.px_per_m
        x, y = self.pos
        best = None
        for _ in range(30):
            a = self._rng.uniform(0.0, 2.0 * math.pi)
            r = self.t.explore_m * math.sqrt(self._rng.uniform(0.15, 1.0)) * ppm   # sqrt — равномерно по площади
            wx, wy = self.anchor[0] + r * math.sin(a), self.anchor[1] - r * math.cos(a)
            best = (wx, wy)
            if math.hypot(wx - x, wy - y) >= self.t.waypoint_min_m * ppm:
                break
        self.waypoint = best
        return best

    def dist_waypoint_m(self) -> float:
        if self.waypoint is None:
            return 0.0
        x, y = self.pos
        return math.hypot(self.waypoint[0] - x, self.waypoint[1] - y) / self.px_per_m

    def arrived(self) -> bool:
        return self.waypoint is not None and self.dist_waypoint_m() <= self.t.arrive_m

    # --- курс ---
    def _yaw_at(self, t_want: float) -> "int | None":
        for t, y in self._yaw_hist:
            if t >= t_want:
                return y
        return None

    def _rotated(self, now: float, window_s: float) -> bool:
        """Камера поворачивалась за окно (поток камеры ведёт цель/осматривается) — курс по дуге."""
        if len(self._yaw_hist) < 2:
            return False
        y_then = self._yaw_at(now - window_s)
        if y_then is None:
            return False
        return abs(self._yaw_hist[-1][1] - y_then) / self.counts_per_deg > self.t.rot_tol_deg

    def camera_heading(self, yaw_now: "int | None", now: float) -> "float | None":
        """Курс камеры сейчас: последний замер + повороты, которые бот сделал с тех пор."""
        if self._cam_ref is None or yaw_now is None or now - self._cam_ref[2] > self.t.cam_ref_max_age_s:
            return None
        h, y_ref, _ = self._cam_ref
        return (h + (yaw_now - y_ref) / self.counts_per_deg) % 360.0

    def motion_bearing(self, now: float) -> "float | None":
        """Куда персонаж бежит по карте (за последние ~0.8 с); None — стоит или мало данных."""
        disp = self.odo.displacement(self.t.motion_window_s, now)
        if disp is None or math.hypot(disp[0], disp[1]) < self.t.motion_min_px:
            return None
        return bearing_deg(disp[0], disp[1])

    def estimate_heading(self, now: float, body: "tuple[float, float] | None",
                         turning: bool = False) -> "float | None":
        """
        Куда смотрит камера: направление сдвига на карте минус направление клавиш в теле
        (W — 0°, D — +90°, A — −90°, S — 180°). body — сдвиг в теле за то же окно от
        MovementController.body_delta. None — сдвинулись мало, клавиши не жали, или
        камера поворачивалась внутри окна (курс по дуге — не курс, и учиться на нём нельзя).
        """
        if self._turned_recently(now, turning, self.t.heading_window_s):
            return None
        if self._rotated(now, self.t.heading_window_s):
            return None
        disp = self.odo.displacement(self.t.heading_window_s, now)
        if disp is None or body is None:
            return None
        dx, dy, _ = disp
        bf, bs = body
        if math.hypot(dx, dy) < self.t.heading_min_px or math.hypot(bf, bs) < 0.3:
            return None
        h = (bearing_deg(dx, dy) - math.degrees(math.atan2(bs, bf))) % 360.0
        self.heading = h
        if self._yaw_hist:
            self._cam_ref = (h, self._yaw_hist[-1][1], now)
        self._learn(now, h)
        return h

    def _turned_recently(self, now: float, turning: bool, window_s: float) -> bool:
        """Камера поворачивается сейчас или кончила меньше window_s назад (окно замера — по дуге)."""
        if turning:
            self._was_turning = True
            return True
        if self._was_turning:
            self._was_turning = False
            self._turn_end_at = now
        return now - self._turn_end_at < window_s

    def _learn(self, now: float, h_after: float) -> None:
        """Сверка прошлого поворота: просили C счётчиков, курс изменился на D° -> C/D на градус."""
        if self._pending_learn is None:
            return
        h0, counts, t_turn = self._pending_learn
        if self._turn_end_at < t_turn:
            return                                   # поворот ещё не доигран (курс после — ещё не измерен)
        self._pending_learn = None
        d = wrap180(h_after - h0)
        if abs(d) < self.t.learn_min_deg or (d > 0) != (counts > 0):
            return                                   # слишком малый или странный поворот — не учимся на нём
        lo, hi = self.t.counts_per_deg_range
        k_obs = min(hi, max(lo, counts / d))
        self._k_samples.append(k_obs)
        ks = sorted(self._k_samples)
        # Медиана замеров, а до трёх замеров — с оглядкой на стартовую оценку: первый же
        # кривой поворот (обход камня посреди) не должен перекосить все следующие.
        pool = ks if len(ks) >= 3 else ks + [self.t.counts_per_deg0]
        pool.sort()
        self.counts_per_deg = pool[len(pool) // 2] if len(pool) % 2 else 0.5 * (pool[len(pool) // 2 - 1] + pool[len(pool) // 2])
        self.turns_learned += 1
        gamepad_debug_logger.debug("NAV     поворот: просил %+.0f счётчиков, курс %+.0f° -> %.1f сч/° (итог %.1f)",
                                   counts, d, k_obs, self.counts_per_deg)

    def steer(self, now: float, heading: float) -> "float | None":
        """Нужен ли поворот к точке: счётчики мыши (+ вправо) или None."""
        if self.waypoint is None or now - self._last_turn_at < self.t.turn_cooldown_s:
            return None
        x, y = self.pos
        want = bearing_deg(self.waypoint[0] - x, self.waypoint[1] - y)
        err = wrap180(want - heading)
        if abs(err) < self.t.turn_tol_deg:
            return None
        counts = err * self.counts_per_deg
        self._last_turn_at = now
        if abs(err) >= self.t.learn_min_deg:
            self._pending_learn = (heading, counts, now)
        return counts

    def turn_counts(self, deg: float) -> float:
        return deg * self.counts_per_deg

    # --- застревание ---
    def is_stuck(self, now: float, moving_since: "float | None",
                 body: "tuple[float, float] | None" = None, turning: bool = False) -> bool:
        """
        WASD зажаты дольше stuck_window_s, клавиши "толкали" хотя бы на stuck_min_body_s бега
        (body — сдвиг в теле за то же окно), а карта сдвинулась меньше stuck_min_m. Во время
        поворота камеры не судим: бег по дуге/развороту даёт малый итоговый сдвиг и без стены.
        """
        if self._turned_recently(now, turning, self.t.stuck_window_s):
            return False
        if moving_since is None or now - moving_since < self.t.stuck_window_s:
            return False
        if now - self._stuck_at < self.t.stuck_cooldown_s:
            return False
        if body is None or math.hypot(*body) < self.t.stuck_min_body_s:
            return False
        disp = self.odo.displacement(self.t.stuck_window_s, now)
        if disp is None:
            return False
        if math.hypot(disp[0], disp[1]) / self.px_per_m >= self.t.stuck_min_m:
            return False
        self._stuck_at = now
        return True

    def summary(self) -> str:
        x, y = self.pos
        return "позиция (%.0f, %.0f) px, до якоря %.0f м, масштаб %.2f px/м%s, %.1f сч/°, курс %s" % (
            x, y, self.dist_anchor_m(), self.px_per_m, "" if self.scale_known else " (оценка)",
            self.counts_per_deg, "?" if self.heading is None else "%.0f°" % self.heading)
