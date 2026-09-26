"""
src/core/input_manager.py

Единственный слой ввода бота. Раньше эмулировал клавиатуру и мышь через
Interception, теперь — эмулирует виртуальный геймпад Xbox 360 через
vgamepad (пользовательская библиотека) поверх драйвера ViGEmBus (ядро).

Почему сменили архитектуру: Interception встраивается в цепочку
фильтров РЕАЛЬНЫХ устройств (клавиатуры/мыши) через реестр
(UpperFilters). Когда эта цепочка ломается — ломаются сами физические
устройства, что и произошло на практике. ViGEmBus работает иначе:
создаёт НОВОЕ отдельное виртуальное устройство, не трогая существующие —
поломка виртуального геймпада физически не может задеть настоящую
клавиатуру/мышь.

Уточнение для истории проекта: причиной поломки устройств ввода в
прошлый раз НЕ была изоляция ядра (HVCI) — лог CodeIntegrity
Operational дважды проверялся и не содержал ни одной записи о
блокировке Interception. Причина была в самой регистрации Interception
как upper filter для классов клавиатуры/мыши. На решение сменить
архитектуру это не влияет — оно верное само по себе, — но пусть в
комментариях останется точная причина, а не гипотеза, которая не
подтвердилась.
"""

import time
import random
import logging
import threading
import queue

import vgamepad as vg

logger = logging.getLogger(__name__)


def _smoothstep(t: float) -> float:
    """
    Плавная кривая ease-in-out: 0 на старте, 1 в конце, с нулевой
    скоростью на обоих концах. Та же формула, что раньше использовалась
    для доворота мыши через Безье — человеческая рука не разгоняет и не
    останавливает стик мгновенно, скачок силы 0 -> 1 за один тик выдаёт
    себя как программное действие сразу же, если логировать сырой ввод.
    """
    return 3 * t**2 - 2 * t**3


class InputManager:
    """
    Обёртка над vgamepad.VX360Gamepad с той же архитектурой безопасности,
    что была у версии на Interception: команды не выполняются напрямую
    из потока FSM, а складываются в очередь и исполняются отдельным
    воркер-потоком — так тик FSM никогда не блокируется на time.sleep()
    внутри удержания кнопки/стика на 60 FPS цикле.

    ВАЖНО про BUTTON_MAP: это ПРОВИЗОРНАЯ раскладка геймпада T&L по
    дефолтным биндам (источник — гайд Game8), НЕ подтверждённая лично
    в игре на твоей реальной раскладке. Сознательно вынесена в один
    словарь наверху класса: если в игре биндинги другие (или ты сам их
    перебинживал) — правишь только эти две строки, остальной код по
    кнопкам не завязан на конкретные имена.
    """

    BUTTON_MAP = {
        "attack": vg.XUSB_BUTTON.XUSB_GAMEPAD_RIGHT_SHOULDER,  # RB — базовая атака (дефолт)
        "target": vg.XUSB_BUTTON.XUSB_GAMEPAD_RIGHT_THUMB,     # R3 — захват/снятие цели (дефолт)
        # Кнопки для комбо скиллов (используются в execute_combo как
        # 'press_A', 'press_X' и т.д.) — правь под свою раскладку.
        "A": vg.XUSB_BUTTON.XUSB_GAMEPAD_A,
        "B": vg.XUSB_BUTTON.XUSB_GAMEPAD_B,
        "X": vg.XUSB_BUTTON.XUSB_GAMEPAD_X,
        "Y": vg.XUSB_BUTTON.XUSB_GAMEPAD_Y,
        # "Сырые" имена модификаторов-кнопок (в отличие от LT/RT — это
        # ЦИФРОВЫЕ кнопки, не аналоговые оси, поэтому им место здесь, а не
        # в _TRIGGER_SETTERS). "RB" — тот же физический бинд, что и
        # "attack" выше (одна и та же кнопка XUSB_GAMEPAD_RIGHT_SHOULDER,
        # просто два имени для двух разных целей: "attack" — семантический
        # алиас для автоатаки в bot.py, "RB" — техническое имя, которое
        # пишет пользователь в строке комбо вроде "RB+A" и понимает
        # parse_combo_string). Дублирование значения в словаре безопасно —
        # это просто два ключа на одно и то же значение, не конфликт.
        "RB": vg.XUSB_BUTTON.XUSB_GAMEPAD_RIGHT_SHOULDER,
        "LB": vg.XUSB_BUTTON.XUSB_GAMEPAD_LEFT_SHOULDER,
        # Крестовина — 4 отдельные "кнопки" в терминах XInput, а не единый
        # аналоговый стик, поэтому легко ложатся в тот же BUTTON_MAP, что
        # и A/B/X/Y, без отдельной инфраструктуры.
        "DPAD_UP": vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_UP,
        "DPAD_DOWN": vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_DOWN,
        "DPAD_LEFT": vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_LEFT,
        "DPAD_RIGHT": vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_RIGHT,
    }

    # Курки LT/RT — модификаторы для скиллов. ВАЖНО: в отличие от кнопок
    # выше, курки в терминах XInput/vgamepad — это НЕ бинарные
    # press/release, а аналоговые оси силы нажатия (0.0..1.0). У
    # vgamepad для них отдельные методы (left_trigger_float /
    # right_trigger_float), а не press_button/release_button — поэтому
    # они не влезают в BUTTON_MAP как есть и обрабатываются отдельной
    # веткой в _do_execute_combo. Значение — имя метода VX360Gamepad,
    # который выставляет силу нажатия этого курка.
    _TRIGGER_SETTERS = {
        "LT": "left_trigger_float",
        "RT": "right_trigger_float",
    }

    # Сколько мс удерживаем кнопку нажатой перед отпусканием. Диапазон,
    # а не фиксированное число — та же анти-детект логика, что была у
    # удержания клавиш раньше: одинаковая длительность нажатия каждый
    # раз — статистически заметный паттерн для анализа поведения.
    _HOLD_BUTTON_MS = (60, 140)

    # Случайная пауза ПЕРЕД самим нажатием кнопки — имитация времени
    # человеческой реакции. Без неё кнопка нажималась бы ровно в тот
    # программный тик, когда FSM приняла решение — идеальная синхронность
    # "решение -> действие" без единой миллисекунды задержки физически
    # недостижима для человека и является явным паттерном для анализа.
    _REACTION_DELAY_S = (0.0, 0.05)

    # Сколько мс держим стик отклонённым НА ПИКЕ силы (после разгона,
    # до начала торможения) за один "довод" камеры. Стик — это НЕ дельта,
    # как было у мыши (bezier_move_relative двигал курсор ровно на dx,dy
    # и всё). Стик — это скорость поворота: чем дольше держишь отклонённым
    # и чем сильнее отклонение, тем больше суммарный доворот камеры.
    # Поэтому здесь два параметра (сила + время), а не один.
    _AIM_HOLD_MS = (40, 90)

    # Длительность плавного разгона стика от нуля до целевой силы и
    # симметричного торможения обратно в ноль. Реальная рука не
    # телепортирует стик в крайнее положение мгновенно — она проходит
    # через промежуточные отклонения, как и с движением мыши по Безье.
    _AIM_RAMP_MS = (50, 90)

    # Сколько промежуточных шагов делаем за время разгона/торможения.
    # Больше шагов — плавнее кривая, но больше вызовов update() за то же
    # время. 4-7 достаточно, чтобы кривая не выглядела скачком, и мало
    # для того, чтобы создать заметную нагрузку на 60 FPS цикл.
    _AIM_RAMP_STEPS = (4, 7)

    # Во время удержания на пике добавляем несколько микро-тиков с
    # небольшим случайным дрожанием вокруг целевой силы — человеческий
    # большой палец на стике никогда не держит идеально одно и то же
    # положение, там всегда есть микротремор.
    _AIM_JITTER_TICKS = (2, 4)
    _AIM_JITTER_AMOUNT = 0.05

    # Пиксельное смещение таргета от центра ROI, при котором сила
    # отклонения стика достигает максимума (1.0). Берём половину ширины
    # ROI таргета (у нас ROI 300px, см. vision_config.TARGET_HP_WIDTH)
    # как естественный масштаб: цель редко уезжает дальше половины зоны
    # обнаружения, не потеряв трекинг совсем.
    _AIM_MAX_OFFSET_PX = 150.0

    # Минимальная сила отклонения стика, гарантированная даже для
    # маленького offset — у контроллеров и в самой игре обычно есть своя
    # мёртвая зона стика, и слишком слабое отклонение просто не даст
    # эффекта на экране. Без этого пола мелкие коррекции были бы "немыми".
    _AIM_MIN_FORCE = 0.15

    # Пауза МЕЖДУ шагами комбо (например, между 'hold_LT' и 'press_X').
    # Это не только анти-детект: если отправить нажатие кнопки СРАЗУ же
    # вслед за зажатием курка в одном программном такте, игра физически
    # может не успеть зарегистрировать модификатор и не засчитать
    # комбинацию как скилл — это чисто игровое требование к таймингу,
    # а не только маскировка под человека.
    _COMBO_STEP_DELAY_S = (0.03, 0.07)

    def __init__(self) -> None:
        self._gamepad = vg.VX360Gamepad()

        # "Холостой" update() СРАЗУ после создания устройства — толкаем в
        # Windows/игру нейтральное состояние (ничего не нажато, стики в
        # нуле) ДО того, как туда полетит первая настоящая команда. Без
        # этого самый первый update(), который вообще видит ОС от этого
        # устройства, — это уже нажатие кнопки в составе первого боевого
        # комбо, а игра в этот момент может ещё не закончить распознавание
        # свежесозданного XInput-устройства и просто пропустить пакет.
        # Один лишний вызов ничего не стоит, а первый реальный press_button
        # после него уже летит на устройство, которое система "видела"
        # хотя бы раз.
        self._gamepad.update()

        self._queue: "queue.Queue[tuple]" = queue.Queue()
        self._running = True
        self._abort_event = threading.Event()

        # daemon=True — тот же приём, что и раньше: поток ввода не
        # должен мешать процессу завершиться, если что-то пойдёт не так.
        self._worker_thread = threading.Thread(
            target=self._worker, daemon=True, name="InputWorker"
        )
        self._worker_thread.start()
        logger.debug("InputManager (vgamepad) инициализирован, воркер запущен.")

    # ================= Публичное API =================

    def press_button(self, action: str) -> None:
        """
        Ставит в очередь нажатие+отпускание кнопки геймпада по имени
        действия ('attack', 'target', ...) — см. BUTTON_MAP.
        """
        if action not in self.BUTTON_MAP:
            logger.error(
                "InputManager: неизвестное действие '%s' — нет в BUTTON_MAP.", action
            )
            return
        self._queue.put(("press_button", action))

    def aim_with_stick(self, dx: float, dy: float) -> None:
        """
        Ставит в очередь коррекцию камеры правым стиком на основе
        смещения таргета (dx, dy) в пикселях от центра ROI — тот же
        сигнал, что раньше шёл напрямую в bezier_move_relative.
        """
        self._queue.put(("aim_with_stick", dx, dy))

    def execute_combo(self, actions: list[str]) -> None:
        """
        Ставит в очередь ЦЕЛУЮ комбинацию как ОДНУ задачу для воркера —
        например ['hold_LT', 'press_X', 'release_LT']. Формат строки:
        '<глагол>_<цель>', где глагол — 'hold'/'release' (для курков
        LT/RT) или 'press' (для кнопок A/B/X/Y и любых из BUTTON_MAP).

        Почему одна задача в очереди, а не три отдельных вызова
        press_button/hold-курок по отдельности: очередь у нас одна на
        весь InputManager, и обрабатывается строго по одной команде за
        раз одним воркер-потоком. Если бы шаги комбо шли отдельными
        элементами очереди, между 'hold_LT' и 'press_X' могла бы
        вклиниться, например, параллельная команда aim_with_stick от
        коррекции прицела — и сломать тайминг модификатор+кнопка.
        Упаковав всю комбинацию в один элемент очереди, мы гарантируем,
        что она выполнится ПОДРЯД, без чужого вмешательства между шагами.
        """
        self._queue.put(("execute_combo", list(actions)))

    @staticmethod
    def parse_combo_string(spec: str) -> list[str]:
        """
        Переводит короткую человекочитаемую строку комбо (например "RB+A",
        "RT+DPAD_UP" или просто "X") в список шагов внутреннего формата,
        который понимает execute_combo(). Это ЕДИНСТВЕННОЕ место в проекте,
        которое умеет делать этот перевод — и bot.py (боевая ротация), и
        F5-тест в main.py читают skills_config.json и сразу прогоняют
        значение поля "combo" через эту функцию, а не пишут список шагов
        руками в файле — так исключается рассинхрон между тем, что человек
        написал в конфиге, и тем, что реально уйдёт на геймпад.

        Формат: "МОДИФИКАТОР+КНОПКА" (один модификатор максимум — T&L не
        использует комбинации из двух модификаторов сразу) или голая
        "КНОПКА" без модификатора. Порядок шагов hold -> press -> release
        генерируется ВСЕГДА одинаково — человеку, который просто пишет
        "RB+A" в JSON, физически негде забыть отпустить модификатор,
        в отличие от ручного списка ['hold_RB', 'press_A'] без release.
        """
        parts = [p.strip().upper() for p in spec.split("+") if p.strip()]

        if not parts:
            raise ValueError(f"Пустая строка комбо: '{spec}'")

        known_targets = set(InputManager.BUTTON_MAP) | set(InputManager._TRIGGER_SETTERS)
        for part in parts:
            if part not in known_targets:
                raise ValueError(
                    f"В комбо '{spec}' неизвестная кнопка/модификатор '{part}' — "
                    f"нет ни в BUTTON_MAP, ни в _TRIGGER_SETTERS."
                )

        if len(parts) == 1:
            return [f"press_{parts[0]}"]

        if len(parts) == 2:
            modifier, button = parts
            return [f"hold_{modifier}", f"press_{button}", f"release_{modifier}"]

        raise ValueError(
            f"Комбо '{spec}' содержит больше одного модификатора — "
            f"в T&L на геймпаде это не встречается, проверь конфиг."
        )

    def halt_immediately(self) -> None:
        """
        Экстренная остановка (F4-пауза). Прерывает текущее удержание
        кнопки/стика, выбрасывает всё, что ждёт в очереди, и ДОПОЛНИТЕЛЬНО
        принудительно обнуляет состояние геймпада прямо отсюда, не
        полагаясь только на то, что воркер сам дойдёт до своего кода
        освобождения. Дублирование дешёвое, а цена залипшего виртуального
        стика — персонаж бесконечно крутит камеру или долбит атаку уже
        после паники — того не стоит.
        """
        self._abort_event.set()
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except queue.Empty:
                break

        try:
            self._gamepad.reset()
            self._gamepad.update()
        except Exception as e:
            logger.error("InputManager: ошибка при экстренном сбросе геймпада: %s", e)

    def reset_abort(self) -> None:
        """Снимает флаг экстренной остановки перед следующим стартом бота."""
        self._abort_event.clear()

    def stop(self) -> None:
        """Штатная остановка — дожидается опустошения очереди, затем гасит воркер."""
        self._queue.join()
        self._running = False
        self._gamepad.reset()
        self._gamepad.update()
        logger.debug("InputManager остановлен штатно.")

    # ================= Внутреннее =================

    def _interruptible_sleep(self, seconds: float) -> bool:
        """True если проспали весь интервал, False если прервано halt_immediately()."""
        return not self._abort_event.wait(timeout=seconds)

    def _worker(self) -> None:
        while self._running:
            try:
                cmd = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue

            try:
                if cmd[0] == "press_button":
                    self._do_press_button(cmd[1])
                elif cmd[0] == "aim_with_stick":
                    self._do_aim_with_stick(cmd[1], cmd[2])
                elif cmd[0] == "execute_combo":
                    self._do_execute_combo(cmd[1])
            except Exception as e:
                logger.error("InputManager: ошибка выполнения команды: %s", e)
            finally:
                self._queue.task_done()

            # Пауза между командами — тоже прерываемая, как и раньше.
            self._interruptible_sleep(random.uniform(0.02, 0.05))

    def _do_press_button(self, action: str) -> None:
        button = self.BUTTON_MAP[action]

        # Пауза перед нажатием — см. докстринг _REACTION_DELAY_S. Если
        # паника случилась ЕЩЁ ДО того, как кнопка физически нажата —
        # отпускать нечего, просто выходим, не трогая геймпад.
        if not self._interruptible_sleep(random.uniform(*self._REACTION_DELAY_S)):
            return

        hold_s = random.uniform(*self._HOLD_BUTTON_MS) / 1000.0

        self._gamepad.press_button(button=button)
        self._gamepad.update()

        self._interruptible_sleep(hold_s)

        # Отпускаем БЕЗУСЛОВНО, даже если удержание было прервано паникой —
        # тот же принцип, что раньше был с key_up/mouse_up: застрявшая
        # нажатой кнопка геймпада эквивалентна залипшей клавише в игре.
        self._gamepad.release_button(button=button)
        self._gamepad.update()

    def _do_aim_with_stick(self, dx: float, dy: float) -> None:
        # Нормализуем пиксельное смещение в силу отклонения [-1.0, 1.0].
        # Используем float-API vgamepad (right_joystick_float), а не
        # сырые int16 (-32768..32767) — так не нужно вручную считать
        # округления, и код читается как "доля от максимума", а не как
        # магическое число из непонятного диапазона.
        target_x = self._offset_to_stick_force(dx)
        # offset_y в системе координат экрана растёт ВНИЗ, а "+1" на
        # стике обычно означает "вверх/от себя" — инвертируем, иначе
        # камера будет доворачиваться по вертикали в обратную сторону.
        target_y = -self._offset_to_stick_force(dy)

        steps = random.randint(*self._AIM_RAMP_STEPS)
        ramp_s = random.uniform(*self._AIM_RAMP_MS) / 1000.0
        step_s = ramp_s / steps

        # --- Разгон: плавно доводим стик от нуля до целевой силы по
        # smoothstep-кривой, а не одним скачком. ---
        aborted = False
        for i in range(1, steps + 1):
            eased = _smoothstep(i / steps)
            self._gamepad.right_joystick_float(
                x_value_float=target_x * eased, y_value_float=target_y * eased
            )
            self._gamepad.update()
            if not self._interruptible_sleep(step_s):
                aborted = True
                break

        # --- Удержание на пике с микро-дрожанием — см. _AIM_JITTER_*. ---
        if not aborted:
            hold_s = random.uniform(*self._AIM_HOLD_MS) / 1000.0
            ticks = random.randint(*self._AIM_JITTER_TICKS)
            tick_s = hold_s / ticks
            for _ in range(ticks):
                jitter_x = random.uniform(-self._AIM_JITTER_AMOUNT, self._AIM_JITTER_AMOUNT)
                jitter_y = random.uniform(-self._AIM_JITTER_AMOUNT, self._AIM_JITTER_AMOUNT)
                self._gamepad.right_joystick_float(
                    x_value_float=max(-1.0, min(1.0, target_x + jitter_x)),
                    y_value_float=max(-1.0, min(1.0, target_y + jitter_y)),
                )
                self._gamepad.update()
                if not self._interruptible_sleep(tick_s):
                    aborted = True
                    break

        # Если прервано паникой — сразу в ноль, тут важна скорость
        # реакции на F4, а не реализм торможения.
        if aborted:
            self._gamepad.right_joystick_float(x_value_float=0.0, y_value_float=0.0)
            self._gamepad.update()
            return

        # --- Торможение: симметричный плавный возврат в центр. ---
        for i in range(1, steps + 1):
            eased = _smoothstep(i / steps)
            self._gamepad.right_joystick_float(
                x_value_float=target_x * (1 - eased), y_value_float=target_y * (1 - eased)
            )
            self._gamepad.update()
            if not self._interruptible_sleep(step_s):
                break

        # Финальная страховка: ровно ноль, независимо от накопленной
        # погрешности округления в шагах разгона/торможения выше.
        self._gamepad.right_joystick_float(x_value_float=0.0, y_value_float=0.0)
        self._gamepad.update()

    def _do_execute_combo(self, actions: list[str]) -> None:
        # Кнопки-модификаторы (RB/LB), которые этот вызов ЗАЖАЛ через
        # "hold_" и ещё не отпустил "release_" внутри списка шагов.
        # Нужен отдельный учёт от курков: у курков всего два варианта
        # (LT/RT) и их можно просто обнулить по имени в конце, а кнопок
        # потенциально несколько разных — проще собирать множество того,
        # что реально нажато, и отпустить именно это на выходе.
        held_buttons: set = set()

        for action in actions:
            verb, _, target = action.partition("_")

            if verb == "hold" and target in self._TRIGGER_SETTERS:
                getattr(self._gamepad, self._TRIGGER_SETTERS[target])(value_float=1.0)
                self._gamepad.update()

            elif verb == "release" and target in self._TRIGGER_SETTERS:
                getattr(self._gamepad, self._TRIGGER_SETTERS[target])(value_float=0.0)
                self._gamepad.update()

            elif verb == "hold" and target in self.BUTTON_MAP:
                # В отличие от "press" ниже — здесь НЕ отпускаем кнопку
                # сразу: она должна оставаться зажатой как модификатор,
                # пока не встретится её собственный "release_" шаг (или
                # пока не сработает страховка в конце метода).
                button = self.BUTTON_MAP[target]
                self._gamepad.press_button(button=button)
                self._gamepad.update()
                held_buttons.add(button)

            elif verb == "release" and target in self.BUTTON_MAP:
                button = self.BUTTON_MAP[target]
                self._gamepad.release_button(button=button)
                self._gamepad.update()
                held_buttons.discard(button)

            elif verb == "press" and target in self.BUTTON_MAP:
                button = self.BUTTON_MAP[target]
                self._gamepad.press_button(button=button)
                self._gamepad.update()
                # Удержание кнопки внутри комбо — та же случайная
                # длительность, что и у одиночного press_button, чтобы
                # шаг комбо не отличался статистически от обычной атаки.
                self._interruptible_sleep(random.uniform(*self._HOLD_BUTTON_MS) / 1000.0)
                self._gamepad.release_button(button=button)
                self._gamepad.update()

            else:
                logger.error(
                    "InputManager: неизвестный шаг комбо '%s' — пропущен.", action
                )
                continue

            # Микро-пауза между шагами — см. докстринг _COMBO_STEP_DELAY_S.
            # Если паника случилась посреди комбо — прерываем оставшиеся
            # шаги (break), но ОБЯЗАТЕЛЬНО отпускаем всё зажатое ниже.
            if not self._interruptible_sleep(random.uniform(*self._COMBO_STEP_DELAY_S)):
                break

        # Страховка на выходе — БЕЗУСЛОВНО отпускаем и курки, и ЛЮБЫЕ
        # кнопки-модификаторы, оставшиеся зажатыми (held_buttons), даже
        # если паника прервала выполнение на середине или в списке шагов
        # была ошибка. Застрявшая зажатой кнопка-модификатор опаснее всего
        # остального: в игре это обычно переключатель режима, который
        # будет менять смысл ЛЮБОГО следующего нажатия, пока кто-то не
        # заметит и не перезапустит бота вручную.
        for button in held_buttons:
            self._gamepad.release_button(button=button)
        self._gamepad.left_trigger_float(value_float=0.0)
        self._gamepad.right_trigger_float(value_float=0.0)
        self._gamepad.update()

    def _offset_to_stick_force(self, offset_px: float) -> float:
        """Пиксельное смещение -> сила отклонения стика в диапазоне [-1.0, 1.0]."""
        if offset_px == 0:
            return 0.0
        magnitude = min(abs(offset_px) / self._AIM_MAX_OFFSET_PX, 1.0)
        magnitude = max(magnitude, self._AIM_MIN_FORCE)
        return magnitude if offset_px > 0 else -magnitude


if __name__ == "__main__":
    # Песочница: без реальной игры, просто проверяем, что виртуальный
    # геймпад создаётся и принимает команды без исключений. Реальную
    # реакцию смотри через встроенную утилиту Windows проверки
    # джойстиков (Win+R -> joy.cpl) — там должен появиться "Xbox 360
    # Controller for Windows" в момент запуска этого файла, и его
    # стик/кнопки будут шевелиться синхронно с командами ниже.
    logging.basicConfig(level=logging.DEBUG)

    # Тест parse_combo_string — чистая функция без геймпада и без очереди,
    # поэтому её удобно проверить прямо здесь текстом, не разворачивая игру.
    print("Тест: parse_combo_string('RB+A') =", InputManager.parse_combo_string("RB+A"))
    print("Тест: parse_combo_string('X') =", InputManager.parse_combo_string("X"))
    try:
        InputManager.parse_combo_string("RB+LB+A")
    except ValueError as e:
        print("Тест: parse_combo_string('RB+LB+A') корректно упал с ошибкой:", e)

    im = InputManager()
    print("Виртуальный геймпад создан. Открой joy.cpl и посмотри на устройство.")
    time.sleep(2)

    print("Тест: press_button('attack')")
    im.press_button("attack")
    time.sleep(0.5)

    print("Тест: aim_with_stick(+100, 0) — доворот вправо")
    im.aim_with_stick(100, 0)
    time.sleep(0.5)

    print("Тест: aim_with_stick(0, -80) — доворот вверх (offset_y отрицательный)")
    im.aim_with_stick(0, -80)
    time.sleep(0.5)

    print("Тест: execute_combo(['hold_LT', 'press_X', 'release_LT'])")
    im.execute_combo(["hold_LT", "press_X", "release_LT"])
    time.sleep(0.5)

    im.stop()
    print("Песочница завершена.")
