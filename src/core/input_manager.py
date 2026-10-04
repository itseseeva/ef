"""
src/core/input_manager.py

Единственный слой ввода бота. Теперь это КЛАВИАТУРА + МЫШЬ из Python
(раньше: виртуальный геймпад vgamepad/ViGEmBus, ещё раньше: Interception).

Цепочка вызовов:

    Vision -> Bot Logic (FSM) -> InputManager -> SendInput -> Windows -> T&L

Что НЕ изменилось (сознательно): публичный API класса. bot.py,
app_launcher.py и skills_config.py зовут те же методы — execute_combo,
press_button, aim_with_stick, clear_aim_target, change_target, flick_target,
cancel_pending_actions, halt_immediately, wait_idle, is_idle, stop. Менялось
только "железо" под ними. Имена вроде aim_with_stick остались историческими
(теперь aim_with_stick() просто принимает смещение цели от центра в px) — переименуем отдельным
рефакторингом, когда всё заработает.

Что изменилось:
  - Скиллы = ЛЮБАЯ клавиша из приложения (поле "slot" в skills_config.json):
    буквы, цифры, F1-F12, знаки, Space и т.д. — см. _SCANCODES и _KEY_ALIASES.
  - Камера = относительное движение МЫШИ. Наведение — пружина с
    демпфером (_AimController): плавный подход к цели без перелёта и
    медленный "дрейф" точки прицеливания вокруг цели. Все настройки — в
    AimTuning; коэффициент пересчёта px мыши -> px экрана измеряет
    `python smoke_test_input.py calibrate`.
  - Ходьба = WASD (метод move()).
  - Смена цели = Tab (change_target / flick_target, см. ниже).

Потоки (как и раньше, ДВА демон-потока):
  - _worker     — FIFO-очередь кнопок/комбо (порядок шагов важен).
  - _aim_worker — непрерывная камера БЕЗ очереди ("почтовый ящик" с
                  последней позицией цели), чтобы коррекция не ждала бой.

Backend (откуда реально уходит ввод) вынесен в маленькие классы ниже:
  - Win32Backend  — боевой: клавиши (скан-коды) и мышь через SendInput.
  - DryRunBackend — песочница: ничего не нажимает, только пишет журнал.
Менять способ отправки ввода нужно ТОЛЬКО в Win32Backend — остальной код
про конкретный способ отправки не знает.
"""

import ctypes
import logging
import math
import os
import queue
import random
import sys
import threading
import time
from collections import deque
from ctypes import wintypes
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Отладочный лог ввода. Раньше gamepad_debug.log — теперь input_debug.log
# (геймпада больше нет). Отдельный логгер с propagate=False и mode="w":
# файл перезаписывается при каждом запуске, в нём всегда самый свежий
# прогон, и он не засоряет общий консольный лог построчными нажатиями.
_INPUT_LOG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "input_debug.log"
)
input_debug_logger = logging.getLogger("input_debug")
input_debug_logger.setLevel(logging.DEBUG)
input_debug_logger.propagate = False
_input_log_handler = logging.FileHandler(_INPUT_LOG_PATH, mode="w", encoding="utf-8")
_input_log_handler.setFormatter(
    logging.Formatter("%(asctime)s.%(msecs)03d  %(message)s", datefmt="%H:%M:%S")
)
input_debug_logger.addHandler(_input_log_handler)

# Алиас под старое имя: bot.py и app_launcher.py импортируют
# gamepad_debug_logger. Чтобы не трогать их на этом этапе, оставляем то же
# имя, указывающее на новый логгер. Уберём вместе с переименованием API.
gamepad_debug_logger = input_debug_logger


# ======================================================================
# Чистые функции камеры (без состояния, легко тестируются отдельно)
# ======================================================================

def _ou_step(value: float, theta: float, sigma: float, dt: float) -> float:
    """
    Один шаг процесса Орнштейна-Уленбека — ограниченное случайное блуждание
    с возвратом к нулю. Используется как тремор руки: реальная рука дрожит
    ПЛАВНО (значение тика зависит от предыдущего), а не белым шумом, и не
    синусом (слишком периодичен — легко ловится частотным анализом).
    theta — сила возврата к нулю, sigma — размер случайной добавки.
    """
    return value + theta * (0.0 - value) * dt + sigma * math.sqrt(dt) * random.gauss(0.0, 1.0)


@dataclass(frozen=True)
class AimTuning:
    """
    Все настройки наведения камеры в ОДНОМ объекте.

    dataclass(frozen=True) — идиома Python для "набора констант с
    именами": нет ручного __init__, есть красивый repr для логов, а
    frozen=True запрещает менять поля после создания. Это важно, потому
    что объект читает поток камеры, а создан он в другом потоке: неизменяемый
    объект нельзя случайно "подкрутить" на лету и получить гонку данных.

    Единицы: "экранные px" — пиксели ИЗОБРАЖЕНИЯ (те, в которых Vision
    считает offset цели), "px мыши" — единицы, которые уходят в SendInput.
    """

    # Жёсткость пружины, рад/с. Больше — камера догоняет цель быстрее, но
    # резче. Время успокоения ~ 4/omega: при 7.0 это ~0.6 с. Это ГЛАВНАЯ
    # ручка "плавность против скорости реакции".
    omega: float = 7.0

    # Потолки скорости и ускорения камеры (экранные px/с и px/с²). Нужны
    # для больших ошибок (первый захват цели, 400+ px): без потолка
    # пружина дала бы мгновенный рывок. Ускорение ограничено — значит,
    # разгон и торможение всегда плавные.
    max_speed: float = 1200.0
    max_accel: float = 3000.0

    # Какую долю ускорения резервируем под ТОРМОЖЕНИЕ. Скорость камеры
    # дополнительно ограничена кривой sqrt(2 * a * расстояние): это
    # скорость, с которой ещё можно остановиться у цели, тормозя с
    # ускорением max_accel * brake_fraction. Без этого пружина на больших
    # ошибках (400+ px) разгонялась бы слишком сильно и пролетала цель.
    # Меньше значение — осторожнее торможение, но медленнее подход.
    brake_fraction: float = 0.5

    # КОЭФФИЦИЕНТ ПЕРЕСЧЁТА: на сколько ЭКРАННЫХ px сдвигается цель при
    # движении мыши на 1 px. Зависит от чувствительности мыши в игре и
    # разрешения. Точное значение даёт `python smoke_test_input.py
    # calibrate`. Если ошибиться: заниженное значение делает камеру
    # резче (возможен лёгкий перелёт), завышенное — вялее, но безопасно.
    # Поэтому стартовое значение сознательно завышено.
    gain_x: float = 2.0
    gain_y: float = 2.0

    # Задержка конвейера "кадр игры -> наш offset", секунды (захват
    # экрана + кадр игры на дисплее + обработка). Нужна для компенсации
    # запаздывания измерения, см. _AimController.measure().
    vision_latency_s: float = 0.05

    # Если свежего измерения нет дольше этого времени — камеру не тянем
    # по устаревшим данным, она спокойно останавливается.
    stale_after_s: float = 0.6

    # "Дыхание вокруг цели": целимся не в центр моба, а в точку, которая
    # медленно блуждает рядом. Размах (стандартное отклонение, экранные
    # px) по X и по Y; по Y меньше — вертикальные уходы заметнее.
    # Указан размах ДО сглаживания; после фильтра wander_smooth_s реальный
    # размах ~25% меньше (около 13 px по X и 7 px по Y).
    wander_std: "tuple[float, float]" = (17.0, 9.0)
    # Постоянная времени блуждания, секунды. Чем больше — тем медленнее
    # и плавнее дрейф.
    wander_tau_s: float = 2.5
    # Сглаживание самой точки дрейфа (постоянная времени фильтра, секунды).
    # Голый OU-процесс на каждом тике получает случайный толчок, поэтому
    # его СКОРОСТЬ — это шум (дрожание). Пропускаем его через RC-фильтр
    # (экспоненциальное сглаживание): дрейф становится плавным и без
    # дрожи, размах почти не меняется.
    wander_smooth_s: float = 0.4
    # Размах дрейфа периодически перевыбирается: "собранность" игрока
    # не постоянна, то он ведёт цель точнее, то свободнее.
    wander_amp_range: "tuple[float, float]" = (0.6, 1.4)
    wander_amp_reroll_s: "tuple[float, float]" = (4.0, 8.0)

    # --- ПОВОРОТ К НОВОМУ МОБУ (adaptive=True, фаза APPROACH; 2026-10-03) ---
    # Камера TL вращается ВОКРУГ ПЕРСОНАЖА, а не вокруг себя. Поэтому моб в паре
    # метров от персонажа при повороте едет по экрану в 3-5 раз МЕДЛЕННЕЕ
    # дальнего (параллакс орбитальной камеры). Лог 16:38:44: регулятор "довернул"
    # ~800 px, а бар близкого моба сдвинулся на 150 — подход к ближнему мобу
    # занимал 4-6 с. Поэтому при повороте к новому мобу регулятор на лету
    # измеряет r = "на сколько px сдвинулась цель / на сколько px мы повернули"
    # (у дальнего моба r~1, у моба рядом 0.2-0.3) и крутит камеру в r раз дальше.
    # Вес нового замера r в скользящем среднем (EMA): 0.35 — за 3-4 кадра r
    # доходит до истинного, а одиночный шумный кадр сдвигает его слабо.
    adapt_alpha: float = 0.35
    # Замер r — только если между двумя кадрами камера повернулась хотя бы на
    # столько px: на малых поворотах шум детекции (1-2 px) даёт мусорный r.
    adapt_min_turn_px: float = 12.0
    # Полная задержка "поворот мыши -> кадр зрения" для замера r, секунды
    # (захват mss + кадр игры на экране + обработка ~ 0.1 с). Больше, чем
    # vision_latency_s: там занижение безопасно (камера вялее), а здесь
    # занижение задержки занижает r и даёт перелёт.
    adapt_latency_s: float = 0.1
    # Границы r. Замер вне (0.05, 2.0) — выброс (захват прыгнул на соседний бар,
    # моб резко побежал, повтор того же кадра) — его просто пропускаем.
    adapt_r_min: float = 0.15
    adapt_r_max: float = 1.0
    # Во сколько раз потолки скорости/ускорения выше при повороте к новому мобу:
    # это не слежение за целью, а "взгляд в сторону" — человек делает его
    # быстрее (1200 px/с * 1.6 ~ 110 град/с при FOV ~90). Плавность та же:
    # разгон и торможение ограничены, перелёта нет (см. orbit-симуляцию в чате).
    approach_speed_mult: float = 1.6
    # Мягкое ведение (gentle=True, 2026-10-04): цель, найденная плавным осмотром после
    # клика по списку, доводится в прицел вдвое медленнее обычного — без рывков.
    gentle_speed_mult: float = 0.8


class _AimController:
    """
    Регулятор камеры: пружина с демпфером (PD-регулятор) + медленный дрейф
    точки прицеливания. Чистая логика БЕЗ потоков и без Windows — получает
    измерения и время, отдаёт целые пиксели мыши. Поэтому его можно
    проверять на симуляции (см. _simulate_aim ниже), не заходя в игру.

    Идея. Пусть ошибка err = (где цель на экране) - (куда хотим её
    поставить). Хотим, чтобы камера "тянула" цель к точке прицеливания:
      ускорение = omega^2 * err  -  2 * omega * скорость
    Первое слагаемое — пружина (тянет к цели тем сильнее, чем дальше),
    второе — демпфер (гасит скорость, чтобы не проскочить). С
    коэффициентом 2*omega демпфирование КРИТИЧЕСКОЕ: самый быстрый подход
    без единого качания. Скорость и ускорение дополнительно ограничены.

    Что изменилось по сравнению со старой моделью "сила стика по
    кривой":
      - нет мёртвой зоны и вогнутой кривой (они были заплаткой под
        геймпад; у мыши мёртвой зоны нет, и именно они давали перелёт и
        качание вокруг цели);
      - между свежими кадрами зрения регулятор сам вычитает то, на сколько
        повернул камеру (dead reckoning, "счисление пути") — не гонит камеру
        по устаревшему offset;
      - когда приходит измерение, из него вычитается поворот, сделанный
        за время задержки конвейера (vision_latency_s) — иначе камера
        "не знала" о собственном движении последних ~50 мс и перелетала.
    Потоково-небезопасен намеренно: им владеет ТОЛЬКО поток камеры.
    """

    def __init__(self, tuning: AimTuning, invert_y: bool = False) -> None:
        self._t = tuning
        self._mouse_sign = (1.0, -1.0 if invert_y else 1.0)
        # deque(maxlen=...) — очередь фиксированной длины: старые записи
        # выпадают сами, память не растёт, чистить вручную не нужно.
        self._history: "deque[tuple[float, float, float]]" = deque(maxlen=64)
        self.reset()

    def reset(self) -> None:
        """Полная остановка: нулевая скорость, дрейф и остатки пикселей."""
        self._est = [0.0, 0.0]     # оценка смещения цели от центра, экр. px
        self._vel = [0.0, 0.0]     # скорость "переноса цели", экр. px/с
        self._wander = [0.0, 0.0]  # сглаженный дрейф точки прицеливания
        self._wander_raw = [0.0, 0.0]  # "сырой" OU-процесс до сглаживания
        self._rem = [0.0, 0.0]     # дробные пиксели мыши (мышь ходит целыми)
        self._cum = [0.0, 0.0]     # сколько экранных px всего довернули
        self._since_measure = float("inf")
        self._amp = 1.0
        self._next_amp_at = 0.0
        self._history.clear()
        self._reset_adapt(False)

    def _reset_adapt(self, adaptive: bool) -> None:
        """r = 1 (как для дальней цели) и забыть прошлый кадр: новый замер r — с нуля."""
        self._gentle = getattr(self, "_gentle", False)
        self._adaptive = adaptive
        self._r = [1.0, 1.0]       # сдвиг цели на экране / наш поворот (см. AimTuning.adapt_*)
        self._prev_meas: "tuple[float, float, float, float] | None" = None

    def begin(self, now: float) -> None:
        """Начало захвата новой цели: дрейф стартует с нуля, размах — случайный."""
        self._wander = [0.0, 0.0]
        self._wander_raw = [0.0, 0.0]
        self._since_measure = float("inf")
        self._reroll_amp(now)
        self._reset_adapt(self._adaptive)

    def _reroll_amp(self, now: float) -> None:
        self._amp = random.uniform(*self._t.wander_amp_range)
        self._next_amp_at = now + random.uniform(*self._t.wander_amp_reroll_s)

    def _cum_at(self, when: float) -> "tuple[float, float]":
        """Накопленный поворот на момент `when` (по журналу шагов)."""
        found = None
        for t, cx, cy in self._history:
            if t <= when:
                found = (cx, cy)
            else:
                break
        if found is None:
            # журнал короче окна задержки — берём самое старое, что есть
            if self._history:
                _, cx, cy = self._history[0]
                return cx, cy
            return 0.0, 0.0
        return found

    def measure(self, dx: float, dy: float, t_meas: float, now: float, adaptive: bool = False,
                gentle: bool = False) -> None:
        """
        Свежее измерение от зрения: цель на (dx, dy) экранных px от центра.
        Кадр был снят около t_meas, но отражает мир на vision_latency_s
        раньше, поэтому часть нашего поворота в него ещё не попала —
        вычитаем её, иначе оценка завышена и камера перелетит.

        adaptive=True (поворот к новому мобу): по двум соседним кадрам меряем r —
        во сколько раз цель на экране сдвинулась меньше, чем мы повернули
        (параллакс орбитальной камеры, см. AimTuning.adapt_*). adaptive=False —
        r = 1, поведение ровно как раньше (бой, осмотр, калибровка).
        """
        self._gentle = gentle
        if adaptive != self._adaptive:
            self._reset_adapt(adaptive)
        cum_now = (self._cum[0], self._cum[1])
        cum_then = self._cum_at(t_meas - self._t.vision_latency_s)
        if adaptive:
            # Для замера r кадр привязываем к повороту с ПОЛНОЙ задержкой конвейера
            # (adapt_latency_s): иначе на разгоне "повернули" уже больше, чем кадр
            # успел показать, и r занижается -> перелёт у дальних мобов.
            cum_r = self._cum_at(t_meas - self._t.adapt_latency_s)
            if self._prev_meas is not None:
                pdx, pdy, pcx, pcy = self._prev_meas
                # (на сколько цель приблизилась к прицелу) / (на сколько повернули за то же время)
                self._update_r(0, pdx - dx, cum_r[0] - pcx)
                self._update_r(1, pdy - dy, cum_r[1] - pcy)
            self._prev_meas = (dx, dy, cum_r[0], cum_r[1])
        self._est[0] = dx - self._r[0] * (cum_now[0] - cum_then[0])
        self._est[1] = dy - self._r[1] * (cum_now[1] - cum_then[1])
        self._since_measure = 0.0

    def _update_r(self, i: int, moved: float, turned: float) -> None:
        """Один замер r по оси i и EMA-сглаживание (см. AimTuning.adapt_*)."""
        t = self._t
        if abs(turned) < t.adapt_min_turn_px:
            return                          # повернули мало — шум детекции больше сигнала
        sample = moved / turned
        if not (0.05 <= sample <= 2.0):
            return                          # выброс: прыжок захвата / тот же кадр / моб рванул
        r = self._r[i] + t.adapt_alpha * (sample - self._r[i])
        self._r[i] = min(t.adapt_r_max, max(t.adapt_r_min, r))

    def step(self, dt: float, engaged: bool, now: float) -> "tuple[int, int]":
        """
        Один тик. engaged=False — цели нет: ошибка считается нулевой, и
        демпфер сам плавно гасит остаточную скорость (камера не замирает
        рывком). Возвращает целые пиксели мыши (dx, dy) для SendInput.
        """
        t = self._t
        self._since_measure += dt
        usable = engaged and self._since_measure <= t.stale_after_s
        if engaged and now >= self._next_amp_at:
            self._reroll_amp(now)

        w2 = t.omega * t.omega
        gains = (t.gain_x, t.gain_y)
        # Регулятор работает в px ПОВОРОТА камеры (как для дальней цели, r=1), а не
        # в px экрана: ошибку на экране делим на r. Тогда пружина/потолки задают
        # угловую скорость камеры — как у человека, — независимо от того, близко
        # моб или далеко. Без adaptive r == 1 и всё ровно как раньше.
        mult = t.gentle_speed_mult if self._gentle else (t.approach_speed_mult if self._adaptive else 1.0)
        max_speed = t.max_speed * mult
        max_accel = t.max_accel * mult
        out = [0, 0]
        for i in (0, 1):
            if engaged:
                # Стандартное отклонение стационарного OU = sigma/sqrt(2*theta);
                # решаем обратное: из желаемого размаха получаем sigma.
                theta = 1.0 / t.wander_tau_s
                sigma = t.wander_std[i] * self._amp * math.sqrt(2.0 * theta)
                self._wander_raw[i] = _ou_step(self._wander_raw[i], theta, sigma, dt)
                # RC-фильтр: догоняем сырое значение с постоянной времени
                # wander_smooth_s (exp даёт верный результат при любом dt).
                alpha = 1.0 - math.exp(-dt / t.wander_smooth_s)
                self._wander[i] += (self._wander_raw[i] - self._wander[i]) * alpha
            else:
                self._wander_raw[i] = 0.0
                self._wander[i] = 0.0

            err = ((self._est[i] - self._wander[i]) / self._r[i]) if usable else 0.0
            accel = w2 * err - 2.0 * t.omega * self._vel[i]
            accel = max(-max_accel, min(max_accel, accel))
            vel = self._vel[i] + accel * dt
            # Потолок скорости: общий максимум И "ещё успею затормозить".
            cap = min(
                max_speed,
                math.sqrt(2.0 * max_accel * t.brake_fraction * abs(err)),
            )
            self._vel[i] = max(-cap, min(cap, vel))

            shift = self._vel[i] * dt        # на сколько px ПОВОРОТА довернули за тик
            self._est[i] -= self._r[i] * shift   # цель на экране сдвинулась в r раз меньше
            self._cum[i] += shift

            self._rem[i] += self._mouse_sign[i] * shift / gains[i]
            whole = int(self._rem[i])        # int() режет к нулю; остаток копится
            self._rem[i] -= whole
            out[i] = whole

        self._history.append((now, self._cum[0], self._cum[1]))
        return out[0], out[1]

    def freeze(self) -> None:
        """
        Камеру заморозили (идёт прокрутка колеса): гасим скорость и остатки пикселей,
        чтобы после разморозки не было "хвоста" движения. Оценку смещения
        цели не трогаем — свежее измерение придёт со следующим кадром зрения.
        """
        self._vel = [0.0, 0.0]
        self._rem = [0.0, 0.0]

    def snapshot(self) -> str:
        """Одна строка состояния для input_debug.log (подбор констант по логу)."""
        return (
            f"est=({self._est[0]:+.0f},{self._est[1]:+.0f}) "
            f"wander=({self._wander[0]:+.0f},{self._wander[1]:+.0f}) "
            f"vel=({self._vel[0]:+.0f},{self._vel[1]:+.0f})px/с"
            + (f" r=({self._r[0]:.2f},{self._r[1]:.2f})" if self._adaptive else "")
        )


@dataclass(frozen=True)
class ScanTuning:
    """
    Настройки "осмотра местности" камерой, когда цели нет (фаза SCAN в SEARCH).
    Все расстояния — в ПИКСЕЛЯХ МЫШИ (то, что уходит в SendInput), а не в
    экранных: сканеру не нужно знать, где что на экране, он просто крутит
    камеру. Поэтому ему не нужна и калибровка gain_x/gain_y — только чтобы
    общий размах осмотра (в градусах) был разумным.
    """

    # КРУГОВОЙ ОБЗОР. Камера поворачивается по кругу в одну сторону
    # сериями взмахов с паузами; набрав полный оборот (full_turn_px),
    # с вероятностью reverse_prob меняет сторону вращения. Раньше камера
    # качалась влево-вправо около стартового курса и никогда не заглядывала
    # назад (персонаж у стены — камера всё время смотрела в стену).
    #
    # full_turn_px — сколько px МЫШИ даёт ровно 360° поворота камеры в игре.
    # Это ОЦЕНКА: зависит от чувствительности мыши в игре. Подбор: запусти
    # `python smoke_test_input.py turn` — камера сделает оборот на это число
    # px; если вид не вернулся в исходную точку, поправь число (мало
    # повернулась -> увеличь, перелетела -> уменьши).
    full_turn_px: float = 6000.0
    reverse_prob: float = 0.5

    # Длина одного взмаха — доля полного оборота (0.12 = 43°, 0.30 = 108°).
    # Взмах всегда достаточно большой, чтобы осмотр шёл заметно, но не
    # настолько, чтобы проскакивать мимо мобов в кадре.
    sweep_turn_range: "tuple[float, float]" = (0.12, 0.30)

    # Иногда (с вероятностью nudge_prob) вместо взмаха — короткий возврат
    # назад на nudge_range px, как человек, который "заметил что-то
    # краем глаза и подвёл камеру обратно". Не засчитывается в оборот.
    nudge_prob: float = 0.15
    nudge_range: "tuple[float, float]" = (100.0, 260.0)

    # Вертикаль: небольшие уходы, плавно возвращающиеся к исходному наклону.
    limit_y: float = 35.0

    # Средняя скорость взмаха (px мыши/с) — выбирается случайно на КАЖДЫЙ
    # взмах. Пиковая скорость профиля minimum-jerk в 1.875 раза выше средней.
    # Скорость подобрана под круговой обзор: полный оборот за ~8-14 с
    # (медиана ~10 с; в симуляции, см. _simulate_scan). Было 320-500 px/с и
    # более длинные паузы — оборот занимал 16-29 с (медиана 19), что при
    # любом обрыве осмотра выглядело как "камера не крутится на 360".
    # Ограничение сверху: в режиме "только панель" мобов находит Tab (раз в
    # 2-3 с), а не бары в кадре, поэтому между двумя Tab камера не должна
    # уходить дальше поля зрения (~90-100°). При ~36°/с средней скорости это
    # ~90° за 2.5 с — на пределе; если мобы пропускаются, снижай скорость
    # (или сокращай _SCAN_TAB_PANEL_ONLY_S в bot.py).
    speed_range: "tuple[float, float]" = (650.0, 950.0)

    # Пауза "присмотрелся" между взмахами: обычно короткая, иногда (с
    # вероятностью long_pause_prob) длинная — человек то водит камерой
    # непрерывно, то на секунду замирает. Две моды, а не равномерный
    # разброс, — по той же причине, что и у пауз между скиллами.
    pause_range: "tuple[float, float]" = (0.10, 0.45)
    long_pause_range: "tuple[float, float]" = (0.7, 1.2)
    long_pause_prob: float = 0.12

    # Вероятность, что у взмаха есть вертикальная составляющая. Без неё
    # вертикаль плавно возвращается к исходному наклону.
    vertical_prob: float = 0.5

    # Постоянная времени торможения при остановке скана (с): скорость
    # камеры затухает экспонентой, а не обрывается за один тик.
    stop_tau_s: float = 0.08

    # Мелкий тремор поверх взмаха (OU-процесс): размах в px мыши и
    # постоянная времени. Идеально гладкая кривая — тоже отпечаток бота.
    tremor_std: float = 1.5
    tremor_tau_s: float = 0.35


class _ScanController:
    """
    Генератор "хаотичного, но человекоподобного" осмотра: серия взмахов
    камерой влево-вправо с паузами. Чистая логика без потоков и без
    Windows (как и _AimController): получает dt, отдаёт целые px мыши —
    поэтому проверяется в песочнице (см. _simulate_scan).

    Как устроен ОДИН взмах. Выбираем случайную конечную точку (положение
    камеры относительно стартового курса, в px мыши) и едем к ней по
    кривой "minimum-jerk" (минимальный рывок):
        s(τ) = 10τ³ − 15τ⁴ + 6τ⁵,   τ = t / T ∈ [0, 1]
    Это стандартная модель движения руки человека к цели: нулевая
    скорость И нулевое ускорение в начале и в конце, колоколообразный
    профиль скорости. Линейное движение или резкий старт сразу видны на
    графике скорости, а у этой кривой — нет.

    Круговой обзор: взмахи идут в ОДНУ сторону, каждый на 12-30% полного
    оборота (full_turn_px), между ними паузы; камера охватывает все 360°,
    в том числе то, что за спиной (важно, когда персонаж стоит лицом к
    стене). Набрав полный оборот, осмотр с вероятностью reverse_prob
    меняет сторону вращения. Редкие короткие возвраты (nudge) и тремор
    добавляют нерегулярности.

    Остановка (active=False) не обрывает движение: скорость гасится
    экспонентой за ~0.1-0.2 с. Это важно в момент, когда мы нашли бар:
    камера не должна встать как вкопанная и тут же рвануть к цели.

    Потоково-небезопасен намеренно: им владеет только поток камеры.
    """

    def __init__(self, tuning: ScanTuning) -> None:
        self._t = tuning
        self.reset()

    def reset(self) -> None:
        """Полный сброс (паника, остановка потока)."""
        self._running = False
        self._pos = [0.0, 0.0]       # идеальная траектория, px мыши от курса старта
        self._p0 = [0.0, 0.0]        # начало текущего взмаха
        self._p1 = [0.0, 0.0]        # конец текущего взмаха
        self._sweep_t = 0.0
        self._sweep_T = 1.0
        self._in_sweep = False
        self._pause_left = 0.0
        # Сторона вращения (+1 вправо, -1 влево): случайная при каждом
        # старте осмотра; после полного оборота может смениться.
        self._dir = random.choice((-1.0, 1.0))
        self._turn_done = 0.0          # сколько px уже прошли в текущую сторону
        self._tremor = [0.0, 0.0]
        self._last_total = [0.0, 0.0]  # идеальная позиция + тремор на прошлом тике
        self._vel = [0.0, 0.0]         # скорость на прошлом тике, px мыши/с
        self._rem = [0.0, 0.0]         # дробные пиксели (мышь ходит целыми)
        self.sweeps_done = 0

    @property
    def running(self) -> bool:
        return self._running

    def _begin(self) -> None:
        """Старт осмотра: текущий курс камеры становится "нулём" (домом)."""
        self.reset()
        self._running = True
        # Небольшая пауза перед первым взмахом: реакция "ага, надо искать".
        self._pause_left = random.uniform(0.1, 0.4)

    def _start_sweep(self) -> None:
        t = self._t
        px, py = self._pos
        if self.sweeps_done > 0 and random.random() < t.nudge_prob:
            # Короткий возврат назад (против вращения). Вычитаем его из
            # счётчика оборота: счётчик должен отражать ЧИСТЫЙ поворот, иначе
            # из-за возвратов полный круг в реальности не доходил бы до 360°.
            back = random.uniform(*t.nudge_range)
            tx = px - self._dir * back
            self._turn_done = max(0.0, self._turn_done - back)
        else:
            # Взмах по кругу в текущую сторону на долю полного оборота.
            length = random.uniform(*t.sweep_turn_range) * t.full_turn_px
            tx = px + self._dir * length
            self._turn_done += length
            if self._turn_done >= t.full_turn_px:
                # Полный круг пройден: счётчик обнуляем и, возможно, крутим
                # дальше в другую сторону — осмотр не должен быть
                # предсказуемым по направлению.
                self._turn_done -= t.full_turn_px
                if random.random() < t.reverse_prob:
                    self._dir = -self._dir

        if random.random() < t.vertical_prob:
            ty = random.uniform(-t.limit_y, t.limit_y)
        else:
            ty = py * 0.5  # вертикаль потихоньку возвращается к исходному наклону

        dist = math.hypot(tx - px, ty - py)
        speed = random.uniform(*t.speed_range)
        self._p0 = [px, py]
        self._p1 = [tx, ty]
        self._sweep_T = max(0.35, dist / speed)
        self._sweep_t = 0.0
        self._in_sweep = True

    def _start_pause(self) -> None:
        t = self._t
        if random.random() < t.long_pause_prob:
            self._pause_left = random.uniform(*t.long_pause_range)
        else:
            self._pause_left = random.uniform(*t.pause_range)
        self._in_sweep = False

    def _quantize(self, fx: float, fy: float) -> "tuple[int, int]":
        """Дробные px -> целые, остаток копится (мышь двигается только целыми)."""
        out = [0, 0]
        for i, v in enumerate((fx, fy)):
            self._rem[i] += v
            whole = int(self._rem[i])  # int() режет к нулю, остаток не теряется
            self._rem[i] -= whole
            out[i] = whole
        return out[0], out[1]

    def step(self, dt: float, active: bool) -> "tuple[int, int]":
        """
        Один тик. active=True — осматриваемся; False — плавно гасим остаток
        движения и замираем. Возвращает целые px мыши (dx, dy).
        """
        t = self._t

        if not active:
            if not self._running:
                return 0, 0
            decay = math.exp(-dt / t.stop_tau_s)
            fx, fy = self._vel[0] * dt, self._vel[1] * dt
            self._vel[0] *= decay
            self._vel[1] *= decay
            if math.hypot(self._vel[0], self._vel[1]) < 3.0:
                self._running = False
                self._vel = [0.0, 0.0]
            return self._quantize(fx, fy)

        if not self._running:
            self._begin()

        if self._in_sweep:
            self._sweep_t += dt
            tau = min(self._sweep_t / self._sweep_T, 1.0)
            s = tau * tau * tau * (10.0 - 15.0 * tau + 6.0 * tau * tau)
            self._pos = [
                self._p0[0] + (self._p1[0] - self._p0[0]) * s,
                self._p0[1] + (self._p1[1] - self._p0[1]) * s,
            ]
            if tau >= 1.0:
                self.sweeps_done += 1
                self._start_pause()
        else:
            self._pause_left -= dt
            if self._pause_left <= 0.0:
                self._start_sweep()

        # Тремор: OU-процесс (см. _ou_step). Стационарный размах tremor_std
        # получаем из sigma = std * sqrt(2 * theta).
        theta = 1.0 / t.tremor_tau_s
        sigma = t.tremor_std * math.sqrt(2.0 * theta)
        for i in (0, 1):
            self._tremor[i] = _ou_step(self._tremor[i], theta, sigma, dt)

        total = [self._pos[0] + self._tremor[0], self._pos[1] + self._tremor[1]]
        fx = total[0] - self._last_total[0]
        fy = total[1] - self._last_total[1]
        self._last_total = total
        self._vel = [fx / dt, fy / dt]
        return self._quantize(fx, fy)

    def freeze(self) -> None:
        """
        Камеру заморозили (идёт прокрутка колеса): обнуляем скорость и, если шёл
        взмах, обрываем его паузой — после разморозки новый взмах начнётся
        плавно с нуля, а не продолжится с середины колокола скорости.
        """
        self._vel = [0.0, 0.0]
        self._rem = [0.0, 0.0]
        if self._running and self._in_sweep:
            self._in_sweep = False
            self._pause_left = random.uniform(0.15, 0.4)

    def snapshot(self) -> str:
        """Одна строка состояния для input_debug.log."""
        phase = "взмах" if self._in_sweep else "пауза"
        return (
            f"{phase} pos=({self._pos[0]:+.0f},{self._pos[1]:+.0f}) "
            f"взмахов={self.sweeps_done}"
        )


# ======================================================================
# Backend: как ввод физически уходит в Windows
# ======================================================================

# Структуры WinAPI для SendInput. Описываем их вручную через ctypes (без
# сторонних библиотек): pyautogui умеет двигать курсор только АБСОЛЮТНО
# (SetCursorPos), а игра с захваченным курсором читает ОТНОСИТЕЛЬНЫЕ дельты
# мыши — для камеры нужен именно SendInput c MOUSEEVENTF_MOVE.
_ULONG_PTR = ctypes.c_size_t


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", _ULONG_PTR),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", _ULONG_PTR),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUT_UNION(ctypes.Union):
    # Все три варианта обязаны быть в union: размер INPUT определяется
    # самым большим, и SendInput сверяет его с sizeof(INPUT).
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUT_UNION)]


_INPUT_MOUSE = 0
_MOUSEEVENTF_MOVE = 0x0001
_MOUSEEVENTF_LEFTDOWN, _MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
_MOUSEEVENTF_RIGHTDOWN, _MOUSEEVENTF_RIGHTUP = 0x0008, 0x0010
_MOUSEEVENTF_MIDDLEDOWN, _MOUSEEVENTF_MIDDLEUP = 0x0020, 0x0040
_MOUSEEVENTF_WHEEL = 0x0800
_MOUSEEVENTF_ABSOLUTE = 0x8000     # dx/dy — абсолютная точка экрана 0..65535 (основной монитор)
_WHEEL_DELTA = 120          # один "щелчок" колеса в Windows

class _CURSORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("hCursor", wintypes.HANDLE), ("ptScreenPos", wintypes.POINT)]


_CURSOR_SHOWING = 0x0001
_INPUT_KEYBOARD = 1
_KEYEVENTF_EXTENDEDKEY = 0x0001
_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_SCANCODE = 0x0008


def _build_scancodes() -> "dict[str, tuple[int, bool]]":
    """
    Имя клавиши -> (скан-код Set 1, extended). Скан-код — номер ФИЗИЧЕСКОЙ
    клавиши, а не символа: P остаётся P и на русской раскладке (где она
    печатает "з"), и игра получает ровно ту кнопку, что забиндена. Именно
    скан-коды читают игры (DirectInput / Raw Input). extended=True — клавиши
    из "серого" блока (стрелки, Insert...), им нужен флаг EXTENDEDKEY.
    Собрано функцией, а не литералом: ряды клавиатуры идут подряд по кодам,
    zip по строке ряда короче и не даёт опечататься в 40 числах.
    """
    sc: "dict[str, tuple[int, bool]]" = {}
    # Ряды основной клавиатуры: первый символ ряда -> его скан-код, дальше +1.
    for row, first in (("1234567890-=", 0x02), ("qwertyuiop[]", 0x10),
                       ("asdfghjkl;'`", 0x1E), ("\\zxcvbnm,./", 0x2B)):
        for i, ch in enumerate(row):
            sc[ch] = (first + i, False)
    for i in range(10):                                  # F1..F10 подряд с 0x3B
        sc[f"f{i + 1}"] = (0x3B + i, False)
    sc["f11"], sc["f12"] = (0x57, False), (0x58, False)
    sc.update({
        "esc": (0x01, False), "backspace": (0x0E, False), "tab": (0x0F, False),
        "enter": (0x1C, False), "ctrl": (0x1D, False), "shift": (0x2A, False),
        "alt": (0x38, False), "space": (0x39, False), "capslock": (0x3A, False),
        "arrowup": (0x48, True), "arrowdown": (0x50, True),
        "arrowleft": (0x4B, True), "arrowright": (0x4D, True),
        "insert": (0x52, True), "delete": (0x53, True), "home": (0x47, True),
        "end": (0x4F, True), "pageup": (0x49, True), "pagedown": (0x51, True),
    })
    return sc


_SCANCODES = _build_scancodes()

# Как клавишу может записать приложение -> каноническое имя из _SCANCODES.
# Приложение берёт символ нажатой клавиши (event.key), поэтому сюда попадают:
#   - русская раскладка (ЙЦУКЕН): "З" — это физическая P, "Э" — апостроф;
#   - символы с Shift: "!" — это клавиша 1, "+" — клавиша =;
#   - длинные имена, обрезанные приложением до 5 букв ("PAGEUP" -> "PAGEU").
_KEY_ALIASES: "dict[str, str]" = {
    **dict(zip("йцукенгшщзхъфывапролджэячсмитьбюё", "qwertyuiop[]asdfghjkl;'zxcvbnm,.`")),
    **dict(zip('!@#$%^&*()_+:"<>?{}|~№', "1234567890-=;',./[]\\`3")),
    "escape": "esc", "control": "ctrl", "spacebar": "space", "return": "enter",
    "pageu": "pageup", "paged": "pagedown", "inser": "insert", "delet": "delete",
    "capsl": "capslock", "backs": "backspace",
}


class Win32Backend:
    """
    Боевой backend (только Windows).

    Всё — через SendInput напрямую (без pyautogui, 2026-10-03):
      - клавиши — СКАН-КОДАМИ (KEYEVENTF_SCANCODE): физическая клавиша, не
        зависит от раскладки (pyautogui слал виртуальный код, который на
        русской раскладке для букв мог не найтись);
      - мышь — относительными сдвигами и кнопками (SetCursorPos не нужен).

    Как проверить локально: запусти smoke_test_input.py (лежит рядом с
    main.py) с открытым блокнотом — убедишься, что клавиши и мышь вообще
    уходят, ДО того как идти в игру.
    """

    _MOUSE_BUTTON_FLAGS = {
        ("left", True): _MOUSEEVENTF_LEFTDOWN,
        ("left", False): _MOUSEEVENTF_LEFTUP,
        ("right", True): _MOUSEEVENTF_RIGHTDOWN,
        ("right", False): _MOUSEEVENTF_RIGHTUP,
        ("middle", True): _MOUSEEVENTF_MIDDLEDOWN,
        ("middle", False): _MOUSEEVENTF_MIDDLEUP,
    }

    def __init__(self) -> None:
        # use_last_error=True — чтобы при сбое SendInput можно было
        # достать код ошибки Windows через ctypes.get_last_error().
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int)
        self._user32.SendInput.restype = wintypes.UINT

    def key_down(self, key: str) -> None:
        self._send_key(key, up=False)

    def key_up(self, key: str) -> None:
        self._send_key(key, up=True)

    def _send_key(self, key: str, up: bool) -> None:
        """
        Одна клавиша скан-кодом. Имя — каноническое из _SCANCODES (его даёт
        InputManager.parse_combo_string). Как проверить без игры:
        smoke_test_input.py с открытым Блокнотом — на английской раскладке
        P печатает "p", на русской "з" (это и есть та же физическая клавиша).
        """
        entry = _SCANCODES.get(key)
        if entry is None:
            logger.error("Win32Backend: клавиши '%s' нет в таблице скан-кодов — не нажата.", key)
            return
        code, extended = entry
        inp = _INPUT()
        inp.type = _INPUT_KEYBOARD
        inp.ki.wVk = 0                       # при SCANCODE виртуальный код игнорируется
        inp.ki.wScan = code
        inp.ki.dwFlags = (_KEYEVENTF_SCANCODE
                          | (_KEYEVENTF_EXTENDEDKEY if extended else 0)
                          | (_KEYEVENTF_KEYUP if up else 0))
        sent = self._user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))
        if sent != 1:
            logger.error("SendInput (клавиша %s) вернул %d (ошибка Windows %d).",
                         key, sent, ctypes.get_last_error())

    def mouse_button(self, button: str, down: bool) -> None:
        self._send_mouse(self._MOUSE_BUTTON_FLAGS[(button, down)])

    def mouse_move_rel(self, dx: int, dy: int) -> None:
        self._send_mouse(_MOUSEEVENTF_MOVE, dx, dy)

    def cursor_pos(self) -> "tuple[int, int]":
        """Где сейчас курсор Windows (px экрана)."""
        pt = wintypes.POINT()
        self._user32.GetCursorPos(ctypes.byref(pt))
        return pt.x, pt.y

    def cursor_visible(self) -> "bool | None":
        """
        Виден ли курсор Windows сейчас (GetCursorInfo, флаг CURSOR_SHOWING). Игра прячет
        его, пока мышь крутит камеру, и показывает, пока зажат Alt. None — не узнали.
        """
        ci = _CURSORINFO()
        ci.cbSize = ctypes.sizeof(_CURSORINFO)
        if not self._user32.GetCursorInfo(ctypes.byref(ci)):
            return None
        return bool(ci.flags & _CURSOR_SHOWING)

    def mouse_move_abs(self, x: float, y: float) -> None:
        """
        Курсор в точку (x, y) экрана — абсолютным SendInput, а не относительным:
        относительный сдвиг Windows искажает "повышенной точностью указателя",
        и курсор не попал бы в строку списка. 0..65535 — шкала основного монитора.
        """
        w = self._user32.GetSystemMetrics(0) or 1920
        h = self._user32.GetSystemMetrics(1) or 1080
        nx = int(round(max(0.0, min(x, w - 1)) * 65535 / max(1, w - 1)))
        ny = int(round(max(0.0, min(y, h - 1)) * 65535 / max(1, h - 1)))
        self._send_mouse(_MOUSEEVENTF_MOVE | _MOUSEEVENTF_ABSOLUTE, nx, ny)

    def mouse_wheel(self, delta: int) -> None:
        """Колесо: delta < 0 — вниз (в игре — камера отдаляется), кратно 120 на щелчок."""
        self._send_mouse(_MOUSEEVENTF_WHEEL, data=delta)

    def _send_mouse(self, flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> None:
        inp = _INPUT()
        inp.type = _INPUT_MOUSE
        inp.mi.dx = dx
        inp.mi.dy = dy
        # mouseData — DWORD (без знака): отрицательное "вниз" кладём дополнительным
        # кодом, & 0xFFFFFFFF; Windows сама прочтёт его как знаковое.
        inp.mi.mouseData = data & 0xFFFFFFFF
        inp.mi.dwFlags = flags
        sent = self._user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))
        if sent != 1:
            logger.error("SendInput вернул %d (ошибка Windows %d).", sent, ctypes.get_last_error())


class DryRunBackend:
    """
    Песочница: НИЧЕГО не нажимает, только запоминает вызовы в self.calls.
    Позволяет проверить логику InputManager (порядок шагов, отпускание
    зажатого после паники, поведение камеры) без игры и без Windows.
    """

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def key_down(self, key: str) -> None:
        self.calls.append(("key_down", key))

    def key_up(self, key: str) -> None:
        self.calls.append(("key_up", key))

    def mouse_button(self, button: str, down: bool) -> None:
        self.calls.append(("mouse_down" if down else "mouse_up", button))

    def mouse_move_rel(self, dx: int, dy: int) -> None:
        self.calls.append(("mouse_move", dx, dy))

    _cursor = (960.0, 540.0)
    alt_shows_cursor = True        # песочница: как в игре, курсор виден, пока зажат Alt

    def cursor_pos(self) -> "tuple[float, float]":
        return self._cursor

    def cursor_visible(self) -> "bool | None":
        downs = sum(1 for c in self.calls if c == ("key_down", "alt"))
        ups = sum(1 for c in self.calls if c == ("key_up", "alt"))
        return self.alt_shows_cursor and downs > ups

    def mouse_move_abs(self, x: float, y: float) -> None:
        self._cursor = (x, y)
        self.calls.append(("mouse_abs", round(x, 1), round(y, 1)))

    def mouse_wheel(self, delta: int) -> None:
        self.calls.append(("mouse_wheel", delta))


# ======================================================================
# Раскладка: имя действия -> ("key", клавиша) | ("mouse", кнопка) | None
# ======================================================================

def _build_input_map() -> "dict[str, tuple[str, str] | None]":
    """
    Единственное место, где написано "какая клавиша что делает". Хочешь
    поменять бинд — правишь здесь, остальной код про конкретные клавиши
    не знает. Собрано функцией, а не литералом внутри класса: генераторы
    словарей в теле класса не видят другие атрибуты класса (особенность
    области видимости Python), а функция — обычная область, без сюрпризов.
    """
    mapping: "dict[str, tuple[str, str] | None]" = {}

    # ЛЮБАЯ клавиша из таблицы скан-кодов (2026-10-03: "какая кнопка в
    # приложении, та и жмётся"). Имя действия = имя клавиши, поэтому поле
    # "slot" из skills_config.json идёт в parse_combo_string как есть.
    # dict comprehension вместо цикла — одна строка, тот же результат.
    mapping.update({name: ("key", name) for name in _SCANCODES})
    # Кнопки мыши, если в приложении вписать LMB / RMB / MMB.
    mapping.update({"lmb": ("mouse", "left"), "rmb": ("mouse", "right"), "mmb": ("mouse", "middle")})

    # Семантические имена, которые использует bot.py:
    # "attack" — ПКМ по выбранной цели: персонаж бежит к ней и бьёт обычной
    # атакой (автоатака). Бот жмёт его при входе в COMBAT и после каста
    # цепочки, пока скиллы на кулдауне (см. bot.py).
    mapping["attack"] = ("mouse", "right")
    # "next_target" — выбрать следующую цель (Tab).
    mapping["next_target"] = ("key", "tab")
    # "target" — раньше R3 (Lock-On геймпада). На клавиатуре отдельного
    # "захвата" нет: цель берёт сам Tab. None = осознанная пустышка:
    # bot.py по-прежнему зовёт execute_combo(["press_target"]) при входе
    # в COMBAT, но это НЕ должно повторно нажимать Tab и сбивать цель,
    # которую панель только что нашла.
    mapping["target"] = None
    # Ctrl+ЛКМ ("lock_on") удалён 2026-10-03 по решению пользователя: в игре он
    # выбирает того, кто под курсором, обрывает бег к мобу и снимает цель при клике
    # в пустоту (логи 12:55, 13:16, 13:25).
    mapping["lock_on"] = ("mouse", "left")
    return mapping


class InputManager:
    """
    Клавиатура + мышь под тем же интерфейсом и с той же архитектурой
    безопасности, что была у версий на Interception и на геймпаде:
    команды не исполняются в потоке FSM, а кладутся в очередь и
    исполняются воркером — тик FSM никогда не блокируется на sleep().

    backend=None выбирает Win32Backend на Windows и DryRunBackend на
    остальных системах (чтобы файл можно было импортировать и тестировать
    где угодно). Для тестов можно передать свой объект с теми же четырьмя
    методами.
    """

    INPUT_MAP = _build_input_map()

    # Модификаторы, которые parse_combo_string разрешает в формате
    # "shift+1". В вашем конфиге их сейчас нет (слоты — голые клавиши),
    # поддержка оставлена, чтобы не ломать формат.
    _MODIFIERS = ("shift", "ctrl", "alt")

    # Допустимые имена направлений ходьбы -> клавиша WASD.
    _MOVE_KEYS = {
        "forward": "w", "back": "s", "left": "a", "right": "d",
        "w": "w", "s": "s", "a": "a", "d": "d",
    }

    # Сколько мс держим клавишу/кнопку. Диапазон, а не константа: одинаковая
    # длительность каждого нажатия — статистический отпечаток бота.
    _HOLD_KEY_MS = (60, 140)

    # Случайная пауза ПЕРЕД нажатием — имитация времени реакции. Идеальная
    # синхронность "решение -> действие" для человека невозможна.
    _REACTION_DELAY_S = (0.0, 0.05)

    # Смена цели Tab-ом: удержание чуть короче обычного скилла (лёгкий тап).
    _TARGET_TAP_HOLD_MS = (40, 90)

    # Длительность ходьбы получает ±джиттер, чтобы повторяющийся
    # "ровно 1.0 секунду вперёд" не выглядел как скрипт.
    _MOVE_DURATION_JITTER = 0.10

    # --- Камера (мышь) ---
    # Частота тика потока камеры. Реальный интервал между тиками
    # измеряется (см. _aim_worker): sleep на Windows неточен.
    _AIM_TICK_S = 0.02

    # Все настройки наведения (жёсткость, дрейф вокруг цели, коэффициент
    # пересчёта px мыши -> px экрана) — в AimTuning. Правь значения там.
    _AIM_TUNING = AimTuning()

    # True, если в игре включена инверсия вертикальной оси мыши.
    _CAMERA_INVERT_Y = False

    # Настройки осмотра местности (фаза SCAN): размах, скорость и паузы
    # взмахов камерой влево-вправо — в ScanTuning.
    _SCAN_TUNING = ScanTuning()

    # Как часто писать строку AIM (состояние регулятора) в input_debug.log.
    _AIM_LOG_INTERVAL_S = 0.5

    # Отдаление камеры колесом (zoom_out): пауза между щелчками и короткая
    # заморозка мыши вокруг прокрутки — человек не крутит колесо, ведя камеру.
    _WHEEL_NOTCH_GAP_S = (0.04, 0.12)
    _WHEEL_SETTLE_S = (0.08, 0.16)

    # ПЛАВНЫЙ РАЗВОРОТ (start_sweep, 2026-10-04, просьба пользователя): камера едет
    # ТОЛЬКО по горизонтали, с почти постоянной скоростью — "по прямой слева направо",
    # без взмахов, пауз и наклона вверх-вниз (обычный осмотр иногда клал камеру в пол).
    # Скорость — px мыши/с; полный оборот ~6000 px мыши (ScanTuning.full_turn_px) -> ~7-9 с.
    _SWEEP_SPEED_PX_S = (680.0, 860.0)
    _SWEEP_RAMP_TAU_S = 0.35             # плавный разгон и торможение (экспонента)
    _SWEEP_SPEED_WOBBLE = 0.06           # медленное "дыхание" скорости +-6%: рука не метроном

    # Клик по интерфейсу с курсором (click_at: список целей, 2026-10-04). Курсор в игре
    # появляется, пока зажат Alt. Человеческое движение: кривая Безье (дуга, не
    # прямая), время по закону Фиттса "дальше и мельче цель — дольше", плавный
    # разгон-торможение, иногда лёгкий перелёт с доводкой. Всё со случайным разбросом.
    # 2026-10-04 ("резче выбор целей, бот не должен тупить"): "натренированная рука" — клик по
    # списку ~0.5-0.7 с вместо 1-1.5 с. Дуга, разгон-торможение и случайный разброс остались.
    _CLICK_ALT_SHOW_S = (0.05, 0.09)      # от нажатия Alt до начала движения (курсор появился)
    # Курсор ДОЛЖЕН появиться, прежде чем двигать мышь (2026-10-04, баг найден пользователем):
    # если Alt не сработал, курсора нет, и "довод мыши к строке списка" крутил КАМЕРУ —
    # влево-вниз, до вида сверху на персонажа. Ждём курсор столько, потом отменяем клик.
    _CLICK_CURSOR_WAIT_S = 0.45
    _CLICK_CURSOR_POLL_S = 0.015
    _CLICK_FITTS_A_S = (0.05, 0.09)       # Фиттс: T = a + b*log2(1 + D/W)
    _CLICK_FITTS_B_S = (0.035, 0.055)
    _CLICK_TARGET_W_PX = 40.0             # "ширина цели" для Фиттса (строка списка ~200x33)
    _CLICK_STEP_S = (0.007, 0.012)        # шаг движения ~100 Гц, как у обычной мыши
    _CLICK_CURVE_FRAC = (0.08, 0.25)      # прогиб дуги — доля расстояния, в случайную сторону
    _CLICK_OVERSHOOT_P = 0.25             # иногда проскочить на 3-8 px и вернуться
    _CLICK_AIM_PAUSE_S = (0.02, 0.05)     # "прицелился" перед нажатием
    _CLICK_HOLD_S = (0.04, 0.08)          # кнопка зажата
    _CLICK_RELEASE_ALT_S = (0.03, 0.06)   # после клика до отпускания Alt
    _CLICK_RELEASE_KEYS = frozenset({"w", "a", "s", "d", "space", "shift"})
    _CLICK_STRAFE_P = 0.85                 # иногда человек и стоит, пока кликает
    _CLICK_STRAFE_LEAD_S = (0.06, 0.16)    # пошёл вбок -> потом Alt
    _CLICK_MOVE_GUARD_S = 0.3

    # Пауза между шагами ВНУТРИ комбо (например shift -> 1 -> отпустить).
    _COMBO_STEP_DELAY_S = (0.03, 0.07)

    # Пауза МЕЖДУ скиллами цепочки: два непересекающихся диапазона, а не
    # один — получается двугорбое распределение (то "заученная связка",
    # то "задумался"), оно ближе к человеку, чем равномерный разброс.
    # 2026-10-03 (скорость): медленный горб 1.2-1.6 -> 1.0-1.25 (средняя пауза
    # 1.17 -> 1.03 с). Быстрый горб не трогаем: 0.88 — нижняя граница, на которую
    # опирается замок цепочек в bot.py (_CHAIN_MIN_GAP_S), и короче неё игра может
    # "съесть" нажатие посреди анимации прошлого скилла.
    _SKILL_CAST_GAP_BANDS_S = ((0.88, 0.98), (1.0, 1.25))

    # Временный флаг отладки: фиксированная пауза вместо двугорбой.
    _DEBUG_FLAT_GAP_S: "float | None" = None

    # Пауза после скилла с ЗАДАННЫМ временем каста = каст * (от, до). Нижняя
    # граница 1.0, а не 0.9: пауза короче каста означает, что следующий скилл
    # жмётся, пока предыдущий ещё кастуется.
    _CAST_GAP_JITTER = (1.0, 1.15)

    def __init__(self, backend=None) -> None:
        if backend is None:
            backend = Win32Backend() if sys.platform == "win32" else DryRunBackend()
        self._backend = backend

        self._queue: "queue.Queue[tuple]" = queue.Queue()
        self._running = True
        # _abort_event — "паника" (F4/Стоп): прерывает текущие паузы и
        # временно глушит камеру. Снимается reset_abort().
        self._abort_event = threading.Event()
        # _stop_event — ТОЛЬКО окончательное завершение (stop()). Отдельно
        # от abort на случай, когда поток камеры должен пережить панику
        # (см. _aim_worker: в gamepad-версии паника навсегда убивала его).
        self._stop_event = threading.Event()
        # _camera_hold — "камера заморожена": пока флаг поднят, поток камеры
        # не шлёт в мышь вообще ничего (ни осмотр, ни доворот к цели).
        # Поднимает его _do_zoom_out на время прокрутки колеса: прокрутка не
        # должна совпадать с движением мыши.
        self._camera_hold = threading.Event()

        # Счётчик "обрывов паузы" (см. cancel_pending_actions(cut_current_wait)).
        # Каждая команда комбо запоминает его значение В МОМЕНТ ПОСТАНОВКИ в
        # очередь; если к концу ожидания значение выросло — вызвали обрыв уже
        # ПОСЛЕ постановки этой команды, и остаток её паузы пропускается.
        # Счётчик, а не флаг Event: флаг, поднятый в момент, когда воркер
        # ничего не ждёт, "протух" бы и обрезал паузу СЛЕДУЮЩЕЙ, совершенно
        # законной команды; значение на момент постановки гонок не имеет —
        # ставит в очередь и обрывает один и тот же поток (FSM).
        self._cut_gen = 0

        # Один лок на "физический ввод": защищает множество зажатого и
        # сами вызовы backend, потому что клавиши трогает _worker, камеру
        # — _aim_worker, а отпускает всё halt_immediately() из третьего
        # потока.
        self._io_lock = threading.Lock()
        # Что СЕЙЧАС зажато (клавиши и кнопки мыши) — чтобы по панике
        # отпустить ровно это и не оставить персонажа бежать/бить.
        self._held: "set[tuple[str, str]]" = set()

        # "Почтовый ящик" позиции цели для камеры (не очередь: нам нужно
        # только последнее значение, а не история).
        self._aim_lock = threading.Lock()
        self._aim_dx = 0.0
        self._aim_dy = 0.0
        self._aim_engaged = False
        # Номер измерения и момент его получения: поток камеры по счётчику
        # отличает СВЕЖЕЕ измерение от того же самого, прочитанного повторно.
        self._aim_seq = 0
        self._aim_t = 0.0
        # Режим "поворот к новому мобу" (adaptive, см. AimTuning.adapt_*): кладётся
        # вместе с измерением, чтобы поток камеры не увидел их вразнобой.
        self._aim_adaptive = False
        self._aim_gentle = False
        # Плавный горизонтальный разворот (start_sweep): сторона (0 — выключен) и скорость.
        self._sweep_dir = 0
        self._sweep_speed = 0.0
        # Чем кончился последний click_at: True — кликнули, False — отменён (курсор не
        # появился), None — кликов ещё не было. Читает bot.py (не заносить строку в
        # чёрный список, если клика по сути не было).
        self.last_click_ok: "bool | None" = None
        # Метки скиллов, которые реально нажаты (execute_combo(tag=...)). Пишет поток кнопок,
        # читает поток FSM; set.add/`in` атомарны под GIL, отдельный лок не нужен.
        self._done_tags: "set[object]" = set()
        # До какого момента (time.monotonic) идёт пауза ДОЛГОГО каста (скилл с заданным
        # cast_time). Контроллер движения (src/core/movement.py) в это время клавиши
        # не жмёт: движение сбило бы каст. 0.0 — каста нет. Пишет поток кнопок, читает
        # поток движения; float присваивается атомарно под GIL.
        self._casting_until = 0.0
        # Сумма ВСЕХ горизонтальных сдвигов камеры, которые отправил бот (поток камеры,
        # жесты поворота, прямые сдвиги). navigation.py по ней ведёт курс камеры между
        # замерами по миникарте: курс = замер + (счётчики сейчас - тогда) / счётчиков_на_градус.
        # int += в одном потоке под _io_lock; читается атомарно.
        self._yaw_counts = 0
        # Сколько кликов курсором реально нажато (кнопка мыши ушла). bot.py по нему понимает,
        # что клик по списку состоялся и стрейфы пора прекратить (дальше персонаж бежит сам).
        self.clicks_pressed = 0
        # Сразу после клика новые клавиши движения не жмём: нажатие оборвало бы бег к цели,
        # который клик только что запустил (страховка на случай, если FSM переключит ноги
        # на "стоп" на тик позже).
        self._click_guard_until = 0.0
        # Включён ли осмотр местности (start_scan/stop_scan). Лежит под тем
        # же _aim_lock, что и цель: поток камеры читает оба значения одним
        # снимком, и они не могут разойтись посреди тика.
        self._scan_enabled = False

        self._worker_thread = threading.Thread(
            target=self._worker, daemon=True, name="InputWorker"
        )
        self._worker_thread.start()
        self._aim_worker_thread = threading.Thread(
            target=self._aim_worker, daemon=True, name="AimWorker"
        )
        self._aim_worker_thread.start()

        logger.debug("InputManager (клавиатура+мышь, %s) инициализирован.", type(backend).__name__)

    # ================= Публичное API =================

    def press_button(self, action: str) -> None:
        """Ставит в очередь одиночное нажатие по имени действия ('attack', '1', 'w'...)."""
        if action not in self.INPUT_MAP:
            logger.error("InputManager: неизвестное действие '%s' — нет в INPUT_MAP.", action)
            return
        self._queue.put(("press_button", action))

    def aim_with_stick(self, dx: float, dy: float, adaptive: bool = False, gentle: bool = False) -> None:
        """
        Сообщает камере САМОЕ СВЕЖЕЕ смещение цели (dx, dy) в пикселях от
        центра ROI. Мгновенная перезапись под локом, не очередь: можно
        звать каждый тик FSM, вызывающий поток не блокируется.

        adaptive=True — поворот к НОВОМУ мобу (фаза APPROACH): камера сама меряет,
        насколько медленнее близкий моб едет по экрану, и доворачивает в разы
        дальше (см. AimTuning.adapt_*). По умолчанию False — как раньше.
        Как проверить без игры: `python src/core/input_manager.py` — печатает
        _simulate_orbit_turn() (модель орбитальной камеры) с adaptive и без.
        """
        with self._aim_lock:
            self._aim_dx = dx
            self._aim_dy = dy
            self._aim_adaptive = adaptive
            self._aim_gentle = gentle
            self._aim_engaged = True
            self._aim_seq += 1
            self._aim_t = time.perf_counter()

    def clear_aim_target(self) -> None:
        """Цель потеряна: камера больше не доворачивает и тремор выключается."""
        with self._aim_lock:
            self._aim_dx = 0.0
            self._aim_dy = 0.0
            self._aim_adaptive = False
            self._aim_gentle = False
            self._aim_engaged = False

    def start_scan(self) -> None:
        """
        Включить "осмотр местности": камера сама водит влево-вправо
        случайными плавными взмахами (см. _ScanController). Идемпотентно —
        можно звать повторно. Пока у камеры есть цель (aim_with_stick),
        осмотр сам притормаживает и уступает ей управление.
        """
        with self._aim_lock:
            self._scan_enabled = True

    def stop_scan(self) -> None:
        """Выключить осмотр: камера плавно (за ~0.1-0.2 с) гасит движение."""
        with self._aim_lock:
            self._scan_enabled = False

    def start_sweep(self) -> None:
        """
        Плавный горизонтальный разворот камеры (см. _SWEEP_*). Идемпотентно: повторный
        вызов не меняет ни сторону, ни скорость. Сторона и скорость — случайные на
        каждый новый разворот. Пока у камеры есть цель (aim_with_stick), разворот
        плавно гаснет и уступает ей. Как проверить без игры: DryRunBackend — в calls
        только mouse_move с dy == 0 и ровными dx.
        """
        with self._aim_lock:
            if self._sweep_dir == 0:
                self._sweep_dir = random.choice((-1, 1))
                self._sweep_speed = random.uniform(*self._SWEEP_SPEED_PX_S)

    def stop_sweep(self) -> None:
        """Остановить разворот (камера плавно затормозит за ~0.3 с)."""
        with self._aim_lock:
            self._sweep_dir = 0

    def move_camera_rel(self, dx: int, dy: int) -> None:
        """Прямой сдвиг камеры (вызывает mouse_move_rel бэкенда)."""
        with self._io_lock:
            self._backend.mouse_move_rel(dx, dy)
            self._yaw_counts += int(dx)

    def zoom_out(self, notches: int) -> None:
        """
        Колесо мыши ВНИЗ на notches щелчков — камера отдаляется, в кадр влезает
        больше мобов (просьба пользователя 2026-10-03). Команда очереди: не
        вклинится в комбо. Щелчки идут с человеческими паузами (_WHEEL_NOTCH_GAP_S).
        Как проверить без игры: hold_test.py (DryRunBackend пишет mouse_wheel).
        """
        self._queue.put(("zoom_out", max(1, int(notches))))

    # Признак для bot.py: click_at умеет strafe=True (старые заглушки тестов — нет).
    CLICK_STRAFE_SUPPORTED = True

    def click_at(self, x: float, y: float, button: str = "left", strafe: bool = False) -> None:
        """
        Клик мышью (button: "left"/"right") по точке экрана с курсором: Alt (появляется
        курсор) -> движение курсора по человеческой кривой -> клик -> отпустить Alt.
        Список целей бот кликает ПРАВОЙ: так персонаж сразу бежит к мобу. Команда очереди:
        не вклинится в комбо. Камера на это время заморожена (_camera_hold): иначе
        поток камеры двигал бы мышью сам курсор, и клик ушёл бы мимо.
        Как проверить без игры: InputManager(backend=DryRunBackend()).click_at(130, 567),
        wait_idle() — в backend.calls путь курсора ("mouse_abs") и порядок Alt/ЛКМ.

        strafe=True (2026-10-04, "чтобы бот не стоял, пока ведёт мышь к списку"): ДО Alt
        зажимаем A или D и держим, пока курсор едет, — отпускаем прямо перед кнопкой мыши
        (клавиша движения в момент клика оборвала бы бег к цели). Новые клавиши при
        зажатом Alt не жмём: Alt+A/Alt+D — уже сочетания игры.
        """
        self._queue.put(("click_at", float(x), float(y), button, bool(strafe)))

    def execute_combo(self, actions: "list[str]", cast_time_s: "float | None" = None,
                      tag: "object | None" = None) -> None:
        """
        Ставит ЦЕЛУЮ комбинацию как ОДНУ задачу очереди, например
        ['press_1'] или ['hold_shift', 'press_1', 'release_shift']. Формат
        шага: '<глагол>_<имя>', глагол — press/hold/release, имя — из
        INPUT_MAP. Одна задача на комбо гарантирует, что шаги пройдут
        подряд, без чужих команд между ними.

        cast_time_s — реальное время каста скилла в игре: если задано,
        пауза ПОСЛЕ скилла считается от него (±10%), а не от обычной
        короткой межскилльной паузы.
        """
        # tag — метка этого скилла (бот: (номер запуска цепочки, номер шага)). Когда клавиша
        # скилла реально нажата, метка попадает в _done_tags (см. combo_done): по ним бот
        # знает, какие скиллы цепочки успели нажаться до смерти цели (заморозка цепочек).
        self._queue.put(("execute_combo", list(actions), cast_time_s, self._cut_gen, tag))

    def combo_done(self, tag: object) -> bool:
        """Нажат ли уже скилл с этой меткой (см. execute_combo(tag=...))."""
        return tag in self._done_tags

    # --- Движение (контроллер src/core/movement.py, свой поток) ---

    def movement_key(self, name: str, down: bool) -> bool:
        """
        Зажать/отпустить клавишу движения НАПРЯМУЮ, мимо очереди: фразы движения идут
        параллельно скиллам (бот двигается и в бою). True — сделано, False — отказ.
        Проверка паники и модификаторов — ПОД _io_lock, тем же, что берёт
        halt_immediately(): иначе гонка "паника отпустила всё -> поток движения тут же
        снова зажал W" оставила бы персонажа бежать после Стопа.
        Новое нажатие не даём, пока бот держит Alt/Ctrl: Alt+A, Ctrl+D — это уже
        сочетания клавиш игры, а не шаг. Отпускать можно всегда.
        """
        target = self.INPUT_MAP.get(name)
        if target is None or target[0] != "key":
            return False
        with self._io_lock:
            if down:
                if self._abort_event.is_set() or self._stop_event.is_set():
                    return False
                if time.monotonic() < self._click_guard_until:
                    return False
                if ("key", "alt") in self._held or ("key", "ctrl") in self._held:
                    return False
                self._backend.key_down(target[1])
                self._held.add(target)
            else:
                self._backend.key_up(target[1])
                self._held.discard(target)
        return True

    def camera_nudge(self, dx: int, dy: int) -> bool:
        """
        Сдвиг камеры из потока движения (жест поворота в ROAM, src/core/movement.py).
        Отказ (False) — паника/стоп, камера заморожена (прокрутка колеса, клик) или бот
        держит Alt: при видимом курсоре сдвиг мыши двигал бы КУРСОР, а не камеру.
        Отказанный сдвиг движение не теряет — повторит на следующем шаге.
        """
        with self._io_lock:
            if self._abort_event.is_set() or self._stop_event.is_set() or self._camera_hold.is_set():
                return False
            if ("key", "alt") in self._held:
                return False
            self._backend.mouse_move_rel(int(dx), int(dy))
            self._yaw_counts += int(dx)
        return True

    def camera_yaw_counts(self) -> int:
        """Сколько счётчиков мыши по горизонтали бот всего отправил в камеру (+ вправо)."""
        return self._yaw_counts

    def movement_blocked(self) -> "str | None":
        """Почему движению сейчас нельзя жать клавиши (None — можно): паника или долгий каст."""
        if self._abort_event.is_set() or self._stop_event.is_set():
            return "стоп"
        if time.monotonic() < self._casting_until:
            return "каст"
        return None

    def change_target(self, direction: str = "right") -> None:
        """
        Смена цели (Tab). direction оставлен ради совместимости со старым
        API геймпада (там это был толчок стика влево/вправо); в T&L на
        клавиатуре Tab один, направление ни на что не влияет.
        """
        self._validate_direction(direction, "change_target")
        self._queue.put(("tap_target", direction))

    def flick_target(self, direction: str = "right") -> None:
        """Синоним change_target() — SEARCH вызывает его. См. комментарий там."""
        self._validate_direction(direction, "flick_target")
        self._queue.put(("tap_target", direction))

    def move(self, direction: str, duration_s: float) -> None:
        """
        Идти в направлении ('forward'/'back'/'left'/'right' или 'w'/'a'/
        's'/'d') duration_s секунд: зажать клавишу, подождать, отпустить.
        Идёт через ту же очередь — не пересечётся с кастом скилла.
        Пока bot.py эту функцию не вызывает (логику не трогали) — это
        готовая точка подключения для движения.
        """
        key = self._MOVE_KEYS.get(direction)
        if key is None:
            raise ValueError(
                f"move: неизвестное направление '{direction}', допустимо: {sorted(self._MOVE_KEYS)}"
            )
        self._queue.put(("move", key, float(duration_s)))

    @staticmethod
    def parse_combo_string(spec: str) -> "list[str]":
        """
        Короткая строка -> список шагов для execute_combo().
        '1' -> ['press_1'];  'shift+1' -> ['hold_shift','press_1','release_shift'].

        Единственное место в проекте, которое делает этот перевод: порядок
        hold -> press -> release генерируется ВСЕГДА одинаково, поэтому в
        конфиге невозможно "забыть отпустить модификатор".
        """
        text = (spec or "").strip()
        if not text and spec == " ":
            text = "space"                   # пробел, записанный символом
        if not text:
            raise ValueError(f"Пустая строка комбо: '{spec}'")
        # Один символ — это клавиша целиком, даже "+" (иначе split("+") съел бы его).
        raw = [text] if len(text) == 1 else [p for p in text.split("+") if p.strip()]

        parts = []
        for part in raw:
            name = InputManager.canonical_key(part)
            if name is None:
                raise ValueError(f"В комбо '{spec}' неизвестная клавиша '{part}' — нет в INPUT_MAP.")
            parts.append(name)

        if len(parts) == 1:
            return [f"press_{parts[0]}"]

        if len(parts) == 2:
            modifier, key = parts
            if modifier not in InputManager._MODIFIERS:
                raise ValueError(f"В комбо '{spec}' '{modifier}' не модификатор {InputManager._MODIFIERS}.")
            return [f"hold_{modifier}", f"press_{key}", f"release_{modifier}"]

        raise ValueError(f"Комбо '{spec}': больше одного модификатора не поддерживается.")

    @staticmethod
    def canonical_key(name: str) -> "str | None":
        """
        Как клавишу записало приложение -> имя для INPUT_MAP, или None.
        "P" -> "p", "З" (русская раскладка) -> "p", "!" -> "1", "PAGEU" -> "pageup".
        Регистр не важен. Как проверить: python src/core/input_manager.py
        (печатает разбор P, "З", "'", F1 и т.д.).
        """
        n = name.strip().lower()
        if n in InputManager.INPUT_MAP:
            return n
        n = _KEY_ALIASES.get(n, n)
        return n if n in InputManager.INPUT_MAP else None

    def cancel_pending_actions(self, cut_current_wait: bool = False) -> None:
        """
        Выбрасывает всё, что ЖДЁТ в очереди, но не трогает уже
        исполняемое нажатие (оборвать его на середине грубее). Зовётся при
        подтверждённой смерти цели — не доигрывать цепочку по трупу.

        cut_current_wait=True — дополнительно обрывает ОСТАТОК паузы после
        уже сыгранного скилла (пауза долгого каста, до ~5 с). Нужен при
        смерти/потере цели: иначе очередь остаётся "занятой", и первый Tab на
        следующего моба уходит только после конца каста по трупу (в логе:
        цель умерла в 13:43:40, Tab ушёл в 13:43:44). НЕ нужен при смене цели
        в бою — там каст идёт по живой цели, и обрывать его нечем.
        """
        if cut_current_wait:
            self._cut_gen += 1
        self._drain_queue()

    def halt_immediately(self) -> None:
        """
        Экстренная остановка (F4/Стоп): прервать паузы, очистить очередь,
        погасить камеру и ОТПУСТИТЬ всё зажатое. Дублирование с
        освобождением внутри воркера сознательное: залипшая W или ЛКМ —
        персонаж бежит/бьёт после паники.
        """
        self._abort_event.set()
        self._drain_queue()
        self.clear_aim_target()
        # Осмотр выключаем явно: без этого после reset_abort() камера сама
        # возобновила бы взмахи, хотя бот уже остановлен.
        self.stop_scan()
        self._camera_hold.clear()
        self._release_all()

    def reset_abort(self) -> None:
        """Снимает флаг паники перед следующим стартом (камера оживает сама)."""
        self._abort_event.clear()

    def stop(self) -> None:
        """Штатная остановка: дождаться очереди, погасить оба потока, отпустить всё."""
        self._queue.join()
        self._running = False
        self._stop_event.set()
        self._aim_worker_thread.join(timeout=1.0)
        self._release_all()
        logger.debug("InputManager остановлен штатно.")

    def wait_idle(self) -> None:
        """Ждёт опустошения очереди, НЕ останавливая потоки (в отличие от stop())."""
        self._queue.join()

    def is_idle(self) -> bool:
        """
        Неблокирующая проверка: очередь пуста И воркер ничего не исполняет.
        unfinished_tasks, а не queue.empty(): empty() даст True, пока
        последняя команда уже вынута из очереди, но ещё физически играется.
        """
        return self._queue.unfinished_tasks == 0

    # ================= Внутреннее =================

    @staticmethod
    def _validate_direction(direction: str, who: str) -> None:
        if direction not in ("left", "right"):
            raise ValueError(f"{who}: direction должен быть 'left' или 'right', получено '{direction}'")

    def _drain_queue(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except queue.Empty:
                break

    def _interruptible_sleep(self, seconds: float) -> bool:
        """True — проспали весь интервал, False — прервала паника."""
        return not self._abort_event.wait(timeout=seconds)

    def _sleep_gap(self, seconds: float, task_cut_gen: int) -> bool:
        """
        Пауза ПОСЛЕ скилла (в том числе долгого каста), прерываемая двумя
        способами: паникой (как _interruptible_sleep) и обрывом
        cancel_pending_actions(cut_current_wait=True), вызванным уже после
        постановки этой команды в очередь (см. self._cut_gen). Опрашиваем
        каждые 50 мс: на 5-секундной паузе это ~100 дешёвых проверок, на
        общий FPS бота не влияет (воркер — отдельный поток).
        True — паузу доиграли или оборвали штатно, False — паника.
        """
        end = time.monotonic() + seconds
        while True:
            if self._abort_event.is_set():
                return False
            if self._cut_gen != task_cut_gen:
                input_debug_logger.debug("--- пауза после скилла оборвана (цель пропала) ---")
                return True
            left = end - time.monotonic()
            if left <= 0:
                return True
            self._abort_event.wait(timeout=min(0.05, left))

    # --- Физический ввод. Только эти методы трогают backend. ---

    def _down(self, name: str) -> bool:
        """Зажать действие. False — у действия нет клавиши (осознанная пустышка)."""
        target = self.INPUT_MAP.get(name)
        if target is None:
            return False
        kind, value = target
        with self._io_lock:
            if kind == "key":
                self._backend.key_down(value)
            else:
                self._backend.mouse_button(value, True)
            self._held.add(target)
        return True

    def _up(self, name: str) -> None:
        target = self.INPUT_MAP.get(name)
        if target is None:
            return
        kind, value = target
        with self._io_lock:
            if kind == "key":
                self._backend.key_up(value)
            else:
                self._backend.mouse_button(value, False)
            self._held.discard(target)

    def _release_all(self) -> None:
        """Отпустить ВСЁ, что сейчас числится зажатым (клавиши и кнопки мыши)."""
        with self._io_lock:
            # list(...) — копия: внутри цикла множество меняется.
            for kind, value in list(self._held):
                try:
                    if kind == "key":
                        self._backend.key_up(value)
                    else:
                        self._backend.mouse_button(value, False)
                except Exception as e:
                    logger.error("InputManager: не удалось отпустить %s/%s: %s", kind, value, e)
            self._held.clear()

    def _tap(self, name: str) -> None:
        """Нажать, подержать случайное время, отпустить. Отпускаем БЕЗУСЛОВНО."""
        if not self._down(name):
            input_debug_logger.debug("SKIP    '%s': у действия нет клавиши (пустышка)", name)
            return
        hold_s = random.uniform(*self._HOLD_KEY_MS) / 1000.0
        input_debug_logger.debug("PRESS   %s (down, держим %.0f мс)", name, hold_s * 1000)
        try:
            self._interruptible_sleep(hold_s)
        finally:
            # try/finally: даже если в паузе что-то упало, клавиша не
            # должна остаться зажатой.
            self._up(name)
        input_debug_logger.debug("PRESS   %s (up)", name)

    # --- Воркер кнопок ---

    def _worker(self) -> None:
        while self._running:
            try:
                cmd = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue

            try:
                try:
                    if cmd[0] == "press_button":
                        self._do_press_button(cmd[1])
                    elif cmd[0] == "execute_combo":
                        pressed = self._do_execute_combo(cmd[1])
                        tag = cmd[4] if len(cmd) > 4 else None
                        if pressed and tag is not None:
                            self._done_tags.add(tag)     # set.add атомарен под GIL
                    elif cmd[0] == "tap_target":
                        self._do_tap_target(cmd[1])
                    elif cmd[0] == "zoom_out":
                        self._do_zoom_out(cmd[1])
                    elif cmd[0] == "click_at":
                        self._do_click_at(cmd[1], cmd[2], cmd[3], cmd[4] if len(cmd) > 4 else False)
                    elif cmd[0] == "move":
                        self._do_move(cmd[1], cmd[2])
                except Exception as e:
                    logger.error("InputManager: ошибка выполнения команды: %s", e)

                # Пауза ПОСЛЕ нажатия, но ДО task_done(): is_idle()/
                # wait_idle() считают очередь свободной только когда
                # пауза (в том числе долгого каста) реально прошла.
                if cmd[0] == "execute_combo":
                    cast_time_s = cmd[2] if len(cmd) > 2 else None
                    task_cut_gen = cmd[3] if len(cmd) > 3 else self._cut_gen
                    if cast_time_s is not None:
                        # Разброс ТОЛЬКО В ПЛЮС (раньше было +-10%): паузы короче
                        # самого каста быть не должно — в нижней половине
                        # разброса следующий скилл/цепочка жались поверх ещё
                        # идущего каста. Пауза отсчитывается от конца нажатия,
                        # а каст в игре — от его начала, это даёт ещё ~0.1-0.2 с
                        # запаса сверху.
                        gap_s = random.uniform(
                            cast_time_s * self._CAST_GAP_JITTER[0],
                            cast_time_s * self._CAST_GAP_JITTER[1],
                        )
                        input_debug_logger.debug(
                            "--- GAP после долгого каста: %.1f сек (задано скиллом, ~%.1fс) ---",
                            gap_s, cast_time_s,
                        )
                        self._casting_until = time.monotonic() + gap_s
                        try:
                            self._sleep_gap(gap_s, task_cut_gen)
                        finally:
                            self._casting_until = 0.0     # оборвали/доиграли — каста больше нет
                    elif self._DEBUG_FLAT_GAP_S is not None:
                        self._sleep_gap(self._DEBUG_FLAT_GAP_S, task_cut_gen)
                    else:
                        band = random.choice(self._SKILL_CAST_GAP_BANDS_S)
                        self._sleep_gap(random.uniform(*band), task_cut_gen)
                else:
                    self._interruptible_sleep(random.uniform(0.02, 0.05))
            finally:
                # task_done() в finally: при ошибке выше счётчик всё равно
                # уменьшится, иначе wait_idle() завис бы навсегда.
                self._queue.task_done()

    def _do_press_button(self, action: str) -> None:
        # Если паника пришла ДО нажатия — отпускать нечего, просто выходим.
        if not self._interruptible_sleep(random.uniform(*self._REACTION_DELAY_S)):
            return
        self._tap(action)

    def _do_tap_target(self, direction: str) -> None:
        input_debug_logger.debug("=== TARGET (Tab; direction=%s игнорируется) ===", direction)
        if not self._interruptible_sleep(random.uniform(*self._REACTION_DELAY_S)):
            return
        if not self._down("next_target"):
            return
        hold_s = random.uniform(*self._TARGET_TAP_HOLD_MS) / 1000.0
        try:
            self._interruptible_sleep(hold_s)
        finally:
            self._up("next_target")
        input_debug_logger.debug("=== TARGET END (держали %.0f мс) ===", hold_s * 1000)

    def _do_zoom_out(self, notches: int) -> None:
        """
        Камера замирает (_camera_hold) -> notches щелчков колеса вниз с паузами ->
        камера оживает. Заморозка снимается в finally безусловно: паника посреди
        прокрутки не должна оставить камеру мёртвой.
        """
        input_debug_logger.debug("WHEEL   вниз x%d (камера отдаляется)", notches)
        self._camera_hold.set()
        try:
            if not self._interruptible_sleep(random.uniform(*self._WHEEL_SETTLE_S)):
                return
            for i in range(notches):
                self._backend.mouse_wheel(-_WHEEL_DELTA)
                if i + 1 < notches and not self._interruptible_sleep(random.uniform(*self._WHEEL_NOTCH_GAP_S)):
                    return
            self._interruptible_sleep(random.uniform(*self._WHEEL_SETTLE_S))
        finally:
            self._camera_hold.clear()

    @staticmethod
    def _cursor_path(x0: float, y0: float, x1: float, y1: float, steps: int, bend: float) -> "list[tuple[float, float]]":
        """
        Кубическая кривая Безье от (x0,y0) до (x1,y1): опорные точки сдвинуты вбок на
        bend*расстояние (знак — сторона дуги). Время по кривой идёт через smoothstep
        (3t^2-2t^3): медленно в начале и в конце, быстро в середине — так двигается рука.
        """
        dx, dy = x1 - x0, y1 - y0
        dist = math.hypot(dx, dy) or 1.0
        nx, ny = -dy / dist, dx / dist                 # перпендикуляр к прямой
        c1 = (x0 + dx * 0.3 + nx * bend * dist, y0 + dy * 0.3 + ny * bend * dist)
        c2 = (x0 + dx * 0.7 + nx * bend * dist * 0.6, y0 + dy * 0.7 + ny * bend * dist * 0.6)
        pts = []
        for i in range(1, steps + 1):
            t = i / steps
            t = t * t * (3.0 - 2.0 * t)
            u = 1.0 - t
            pts.append((
                u ** 3 * x0 + 3 * u * u * t * c1[0] + 3 * u * t * t * c2[0] + t ** 3 * x1,
                u ** 3 * y0 + 3 * u * u * t * c1[1] + 3 * u * t * t * c2[1] + t ** 3 * y1,
            ))
        return pts

    def _move_cursor_human(self, x: float, y: float) -> bool:
        """Довести курсор до (x, y). False — паника оборвала движение."""
        x0, y0 = self._backend.cursor_pos()
        dist = math.hypot(x - x0, y - y0)
        duration = (random.uniform(*self._CLICK_FITTS_A_S)
                    + random.uniform(*self._CLICK_FITTS_B_S) * math.log2(1.0 + dist / self._CLICK_TARGET_W_PX))
        step = random.uniform(*self._CLICK_STEP_S)
        steps = max(4, int(duration / step))
        bend = random.uniform(*self._CLICK_CURVE_FRAC) * random.choice((-1.0, 1.0))
        tx, ty = x, y
        overshoot = dist > 60 and random.random() < self._CLICK_OVERSHOOT_P
        if overshoot:                                  # проскочить по направлению движения
            k = random.uniform(3.0, 8.0) / max(dist, 1.0)
            tx, ty = x + (x - x0) * k, y + (y - y0) * k
        for px, py in self._cursor_path(x0, y0, tx, ty, steps, bend):
            self._backend.mouse_move_abs(px, py)
            if not self._interruptible_sleep(step * random.uniform(0.8, 1.2)):
                return False
        if overshoot:                                  # короткая доводка назад
            for px, py in self._cursor_path(tx, ty, x, y, random.randint(3, 6), 0.0):
                self._backend.mouse_move_abs(px, py)
                if not self._interruptible_sleep(step * random.uniform(0.9, 1.4)):
                    return False
        return True

    def _wait_cursor(self, visible: bool, timeout_s: float) -> bool:
        """Ждать, пока курсор станет видимым/скрытым. True — дождались (или узнать нельзя)."""
        fn = getattr(self._backend, "cursor_visible", None)
        if fn is None:
            return True
        end = time.monotonic() + timeout_s
        while True:
            state = fn()
            if state is None or state == visible:
                return True
            if time.monotonic() >= end:
                return False
            if not self._interruptible_sleep(self._CLICK_CURSOR_POLL_S):
                return False

    def _do_click_at(self, x: float, y: float, button: str = "left", strafe: bool = False) -> None:
        """
        Alt -> ДОЖДАТЬСЯ курсора -> курсор к точке -> клик -> отпустить Alt. Нет курсора —
        мышь не трогаем (иначе она крутила бы камеру), клик отменяется: last_click_ok=False.
        Alt и заморозка камеры снимаются в finally.
        """
        input_debug_logger.debug("CLICK   Alt+%s по (%.0f, %.0f)", "ПКМ" if button == "right" else "ЛКМ", x, y)
        self.last_click_ok = False
        self._camera_hold.set()
        alt_down = False
        strafe_key = None
        try:
            if strafe:
                strafe_key = self._click_strafe_start()
            if not self._interruptible_sleep(random.uniform(0.01, 0.03)):
                return
            alt_down = self._down("alt")
            if not self._interruptible_sleep(random.uniform(*self._CLICK_ALT_SHOW_S)):
                return
            if not self._wait_cursor(True, self._CLICK_CURSOR_WAIT_S):
                # Вторая попытка: в игре Alt может работать как переключатель (нажал — курсор
                # есть, нажал ещё — пропал). Отпускаем и жмём снова.
                self._up("alt")
                alt_down = False
                if not self._interruptible_sleep(random.uniform(0.08, 0.14)):
                    return
                alt_down = self._down("alt")
                if not self._wait_cursor(True, self._CLICK_CURSOR_WAIT_S):
                    input_debug_logger.debug("CLICK   курсор не появился (Alt не сработал) — клик отменён, мышь не трогаю")
                    return
            if not self._move_cursor_human(x, y):
                return
            if not self._interruptible_sleep(random.uniform(*self._CLICK_AIM_PAUSE_S)):
                return
            with self._io_lock:
                # Стрейф, зажатый во время ведения мыши ("живее", 2026-10-04), отпускаем ДО
                # клика: клавиша движения в момент клика оборвала бы бег к цели.
                released = [v for kind, v in list(self._held) if kind == "key" and v in self._CLICK_RELEASE_KEYS]
                for v in released:
                    self._backend.key_up(v)
                    self._held.discard(("key", v))
                self._backend.mouse_button(button, True)
                self.clicks_pressed += 1
                self._click_guard_until = time.monotonic() + self._CLICK_MOVE_GUARD_S
            if released:
                input_debug_logger.debug("CLICK   перед кликом отпустил %s", "+".join(sorted(released)))
            try:
                self._interruptible_sleep(random.uniform(*self._CLICK_HOLD_S))
            finally:
                with self._io_lock:
                    self._backend.mouse_button(button, False)
            self.last_click_ok = True
            self._interruptible_sleep(random.uniform(*self._CLICK_RELEASE_ALT_S))
        finally:
            if strafe_key is not None and ("key", strafe_key) in self._held:
                self._up(strafe_key)              # клик отменён до кнопки — стрейф всё равно отпускаем
            if alt_down:
                self._up("alt")
                # Курсор должен спрятаться, иначе мышь не вернёт управление камерой. Если
                # Alt в игре — переключатель, курсор останется: тогда жмём Alt ещё раз.
                if not self._wait_cursor(False, 0.35):
                    self._down("alt")
                    self._interruptible_sleep(random.uniform(0.05, 0.09))
                    self._up("alt")
                    if not self._wait_cursor(False, 0.35):
                        input_debug_logger.debug("CLICK   курсор не прячется после Alt — проверь настройку курсора в игре")
            self._camera_hold.clear()
            input_debug_logger.debug("CLICK   %s, Alt отпущен", "готово" if self.last_click_ok else "отменён")

    def _click_strafe_start(self) -> "str | None":
        """
        Перед Alt: если стрейф ещё не зажат (его мог зажать поток движения) — с вероятностью
        _CLICK_STRAFE_P зажать A или D и чуть "разогнаться" (человек сначала пошёл вбок,
        потом потянулся к Alt). Возвращает нажатую здесь клавишу или None.
        """
        with self._io_lock:
            if any(("key", k) in self._held for k in ("a", "d")):
                return None                       # уже стрейфим (фраза движения) — держим как есть
            if self._abort_event.is_set() or random.random() >= self._CLICK_STRAFE_P:
                return None
            key = random.choice(("a", "d"))
            self._backend.key_down(key)
            self._held.add(("key", key))
        input_debug_logger.debug("CLICK   стрейф %s, пока веду мышь к цели", key.upper())
        self._interruptible_sleep(random.uniform(*self._CLICK_STRAFE_LEAD_S))
        return key

    def _do_move(self, key: str, duration_s: float) -> None:
        if not self._interruptible_sleep(random.uniform(*self._REACTION_DELAY_S)):
            return
        jitter = self._MOVE_DURATION_JITTER
        real_s = max(0.0, duration_s * random.uniform(1.0 - jitter, 1.0 + jitter))
        input_debug_logger.debug("MOVE    %s на %.2f сек", key, real_s)
        if not self._down(key):
            return
        try:
            self._interruptible_sleep(real_s)
        finally:
            self._up(key)
        input_debug_logger.debug("MOVE    %s отпущена", key)

    def _do_execute_combo(self, actions: "list[str]") -> bool:
        """True — основная клавиша скилла (press_) реально нажата."""
        # Имена, которые ЭТОТ вызов зажал через hold_ и ещё не отпустил:
        # страховка в конце отпустит именно их, даже если паника оборвала
        # комбо посередине.
        held_here: "set[str]" = set()
        pressed = False
        input_debug_logger.debug("=== COMBO START %s ===", actions)

        for action in actions:
            verb, _, target = action.partition("_")

            if target not in self.INPUT_MAP or verb not in ("press", "hold", "release"):
                logger.error("InputManager: неизвестный шаг комбо '%s' — пропущен.", action)
                input_debug_logger.debug("SKIP    неизвестный шаг '%s'", action)
                continue

            if verb == "hold":
                if self._down(target):
                    held_here.add(target)
                    input_debug_logger.debug("HOLD    %s", target)
            elif verb == "release":
                self._up(target)
                held_here.discard(target)
                input_debug_logger.debug("RELEASE %s", target)
            else:  # press
                self._tap(target)
                pressed = True

            gap_s = random.uniform(*self._COMBO_STEP_DELAY_S)
            if not self._interruptible_sleep(gap_s):
                input_debug_logger.debug("=== COMBO ABORTED (halt_immediately) ===")
                break

        for name in held_here:
            self._up(name)
            input_debug_logger.debug("SAFETY  отпускаю зависшую %s", name)
        input_debug_logger.debug("=== COMBO END ===")
        return pressed

    # --- Камера ---

    def _read_aim_target(self) -> "tuple[float, float, bool, int, float, bool, bool, bool]":
        with self._aim_lock:
            return (
                self._aim_dx, self._aim_dy, self._aim_engaged,
                self._aim_seq, self._aim_t, self._scan_enabled, self._aim_adaptive, self._aim_gentle,
            )

    def _aim_worker(self) -> None:
        """
        НЕПРЕРЫВНЫЙ цикл камеры. Сам расчёт — в _AimController (пружина +
        демпфер + дрейф вокруг цели, см. его докстринг); здесь только
        "проводка": читаем почтовый ящик, зовём регулятор, отправляем
        целые пиксели в мышь.

        Что важно знать:
          - dt ИЗМЕРЯЕТСЯ (perf_counter), а не берётся константой: sleep
            на Windows неточен, и скорость камеры плавала бы вместе с
            таймером. Сверху dt ограничен 50 мс — после лага потока один
            огромный шаг дал бы скачок камеры.
          - Паника (abort) только обнуляет регулятор и глушит вывод, но
            НЕ завершает поток: выходим только по _stop_event.
          - Когда цель пропала (engaged=False), регулятор не обрывает
            движение, а плавно гасит остаточную скорость.
          - На время прокрутки колеса (_camera_hold) вывод в мышь полностью выключен.
          - Осмотр местности (_ScanController) и доворот к цели (_AimController)
            — два независимых генератора, а мышь одна. Приоритет у цели:
            пока есть aim_with_stick() (engaged), осмотр плавно тормозит;
            их выходы просто складываются (на стыке это доли пикселя).

        Как тестировать без игры: `python input_manager.py` прогоняет
        симуляцию подхода к цели (без Windows и без игры), а
        DryRunBackend позволяет подать aim_with_stick() и посмотреть, какие
        mouse_move ушли.
        """
        ctrl = _AimController(self._AIM_TUNING, self._CAMERA_INVERT_Y)
        scan = _ScanController(self._SCAN_TUNING)
        sweep_v, sweep_rem = 0.0, 0.0              # плавный разворот: скорость и дробные px
        last_seq = -1
        was_engaged = False
        was_scanning = False
        hold_active = False
        next_log_at = 0.0
        next_scan_log_at = 0.0
        last_t = time.perf_counter()

        while not self._stop_event.is_set():
            now = time.perf_counter()
            dt = min(max(now - last_t, 1e-3), 0.05)
            last_t = now

            if self._abort_event.is_set():
                ctrl.reset()
                scan.reset()
                was_engaged = False
                was_scanning = False
                hold_active = False
                last_seq = -1
            elif self._camera_hold.is_set():
                # Камера заморожена на время прокрутки колеса: мышь не трогаем вообще.
                # Время (last_t) тикает как обычно, а регуляторы при первом
                # тике заморозки обнуляем — иначе после разморозки камера
                # рванула бы с накопленной скоростью.
                if not hold_active:
                    hold_active = True
                    ctrl.freeze()
                    scan.freeze()
                    input_debug_logger.debug("CAMERA  заморожена (lock-on)")
            else:
                if hold_active:
                    hold_active = False
                    input_debug_logger.debug("CAMERA  разморожена")
                dx, dy, engaged, seq, t_meas, scan_on, adaptive, gentle = self._read_aim_target()

                if engaged and not was_engaged:
                    ctrl.begin(now)
                    input_debug_logger.debug("AIM     захват цели dx=%+.1f dy=%+.1f", dx, dy)
                elif was_engaged and not engaged:
                    input_debug_logger.debug("AIM     цель потеряна -> плавная остановка")
                was_engaged = engaged

                if engaged and seq != last_seq:
                    ctrl.measure(dx, dy, t_meas, now, adaptive, gentle)
                    last_seq = seq

                step_x, step_y = ctrl.step(dt, engaged, now)

                # Осмотр: работает только пока включён И цели нет. Выход
                # складываем с выходом регулятора (при engaged осмотр лишь
                # доигрывает затухание скорости, см. _ScanController.step).
                scan_x, scan_y = scan.step(dt, scan_on and not engaged)
                step_x += scan_x
                step_y += scan_y

                # Плавный горизонтальный разворот: скорость экспонентой тянется к цели
                # (разгон/торможение без рывка), вертикали нет вовсе.
                with self._aim_lock:
                    sweep_dir, sweep_speed = self._sweep_dir, self._sweep_speed
                wobble = 1.0 + self._SWEEP_SPEED_WOBBLE * math.sin(now * 1.3 + sweep_speed)
                target_v = sweep_dir * sweep_speed * wobble if not engaged else 0.0
                alpha = 1.0 - math.exp(-dt / self._SWEEP_RAMP_TAU_S)
                sweep_v += (target_v - sweep_v) * alpha
                if abs(sweep_v) < 1.0 and target_v == 0.0:
                    sweep_v, sweep_rem = 0.0, 0.0
                sweep_rem += sweep_v * dt
                whole = int(sweep_rem)
                sweep_rem -= whole
                step_x += whole
                if scan.running != was_scanning:
                    was_scanning = scan.running
                    input_debug_logger.debug(
                        "SCAN    %s", "осмотр начат" if was_scanning else "осмотр остановлен"
                    )
                if scan.running and now >= next_scan_log_at:
                    input_debug_logger.debug("SCAN    %s", scan.snapshot())
                    next_scan_log_at = now + 1.0

                if step_x or step_y:
                    try:
                        with self._io_lock:
                            self._backend.mouse_move_rel(step_x, step_y)
                            self._yaw_counts += step_x
                    except Exception as e:
                        logger.error("InputManager: ошибка движения камеры: %s", e)

                if engaged and now >= next_log_at:
                    input_debug_logger.debug("AIM     %s", ctrl.snapshot())
                    next_log_at = now + self._AIM_LOG_INTERVAL_S

            # Event.wait вместо time.sleep: выходит мгновенно при stop().
            if self._stop_event.wait(timeout=self._AIM_TICK_S):
                break


def _simulate_aim(
    tuning: AimTuning,
    start_px: "tuple[float, float]",
    real_gain: float,
    seconds: float = 4.0,
    target_vel: "tuple[float, float]" = (0.0, 0.0),
    noise_px: float = 1.5,
    seed: int = 1,
) -> "list[tuple[float, float, float]]":
    """
    Песочница: замкнутый контур "регулятор + упрощённая игра" без Windows.
    Игра моделируется так: движение мыши на 1 px сдвигает цель на экране на
    real_gain px; измерение приходит с задержкой vision_latency_s и с
    шумом detection noise_px. Возвращает список (t, offset_x, offset_y)
    истинного смещения цели.

    Зачем: перед боем проверить, что подход без перелёта и без качания, и
    увидеть, что будет, если коэффициент пересчёта подобран неточно
    (real_gain != tuning.gain_x).
    """
    random.seed(seed)
    ctrl = _AimController(tuning)
    dt = 0.02
    off = [start_px[0], start_px[1]]
    log: "list[tuple[float, list[float]]]" = []
    trace = []
    ctrl.begin(0.0)
    steps = int(seconds / dt)
    delay_steps = max(0, round(tuning.vision_latency_s / dt))
    for n in range(steps):
        now = n * dt
        log.append((now, list(off)))
        seen = log[max(0, n - delay_steps)][1]
        ctrl.measure(
            seen[0] + random.gauss(0.0, noise_px),
            seen[1] + random.gauss(0.0, noise_px),
            now, now,
        )
        mx, my = ctrl.step(dt, True, now)
        off[0] += target_vel[0] * dt - mx * real_gain
        off[1] += target_vel[1] * dt - my * real_gain
        trace.append((now, off[0], off[1]))
    return trace


def _simulate_orbit_turn(
    tuning: AimTuning,
    start_px: float,
    mob_dist_m: float,
    cam_dist_m: float = 6.0,
    adaptive: bool = True,
    vision_dt: float = 0.04,
    latency_s: float = 0.09,
    tol_px: float = 45.0,
    settle_s: float = 0.25,
    seconds: float = 8.0,
    seed: int = 1,
) -> "float | None":
    """
    Песочница поворота к мобу на модели ОРБИТАЛЬНОЙ камеры TL (без игры).
    Камера в cam_dist_m за персонажем и вращается вокруг него; моб в mob_dist_m
    от персонажа виден на start_px от прицела. Экранное смещение моба:
        x = F * D*sin(phi) / (C + D*cos(phi)),  phi — угол моба от взгляда.
    Чем ближе моб (D << C), тем меньше он едет по экрану на градус поворота.
    Кадры зрения — раз в vision_dt, каждый показывает мир latency_s назад.
    Возвращает время (с), когда моб продержался в tol_px >= settle_s (момент
    клика ПКМ в APPROACH), или None, если не дошли за seconds.
    """
    random.seed(seed)
    focal = 960.0                                  # FOV ~90 градусов на 1920 px
    px_per_deg_far = focal * math.pi / 180.0       # калибровка gain_x — по ДАЛЬНИМ
    deg_per_mouse = tuning.gain_x / px_per_deg_far

    def screen_x(phi_deg: float) -> float:
        p = math.radians(phi_deg)
        return focal * mob_dist_m * math.sin(p) / (cam_dist_m + mob_dist_m * math.cos(p))

    phi0 = next((k / 10.0 for k in range(1800) if screen_x(k / 10.0) >= start_px), None)
    if phi0 is None:
        return None                                # так далеко в сторону моб не виден
    ctrl = _AimController(tuning)
    ctrl.begin(0.0)
    dt, now, theta = 0.02, 0.0, 0.0
    hist: "list[tuple[float, float]]" = []
    next_frame, inside_since = 0.0, None
    while now < seconds:
        hist.append((now, screen_x(phi0 - theta)))
        if now >= next_frame:
            seen = next((x for t0, x in reversed(hist) if t0 <= now - latency_s), hist[0][1])
            ctrl.measure(seen + random.gauss(0.0, 1.5), 0.0, now, now, adaptive)
            next_frame = now + vision_dt
            if abs(seen) <= tol_px:
                inside_since = now if inside_since is None else inside_since
                if now - inside_since >= settle_s:
                    return now
            else:
                inside_since = None
        mx, _ = ctrl.step(dt, True, now)
        theta += mx * deg_per_mouse
        now += dt
    return None


def _simulate_scan(
    tuning: ScanTuning, seconds: float = 30.0, seed: int = 1, dt: float = 0.02,
) -> "list[tuple[float, int, int, float, float]]":
    """
    Песочница осмотра: гоняет _ScanController без игры и возвращает строки
    (t, step_x, step_y, pos_x, pos_y), где pos — накопленное положение
    камеры в px мыши. Нужна, чтобы глазами (по цифрам) проверить: камера
    остаётся около курса, взмахи разной длины, скорость без рывков.
    """
    random.seed(seed)
    ctrl = _ScanController(tuning)
    px = py = 0.0
    rows = []
    for n in range(int(seconds / dt)):
        sx, sy = ctrl.step(dt, True)
        px += sx
        py += sy
        rows.append((n * dt, sx, sy, px, py))
    return rows


def _summarize_trace(trace: "list[tuple[float, float, float]]", axis: int = 1) -> "tuple[float, float | None]":
    """
    (перелёт в px, время успокоения в с) по трассе из _simulate_aim().
    Перелёт — насколько цель проскочила за точку прицеливания в
    противоположную сторону. Успокоение — момент, после которого смещение
    навсегда остаётся в пределах 5 px (None, если так и не успокоилось).
    Идём с конца и ищем последний выход за порог — один проход вместо
    вложенного перебора.
    """
    values = [row[axis] for row in trace]
    overshoot = max(0.0, -min(values))
    last_bad = -1
    for k in range(len(values) - 1, -1, -1):
        if abs(values[k]) >= 5.0:
            last_bad = k
            break
    if last_bad == len(values) - 1:
        return overshoot, None
    return overshoot, trace[last_bad + 1][0]


if __name__ == "__main__":
    # Песочница БЕЗ игры. По умолчанию — DryRunBackend: ничего реально не
    # нажимается, печатаем, что бы ушло в Windows. Реальный прогон:
    #   python input_manager.py --live
    # (через 5 секунд начнёт ЖАТЬ клавиши в активном окне — переключись в
    # Блокнот, а НЕ в игру; для игры используй smoke_test_input.py).
    logging.basicConfig(level=logging.DEBUG)
    live = "--live" in sys.argv

    print("parse_combo_string('1')       =", InputManager.parse_combo_string("1"))
    print("parse_combo_string('shift+2') =", InputManager.parse_combo_string("shift+2"))
    print("parse_combo_string('=')       =", InputManager.parse_combo_string("="))
    # Любая клавиша из приложения (2026-10-03): буквы, русская раскладка, знаки, F-клавиши.
    for _k in ("P", "З", "'", "F1", "SPACE", "LMB"):
        print(f"parse_combo_string({_k!r}) =", InputManager.parse_combo_string(_k),
              "| скан-код", hex(_SCANCODES[InputManager.canonical_key(_k)][0]) if InputManager.canonical_key(_k) in _SCANCODES else "-")
    try:
        InputManager.parse_combo_string("RB+X")
    except ValueError as e:
        print("parse_combo_string('RB+X') корректно упал:", e)

    # Симуляция наведения (без игры): подход к цели с 445 px, дрейф выключен,
    # чтобы чисто увидеть перелёт/время успокоения. real_gain/assumed_gain —
    # во сколько раз реальная чувствительность камеры отличается от
    # предположенной в AimTuning (1.0 = калибровка точная).
    _calm = AimTuning(wander_std=(0.0, 0.0))
    for ratio in (0.5, 1.0, 2.0):
        tr = _simulate_aim(_calm, (445.0, 0.0), real_gain=_calm.gain_x * ratio)
        overshoot, settle = _summarize_trace(tr, axis=1)
        settle_txt = "не успокоилась" if settle is None else f"~{settle:.2f} с"
        print(f"Симуляция: реальная чувствительность x{ratio}: перелёт {overshoot:.0f} px, успокоение {settle_txt}")

    # Поворот к новому мобу (без игры): близкий моб на орбитальной камере едет по
    # экрану медленнее дальнего. adaptive=True должен сокращать время до клика.
    for _dist in (100.0, 4.0, 2.5):
        _old = _simulate_orbit_turn(_calm, 400.0, _dist, adaptive=False)
        _new = _simulate_orbit_turn(_calm, 400.0, _dist, adaptive=True)
        _fmt = lambda v: "не дошли" if v is None else f"{v:.2f} с"
        print(f"Поворот к мобу в {_dist:g} м (400 px сбоку): было {_fmt(_old)}, стало {_fmt(_new)}")

    # Осмотр местности (без игры): 40 секунд сухого прогона генератора.
    _rows = _simulate_scan(ScanTuning(), seconds=40.0)
    _xs = [r[3] for r in _rows]
    _speed = [abs(r[1]) / 0.02 for r in _rows]
    print(
        "Осмотр за 40 с: X от %+.0f до %+.0f px мыши, пик скорости %.0f px/с, "
        "Y в пределах %+.0f..%+.0f" % (
            min(_xs), max(_xs), max(_speed), min(r[4] for r in _rows), max(r[4] for r in _rows),
        )
    )

    if live:
        print("LIVE: переключись в Блокнот. Старт через 5 секунд...")
        time.sleep(5)
        backend = None
    else:
        backend = DryRunBackend()

    im = InputManager(backend=backend)
    im.execute_combo(InputManager.parse_combo_string("1"))
    im.press_button("attack")
    im.change_target("right")
    im.move("forward", 0.3)
    im.wait_idle()

    print("Камера: цель правее центра на 100 px, 1 секунда...")
    im.aim_with_stick(100.0, 0.0)
    time.sleep(1.0)
    im.clear_aim_target()
    time.sleep(0.3)
    im.stop()

    if isinstance(backend, DryRunBackend):
        moves = [c for c in backend.calls if c[0] == "mouse_move"]
        other = [c for c in backend.calls if c[0] != "mouse_move"]
        print("Вызовы клавиш/кнопок:", other)
        print("Движений мыши: %d, суммарно dx=%d" % (len(moves), sum(c[1] for c in moves)))
    print("Песочница завершена.")
