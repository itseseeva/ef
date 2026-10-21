"""
Наклон камеры (вверх/вниз): бот знает, под каким углом смотрит камера, и не уводит её в упор.

Проблема (2026-10-06, наблюдение игрока): камера часто "висит сверху" над персонажем —
это нижний упор наклона. Регулятор прицела тянул камеру вниз за близким мобом, упирался,
и мышь дальше слала сдвиг впустую: прицел так и оставался выше моба.

Решение — "одометр наклона" (счётчик), как у одометра колёс:
  - вертикаль камеры двигает только бот, значит, сумма всех отправленных dy = наклон;
  - точку отсчёта даёт сама игра: если отправить вниз заведомо больше полного диапазона,
    камера встанет в нижний упор — это p = 0 (калибровка, ~1.5 с);
  - дальше p считаем сами и держим в рабочем коридоре [lo, hi]: вниз ниже lo не пускаем.
    То, что регулятор хотел, но не получил, он "возвращает" себе (см. _AimController.cancel_y).

Модуль — чистая логика без потоков и Windows: на вход dt и dy, на выход dy. Им владеет поток
камеры (InputManager._aim_worker); из других потоков — только через тот же _io_lock.
Как проверить без игры: camera_pitch_test.py (модель игры с упором, без экрана).
"""
from __future__ import annotations

import math
import random

# Сколько счётчиков мыши на 1 градус: ScanTuning.full_turn_px = 6000 на 360°.
COUNTS_PER_DEG = 6000.0 / 360.0

# Коридор наклона в градусах ОТ НИЖНЕГО УПОРА (0 — камера смотрит сверху вниз до упора).
# lo: ниже не опускаем — там "вид сверху", прицел не достаёт близкого моба.
# hi: выше не поднимаем — там небо, мобов нет. home — куда ставим после калибровки.
# Подобрано без скриншотов: если камера после калибровки слишком низко/высоко — двигать home.
PITCH_LO_DEG = 25.0
PITCH_HI_DEG = 75.0
PITCH_HOME_DEG = 45.0

CAL_DOWN_DEG = 200.0               # вниз заведомо больше полного диапазона наклона (он < 180°)
CAL_DOWN_SPEED = (3200.0, 4200.0)  # счётчиков/с: быстрый, но плавный "взгляд вниз", ~0.8-1 с
CAL_UP_SPEED = (1100.0, 1500.0)    # подъём в коридор медленнее — как человек выставляет вид
CAL_EASE_S = 0.12                  # разгон/торможение жеста (не прямоугольный рывок)

# Поза для орлиного зрения (R, режим пул, 2026-10-14, от игрока: "скан применяется там, где курсор").
# Камера вниз до упора — прицел (центр экрана) на самом персонаже, скан накрывает всё вокруг него.
# Потом камера возвращается туда, где была. Тот же жест, что калибровка (разгон/торможение).
SCAN_MARGIN_DEG = 45.0             # вниз с большим запасом сверх известного наклона: одометр мог уплыть (игра
                                   # сама упирала камеру), а лишний сдвиг вниз игра просто съедает упором.
                                   # Было 12° — "камера иногда не совсем внизу" (2026-10-14)
SCAN_HOLD_MAX_S = 3.0              # страховка: в упоре дольше не стоим, даже если R так и не нажали
                                   # (R ждёт конца текущего нажатия — зажатие "=" и т.п.)
SCAN_DOWN_SPEED = (4800.0, 5800.0) # вниз под R — быстрый "кивок" (путь длиннее из-за запаса), с тем же разгоном
SCAN_UP_SPEED = (1800.0, 2400.0)   # назад быстрее калибровки: вид уже знакомый, ~0.3-0.4 с


class PitchGuard:
    def __init__(self, counts_per_deg: float = COUNTS_PER_DEG, rng: "random.Random | None" = None) -> None:
        self.k = counts_per_deg
        self.lo = PITCH_LO_DEG * counts_per_deg
        self.hi = PITCH_HI_DEG * counts_per_deg
        self.home = PITCH_HOME_DEG * counts_per_deg
        self.p: "float | None" = None          # счётчиков выше нижнего упора; None — не откалиброван
        self._rng = rng or random.Random()
        self._cal: "dict | None" = None        # идущая калибровка (фаза, сколько осталось, скорость)
        self.blocked_down_total = 0            # сколько счётчиков "вниз" не пустили (для лога)
        self.blocked_down = False              # в последнем тике регулятор упёрся в нижнюю границу
        self.calibrations = 0

    # --- учёт ---
    def applied(self, dy: int) -> None:
        """Отправили в камеру dy (+ вниз). Нижний упор есть и в игре: ниже 0 счётчик не уходит."""
        if self.p is not None and dy:
            self.p = max(0.0, self.p - dy)

    def allowed(self, dy: int) -> int:
        """Сколько из желаемого dy можно отправить, не выйдя из коридора. Не откалиброван — всё."""
        self.blocked_down = False
        if self.p is None or not dy:
            return dy
        if dy > 0:
            room = max(0, int(self.p - self.lo))           # сколько ещё можно вниз
            if dy > room:
                self.blocked_down = True
                self.blocked_down_total += dy - room
                return room
            return dy
        room = max(0, int(self.hi - self.p))               # сколько ещё можно вверх
        return max(dy, -room)

    # --- калибровка ---
    @property
    def calibrating(self) -> bool:
        return self._cal is not None

    def start_calibration(self) -> None:
        """Вниз до упора, потом вверх в home. Сам жест играет calib_step() потока камеры."""
        self._cal = {"phase": "down", "left": CAL_DOWN_DEG * self.k,
                     "speed": self._rng.uniform(*CAL_DOWN_SPEED), "t": 0.0}

    def start_scan(self) -> None:
        """Камера вниз до упора и стоять (фаза "hold"), пока не release_scan(); потом назад."""
        # Назад — в стандартный наклон home, а не "где был": после упора наклон известен точно, а "где был"
        # мог быть ошибкой одометра (лог 02:15:18: вернулись на 75° — край коридора, вид в небо).
        back = self.home
        left = (CAL_DOWN_DEG * self.k) if self.p is None else self.p + SCAN_MARGIN_DEG * self.k
        self._cal = {"phase": "down", "left": left, "speed": self._rng.uniform(*SCAN_DOWN_SPEED), "t": 0.0,
                     "scan": True, "back": back}

    @property
    def at_floor(self) -> bool:
        """Поза для R готова: камера в нижнем упоре и стоит."""
        return self._cal is not None and self._cal["phase"] == "hold"

    def release_scan(self, after_s: float = 0.0) -> None:
        """R нажат — через after_s камера идёт назад (нажатию дать дойти до игры в этой позе)."""
        c = self._cal
        if c is not None and c.get("scan"):
            if c["phase"] == "hold":
                c["hold_left"] = min(c["hold_left"], after_s)
            elif c["phase"] == "down":
                c["release_at_floor"] = True         # ещё едем вниз — в упоре сразу назад

    def abort_calibration(self) -> None:
        """Оборвали (появилась цель/пауза). На фазе "вниз" точку отсчёта не получили — p не трогаем."""
        self._cal = None

    def calib_step(self, dt: float) -> float:
        """
        Сдвиг камеры за тик калибровки (дробные счётчики; целые копит вызывающий).
        Скорость — с плавным разгоном (1 - e^(-t/tau)) и торможением у конца пути: жест
        выглядит как движение руки, а не как телепорт камеры.
        """
        c = self._cal
        if c is None:
            return 0.0
        if c["phase"] == "hold":
            c["hold_left"] -= dt
            if c["hold_left"] <= 0.0:
                self._cal = {"phase": "up", "left": c["back"], "speed": self._rng.uniform(*SCAN_UP_SPEED), "t": 0.0}
            return 0.0
        c["t"] += dt
        ease_in = 1.0 - math.exp(-c["t"] / CAL_EASE_S)
        ease_out = min(1.0, c["left"] / (c["speed"] * CAL_EASE_S * 2.0) + 0.15)
        step = min(c["left"], c["speed"] * ease_in * ease_out * dt)
        c["left"] -= step
        if c["phase"] == "down":
            if c["left"] <= 0.5:
                self.p = 0.0                               # стоим в нижнем упоре — точка отсчёта
                self.calibrations += 1
                if c.get("scan"):
                    hold = 0.0 if c.get("release_at_floor") else SCAN_HOLD_MAX_S
                    self._cal = {"phase": "hold", "hold_left": hold, "back": c["back"], "scan": True}
                else:
                    self._cal = {"phase": "up", "left": self.home, "speed": self._rng.uniform(*CAL_UP_SPEED), "t": 0.0}
            return step                                    # + вниз
        if c["left"] <= 0.5:
            self._cal = None
        return -step                                       # - вверх
