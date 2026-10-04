"""
smoke_test_input.py — проверка, что игра вообще ПРИНИМАЕТ клавиатуру и мышь из Python.

Зачем: прежде чем гонять бота, нужно ответить на один вопрос — "реагирует ли
Throne and Liberty на ввод от pyautogui / SendInput?". Дебажить это внутри
боя (с Vision, FSM, цепочками) слишком дорого, поэтому тест ходит НИЖЕ всей
логики бота: напрямую в Win32Backend, по одной клавише за раз.

Как запускать (из корня проекта, рядом с main.py), игра в оконном/безрамочном
режиме, персонаж стоит в безопасном месте:

    python smoke_test_input.py keys     # слоты 1 2 3 4 5 6 7 8 9 0 - = (по одному, пауза 2 с)
    python smoke_test_input.py wasd     # W, S, A, D по 1 секунде
    python smoke_test_input.py attack   # один клик ПКМ по выбранной цели (бег к цели + автоатака)
    python smoke_test_input.py tab      # один Tab (смена цели) — перед этим встань рядом с мобом
    python smoke_test_input.py mouse    # камера: вправо, влево, вверх, вниз
    python smoke_test_input.py calibrate # КАЛИБРОВКА камеры: измеряет, на сколько px экрана сдвигается цель
                                         # при движении мыши на 1 px (нужен выбранный неподвижный моб в кадре)
    python smoke_test_input.py aim      # вся цепочка камеры: искусственная "цель" качается влево-вправо
    python smoke_test_input.py wheel    # колесо мыши вниз x4 — камера должна отдалиться (как делает бот в поиске)
    python smoke_test_input.py turn [px]  # поворот камеры на px мыши вправо (по умолчанию = ScanTuning.full_turn_px, ~360°):
                                         # подбор числа px на полный оборот; вид должен вернуться в исходную точку
    python smoke_test_input.py scan     # осмотр местности: камера сама водит влево-вправо хаотичными взмахами
    python smoke_test_input.py all      # keys + wasd + attack + tab + mouse

После команды у тебя 5 секунд, чтобы кликнуть в окно игры. F4 — аварийная остановка
(отпускает всё зажатое). В консоли печатается, ЧТО именно сейчас отправлено, — сверяй
с тем, что видишь в игре, и записывай, что НЕ сработало.
"""

import ctypes
import statistics
import sys
import time

from src.core.input_manager import InputManager, Win32Backend

_VK_F4 = 0x73
_COUNTDOWN_S = 5


def _f4_pressed() -> bool:
    # GetAsyncKeyState видит клавишу глобально, даже когда фокус в игре.
    # Старший бит (0x8000) = "нажата прямо сейчас".
    return bool(ctypes.windll.user32.GetAsyncKeyState(_VK_F4) & 0x8000)


def _wait(seconds: float) -> None:
    """Пауза, которую можно прервать по F4 (проверяем каждые 50 мс)."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if _f4_pressed():
            raise KeyboardInterrupt("F4")
        time.sleep(0.05)


def stage_keys(be: Win32Backend) -> None:
    for key in "1234567890-=":
        print(f"  жму слот-клавишу '{key}' — смотри, сработал ли скилл на панели")
        be.key_down(key)
        _wait(0.1)
        be.key_up(key)
        _wait(2.0)


def stage_wasd(be: Win32Backend) -> None:
    for key, name in (("w", "вперёд"), ("s", "назад"), ("a", "влево"), ("d", "вправо")):
        print(f"  зажимаю '{key}' на 1 сек ({name}) — персонаж должен пойти")
        be.key_down(key)
        try:
            _wait(1.0)
        finally:
            be.key_up(key)
        _wait(0.7)


def stage_attack(be: Win32Backend) -> None:
    print("  клик ПКМ — выбери цель (Tab) и встань чуть дальше: персонаж должен побежать и бить")
    be.mouse_button("right", True)
    _wait(0.1)
    be.mouse_button("right", False)
    _wait(2.0)


def stage_wheel(be: Win32Backend) -> None:
    # Ровно то, что делает InputManager.zoom_out(): щелчки колеса вниз с паузами.
    # Ctrl+ЛКМ удалён из бота (2026-10-03), его проверка здесь больше не нужна.
    print("  колесо вниз x4 — камера должна отдалиться")
    for _ in range(4):
        be.mouse_wheel(-120)
        _wait(0.08)
    _wait(1.0)


def stage_tab(be: Win32Backend) -> None:
    print("  Tab — цель должна смениться/выбраться (встань рядом с мобом)")
    be.key_down("tab")
    _wait(0.06)
    be.key_up("tab")
    _wait(1.0)


def stage_mouse(be: Win32Backend) -> None:
    # Двигаем мышь ПЛАВНО (по 15 px за шаг), а не одним прыжком: так
    # проще заметить, что камера крутится, и ближе к тому, что будет делать
    # _aim_worker. 20 шагов * 15 px = 300 px в каждую сторону.
    for name, dx, dy in (("вправо", 15, 0), ("влево", -15, 0), ("вниз", 0, 15), ("вверх", 0, -15)):
        print(f"  камера {name}: 20 шагов по 15 px (всего 300 px)")
        for _ in range(20):
            be.mouse_move_rel(dx, dy)
            _wait(0.02)
        _wait(0.8)
    print("  Если камера НЕ крутилась — игра игнорирует SendInput-мышь (напиши мне).")
    print("  Если крутилась ОЧЕНЬ слабо/сильно — запомни, на сколько градусов от 300 px.")


def _sweep(be: Win32Backend, dx_total: int, dy_total: int, steps: int = 20) -> None:
    """
    Плавно переместить мышь на (dx_total, dy_total) целых px за `steps` шагов.
    Шаг считаем как разность округлённых накопленных сумм, а не total/steps:
    так сумма ровно равна заказанной, и ошибки округления не копятся.
    """
    for k in range(steps):
        sx = round((k + 1) * dx_total / steps) - round(k * dx_total / steps)
        sy = round((k + 1) * dy_total / steps) - round(k * dy_total / steps)
        if sx or sy:
            be.mouse_move_rel(sx, sy)
        _wait(0.02)


def stage_calibrate(be: Win32Backend) -> None:
    """
    Калибровка камеры. Выбери (Tab) НЕПОДВИЖНОГО моба прямо перед собой —
    чтобы его HP-бар был виден. Скрипт двигает мышь на известное число px,
    Vision измеряет, на сколько сдвинулся бар, и печатает коэффициент.

    gain = на сколько ЭКРАННЫХ px сдвигается цель при движении мыши на 1 px.
    Это то самое число, которое регулятор камеры (AimTuning.gain_x/gain_y)
    использует, чтобы переводить "нужно довернуть на N px экрана" в "сдвинь
    мышь на M px". Без точного значения камера либо вялая, либо резкая.

    Как тестировать без игры: сам расчёт — одна строка (-delta / mouse_px),
    остальное — движение мыши и чтение Vision; проверить расчёт можно,
    подставив в _gain_from() числа руками.
    """
    from src.config import vision_config
    from src.core.vision.vision_manager import VisionManager

    vision = VisionManager()
    roi = vision.build_roi(
        vision_config.TARGET_HP_OFFSET_X,
        vision_config.TARGET_HP_OFFSET_Y,
        vision_config.TARGET_HP_WIDTH,
        vision_config.TARGET_HP_HEIGHT,
    )

    def read_offset():
        # Медиана нескольких кадров гасит случайный шум детекции бара.
        xs, ys = [], []
        for _ in range(7):
            info = vision.get_target_info(roi)
            if info.found:
                xs.append(info.offset_x)
                ys.append(info.offset_y)
            _wait(0.03)
        if len(xs) < 4:
            return None
        return statistics.median(xs), statistics.median(ys)

    def _gain_from(before, after, mouse_dx, mouse_dy):
        # Мышь вправо/вниз -> камера поворачивается -> цель на экране
        # сдвигается в ПРОТИВОПОЛОЖНУЮ сторону, поэтому знак минус.
        gx = -(after[0] - before[0]) / mouse_dx if mouse_dx else None
        gy = -(after[1] - before[1]) / mouse_dy if mouse_dy else None
        return gx, gy

    # (мышь dx, мышь dy): туда и обратно по каждой оси — усредняем, чтобы
    # убрать влияние движения моба и ошибки одного замера.
    moves = [(60, 0), (-60, 0), (0, 30), (0, -30)]
    gains_x: list = []
    gains_y: list = []

    print("  Калибровка: НЕ трогай мышь и клавиатуру. Цель должна стоять и быть в кадре.")
    before = read_offset()
    if before is None:
        print("  Цель не найдена Vision-ом. Выбери моба (Tab), чтобы был виден его HP-бар, и повтори.")
        return

    for dx, dy in moves:
        _sweep(be, dx, dy)
        _wait(0.6)  # даём камере игры доехать (в игре есть своё сглаживание)
        after = read_offset()
        if after is None:
            print(f"  Цель пропала из кадра после сдвига {dx},{dy} — уменьши сдвиг в moves или встань ближе.")
            return
        gx, gy = _gain_from(before, after, dx, dy)
        if gx is not None:
            gains_x.append(gx)
            print(f"  мышь dx={dx:+d}: цель сдвинулась на {after[0] - before[0]:+.0f} px экрана -> gain_x = {gx:+.2f}")
        if gy is not None:
            gains_y.append(gy)
            print(f"  мышь dy={dy:+d}: цель сдвинулась на {after[1] - before[1]:+.0f} px экрана -> gain_y = {gy:+.2f}")
        before = after

    gx = statistics.median(gains_x)
    gy = statistics.median(gains_y)
    print("\n  РЕЗУЛЬТАТ")
    if abs(gx) < 0.2 or abs(gy) < 0.2:
        print("  Камера почти не сдвинулась: либо игра игнорирует мышь из Python, либо цель вне кадра. "
              "Сначала проверь `mouse`.")
        return
    invert_y = gy < 0
    print(f"  В input_manager.py -> AimTuning поставь:  gain_x = {abs(gx):.2f}   gain_y = {abs(gy):.2f}")
    if invert_y:
        print("  Ось Y у тебя ИНВЕРТИРОВАНА в игре: поставь ещё InputManager._CAMERA_INVERT_Y = True")
    if gx < 0:
        print("  ВНИМАНИЕ: gain_x отрицательный — камера крутится в обратную сторону. Напиши мне.")


def stage_aim() -> None:
    # Полный путь камеры: InputManager._aim_worker (пружина + демпфер +
    # дрейф вокруг цели) с искусственной "целью", которая качается влево-
    # вправо на +-120 px. Камера должна плавно следовать за ней, а не
    # дёргаться. Цель здесь НЕ связана с реальной камерой (это просто
    # заданная синусоида), поэтому смотри только на ПЛАВНОСТЬ движения.
    # Подбор: плавность/скорость — AimTuning.omega; амплитуда "дыхания" —
    # AimTuning.wander_std; ЕСЛИ камера крутится слишком быстро/медленно
    # относительно ожидаемого — сначала запусти `calibrate`.
    import math
    im = InputManager()
    print("  камера качается влево-вправо 8 секунд (F4 — стоп)")
    t0 = time.monotonic()
    try:
        while time.monotonic() - t0 < 8.0:
            if _f4_pressed():
                break
            t = time.monotonic() - t0
            im.aim_with_stick(120.0 * math.sin(t * 1.2), 0.0)
            time.sleep(1 / 60)
    finally:
        im.halt_immediately()
        im.stop()


def stage_turn(be: Win32Backend) -> None:
    # Подбор ScanTuning.full_turn_px: сколько px мыши даёт ровно 360° в игре.
    # Камера плавно (шагами по ~12 px) поворачивается вправо на заданное
    # число px. Запомни, на что смотрела камера в начале (дерево, стена),
    # и сравни с концом: если не добралась до той же точки — число мало,
    # если пролетела дальше — велико. Пересчёт: новое = старое * (360 /
    # реальный поворот в градусах). Число передай вторым аргументом, например:
    #     python smoke_test_input.py turn 5000
    from src.core.input_manager import ScanTuning

    px = int(float(sys.argv[2])) if len(sys.argv) > 2 else int(ScanTuning().full_turn_px)
    steps = max(1, abs(px) // 12)
    print(f"  камера плавно поворачивается вправо на {px} px мыши (~{steps * 0.02:.0f} с). Следи, где остановится.")
    _sweep(be, px, 0, steps=steps)
    _wait(1.0)
    print("  Готово. Вернулась ровно в исходный вид — число px = полный оборот; иначе подправь (см. описание).")


def stage_scan() -> None:
    # Осмотр местности (фаза SCAN в SEARCH): InputManager.start_scan() водит
    # камеру плавными случайными взмахами влево-вправо, с паузами и тремором.
    # Здесь БЕЗ бота и зрения — только проверка, что камера двигается так,
    # как ты хочешь. Что смотреть: взмахи плавные (без рывков), камера
    # остаётся около стартового курса (+-700 px мыши), вертикаль почти не
    # уходит. Подбор: размах/скорость/паузы — ScanTuning в input_manager.py.
    im = InputManager()
    print("  осмотр 20 секунд (F4 — стоп)")
    im.start_scan()
    t0 = time.monotonic()
    try:
        while time.monotonic() - t0 < 20.0:
            if _f4_pressed():
                break
            time.sleep(0.05)
    finally:
        im.stop_scan()
        time.sleep(0.4)  # даём камере плавно затормозить
        im.halt_immediately()
        im.stop()


def main() -> None:
    stage = sys.argv[1] if len(sys.argv) > 1 else ""
    stages = {
        "keys": stage_keys, "wasd": stage_wasd, "attack": stage_attack,
        "tab": stage_tab, "mouse": stage_mouse, "wheel": stage_wheel,
    }
    if stage not in stages and stage not in ("aim", "scan", "turn", "calibrate", "all"):
        print(__doc__)
        return

    print(f"Кликни в окно игры. Старт через {_COUNTDOWN_S} секунд (F4 — аварийная остановка)...")
    for left in range(_COUNTDOWN_S, 0, -1):
        print(f"  {left}...")
        time.sleep(1.0)

    be = Win32Backend()
    try:
        if stage == "aim":
            stage_aim()
        elif stage == "scan":
            stage_scan()
        elif stage == "turn":
            stage_turn(be)
        elif stage == "calibrate":
            stage_calibrate(be)
        elif stage == "all":
            for name in ("keys", "wasd", "attack", "tab", "mouse"):
                print(f"\n=== {name} ===")
                stages[name](be)
        else:
            print(f"\n=== {stage} ===")
            stages[stage](be)
    except KeyboardInterrupt:
        print("\nОстановлено по F4.")
    finally:
        # Страховка: что бы ни случилось, отпускаем всё, что тест мог зажать.
        for key in ("w", "a", "s", "d", "tab", "shift", "ctrl", "alt"):
            be.key_up(key)
        be.mouse_button("left", False)
        be.mouse_button("right", False)
        print("\nГотово. Отпустил всё зажатое.")


if __name__ == "__main__":
    main()
