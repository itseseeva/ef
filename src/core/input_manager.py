import time
import random
import threading
import queue
import interception

class InputManager:
    """
    Менеджер ввода через драйвер Interception.
    Относится к инфраструктурному слою (база для всех FSM состояний).
    """
    def __init__(self):
        # queue.Queue потокобезопасна. Защищает основной цикл от микрофризов.
        self._queue = queue.Queue()
        self._running = True
        # daemon=True гарантирует, что поток умрет при закрытии основной программы
        self._worker_thread = threading.Thread(target=self._worker, daemon=True)
        self._worker_thread.start()

    def _worker(self):
        """Фоновый воркер, который читает команды и выполняет их с задержками."""
        while self._running:
            try:
                task = self._queue.get(timeout=0.1)
                if task is None:
                    continue
                
                action, args = task
                if action == "press":
                    self._humanized_press(*args)
                elif action == "click":
                    self._humanized_click(*args)
                elif action == "move":
                    self._humanized_move(*args)
                    
                self._queue.task_done()
            except queue.Empty:
                continue

    def stop(self):
        """Корректная остановка фонового потока."""
        self._running = False
        self._worker_thread.join()

    def press_key(self, key: str):
        """Отправляет команду нажатия клавиши в очередь (не блокирует основной поток)."""
        self._queue.put(("press", (key,)))

    def click(self, button: str = "left"):
        """Отправляет команду клика мыши в очередь."""
        self._queue.put(("click", (button,)))

    def move_mouse(self, x: int, y: int):
        """Отправляет команду перемещения курсора (никаких хардкодных координат внутри класса)."""
        self._queue.put(("move", (x, y)))

    def _humanized_press(self, key: str):
        """
        Внутренний метод: физически зажимает кнопку.
        random.uniform(0.03, 0.08) имитирует разное время зажатия клавиши человеком,
        чтобы EAC не увидел идеальные машинные тайминги.
        """
        hold_time = random.uniform(0.03, 0.08)
        interception.press(key) # Interception-python сама может нажать, но мы можем использовать явные up/down
        # Для надежности эмуляции используем раздельные down/up, если библиотека поддерживает,
        # либо встроенный press. Библиотека interception оборачивает это внутри себя.
        # В рамках этого мока мы используем стандартный API interception-python:
        interception.key_down(key)
        time.sleep(hold_time)
        interception.key_up(key)
        
        # Микро-пауза после отпускания, чтобы два действия не "слипались" в миллисекунду
        time.sleep(random.uniform(0.01, 0.04))

    def _humanized_click(self, button: str):
        hold_time = random.uniform(0.02, 0.07)
        interception.mouse_down(button)
        time.sleep(hold_time)
        interception.mouse_up(button)
        time.sleep(random.uniform(0.01, 0.03))

    def _humanized_move(self, x: int, y: int):
        # В будущем сюда добавим кривые Безье. Пока — прямой, но асинхронный вызов.
        interception.move_to(x, y)
        time.sleep(random.uniform(0.01, 0.02))

if __name__ == "__main__":
    # ПЕСОЧНИЦА ДЛЯ ЛОКАЛЬНОГО ТЕСТИРОВАНИЯ
    # Запусти этот скрипт напрямую, открой пустой блокнот за 3 секунды,
    # и скрипт напечатает "h" через настоящий драйвер.
    print("Тестируем драйвер Interception... Открой блокнот за 3 секунды.")
    time.sleep(3)
    
    inp = InputManager()
    inp.press_key("h")
    
    # Ждем, чтобы воркер успел вытащить задачу из очереди
    time.sleep(1)
    inp.stop()
    print("Тест завершен.")
