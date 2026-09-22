import time
import random
import threading
import queue
import logging

import interception

logger = logging.getLogger(__name__)


class InputManager:
    """
    Инфраструктурный модуль эмуляции ввода через драйвер Interception.
    Используется всеми FSM-состояниями (COMBAT, LOOT, ESCAPE и др.).

    Humanizer-логика:
        Каждая команда (нажатие, клик, движение) выполняется в фоновом потоке
        через очередь. Это защищает основной игровой цикл от микрофризов.

        Между press и release — рандомная пауза 30–80 мс (имитация пальца).
        Между разными командами — рандомная пауза 10–40 мс (имитация паузы руки).
        Без этих задержек EAC видит машинные тайминги и банит.

    Запрещено:
        pyautogui, keyboard, mouse — детектируются EAC на уровне API Windows.
    """

    # Границы задержек вынесены в константы класса —
    # так их легко найти и подправить без погружения в логику методов.
    _HOLD_KEY_MS   = (0.030, 0.080)   # время зажатия клавиши
    _HOLD_CLICK_MS = (0.020, 0.070)   # время зажатия кнопки мыши
    _AFTER_CMD_MS  = (0.010, 0.040)   # пауза МЕЖДУ командами (анти-EAC)

    def __init__(self):
        # queue.Queue потокобезопасна «из коробки» — не нужен Lock вручную.
        self._queue: queue.Queue = queue.Queue()
        self._running: bool = True

        # daemon=True: поток автоматически умирает вместе с основной программой.
        # Без этого Python не завершится, пока воркер жив.
        self._worker_thread = threading.Thread(
            target=self._worker, daemon=True, name="InputWorker"
        )
        self._worker_thread.start()

    # ------------------------------------------------------------------
    # Публичный API — вызывается из FSM-состояний
    # ------------------------------------------------------------------

    def press_key(self, key: str) -> None:
        """Поставить нажатие клавиши в очередь. Не блокирует вызывающий поток."""
        self._queue.put(("press", key))

    def click(self, button: str = "left") -> None:
        """Поставить клик мыши в очередь."""
        self._queue.put(("click", button))

    def move_mouse(self, x: int, y: int) -> None:
        """
        Поставить перемещение курсора в очередь.
        Координаты всегда передаются снаружи — никаких хардкодных значений здесь.

        TODO (Этап 3): заменить прямое перемещение на кривые Безье.
              Прямой прыжок из точки A в B — детектируется EAC.
              Модуль: core/input/bezier.py → метод bezier_move(x, y, duration).
        """
        self._queue.put(("move", x, y))

    def stop(self) -> None:
        """Корректная остановка: дождаться завершения всех команд в очереди."""
        # join() блокирует до тех пор, пока очередь не опустеет.
        # Только после этого ставим флаг — иначе воркер может бросить задачи.
        self._queue.join()
        self._running = False
        self._worker_thread.join()

    # ------------------------------------------------------------------
    # Внутренняя логика воркера
    # ------------------------------------------------------------------

    def _worker(self) -> None:
        """
        Фоновый поток, который последовательно выполняет команды из очереди.
        try/except вокруг каждой задачи — защита от падения воркера.
        Если одна команда упадёт (драйвер недоступен), остальные продолжат выполняться.
        """
        while self._running:
            try:
                # timeout=0.1: воркер не висит вечно если очередь пуста —
                # каждые 100 мс проверяет флаг _running для корректного завершения.
                task = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue

            try:
                action = task[0]
                if action == "press":
                    self._do_press(task[1])
                elif action == "click":
                    self._do_click(task[1])
                elif action == "move":
                    self._do_move(task[1], task[2])
                else:
                    logger.warning("InputManager: неизвестная команда '%s'", action)
            except Exception as e:
                # Логируем ошибку, но НЕ роняем воркер.
                # Без этого блока одна ошибка драйвера убьёт весь ввод тихо.
                logger.error("InputManager: ошибка выполнения команды: %s", e)
            finally:
                # task_done() обязателен — иначе queue.join() в stop() зависнет навсегда.
                self._queue.task_done()

            # Пауза МЕЖДУ командами — ключевой элемент humanizer-а.
            # EAC анализирует не только длительность нажатий, но и ритм между ними.
            time.sleep(random.uniform(*self._AFTER_CMD_MS))

    def _do_press(self, key: str) -> None:
        """
        Физически нажимает и отпускает клавишу с рандомной задержкой.
        ВАЖНО: только key_down + sleep + key_up — без лишних вызовов press().
        Дублирующий вызов interception.press() до key_down был багом:
        клавиша срабатывала дважды за одну команду.
        """
        hold = random.uniform(*self._HOLD_KEY_MS)
        interception.key_down(key)
        time.sleep(hold)
        interception.key_up(key)

    def _do_click(self, button: str) -> None:
        """Нажимает и отпускает кнопку мыши с рандомной задержкой."""
        hold = random.uniform(*self._HOLD_CLICK_MS)
        interception.mouse_down(button)
        time.sleep(hold)
        interception.mouse_up(button)

    def _do_move(self, x: int, y: int) -> None:
        """
        Прямое перемещение курсора — временная реализация.
        Заменить на Безье в Этапе 3 (см. TODO в move_mouse).
        """
        interception.move_to(x, y)
        # Микро-пауза после перемещения — рука не останавливается мгновенно.
        time.sleep(random.uniform(0.010, 0.025))


# ----------------------------------------------------------------------
# ПЕСОЧНИЦА ДЛЯ ЛОКАЛЬНОГО ТЕСТИРОВАНИЯ
# Запусти этот файл напрямую: python input_manager.py
# За 3 секунды открой Блокнот — скрипт напечатает букву "h" через драйвер.
# Это проверяет связь с Interception без входа в игру.
# ----------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    print("Тест драйвера Interception...")
    print("Открой Блокнот — через 3 секунды скрипт нажмёт 'h'.")
    time.sleep(3)

    inp = InputManager()
    inp.press_key("h")

    # Ждём завершения очереди перед выходом.
    # Без этого программа закроется раньше, чем воркер выполнит команду.
    inp.stop()
    print("Тест завершён. Если в Блокноте появилась 'h' — драйвер работает.")