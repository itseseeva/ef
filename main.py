"""
main.py

Точка входа проекта. Создаёт FarmBot и даёт безопасно управлять им через
горячие клавиши — без выхода из игры и без переключения окна на консоль:
  F4 — запуск/остановка боевого цикла.
  F5 — диагностический прогон: по очереди, САМ, без остановок, отправляет
       на геймпад ВСЕ комбо из skills_config.json с фиксированной паузой
       между ними, логируя каждый шаг в терминал и в файл. Специально БЕЗ
       вопросов в терминале между шагами — раньше это заставляло уходить
       из игры в консоль между каждым скиллом, и игра из-за потери фокуса
       не видела часть нажатий. Теперь во время теста нужно просто
       смотреть в игру и записывать самому, что сработало, не отвлекаясь
       на терминал; таймкоды в файле лога (logs/combo_test_*.txt) потом
       помогают сверить свои записи по порядку.
"""

import os
import time
import ctypes
import logging
import threading

from src.core.bot import FarmBot

logger = logging.getLogger(__name__)

# Виртуальные коды клавиш (WinAPI VK-код). Таблица функциональных клавиш в
# WinAPI идёт подряд: VK_F1=0x70, VK_F2=0x71, ..., VK_F4=0x73, VK_F5=0x74 —
# поменяй на другие функциональные клавиши простой заменой этих чисел, если
# F4/F5 заняты чем-то в игре.
HOTKEY_TOGGLE_VK = 0x73  # F4 — старт/стоп бота
HOTKEY_TEST_VK = 0x74    # F5 — тест-прогон комбо из skills_config.json

# Как часто опрашиваем состояние клавиш. 50 мс — с большим запасом
# достаточно для реакции человека на кнопку; это НЕ имеет отношения к
# 60 FPS циклу самого бота — тот крутится в отдельном потоке и не зависит
# от частоты опроса хоткеев.
_POLL_INTERVAL_SEC = 0.05

# Пауза ОДИН РАЗ при старте программы, сразу после создания виртуального
# геймпада (см. main() ниже) — даёт Windows/игре время фактически
# распознать новое XInput-устройство. Специально не привязана к F4/F5:
# к моменту, когда ты реально нажмёшь любой из хоткеев, эта пауза почти
# наверняка уже прошла сама по себе (нужно время, чтобы переключиться в
# игру) — вешать такую же паузу ЕЩЁ РАЗ на каждое нажатие F5 значило бы
# тратить эти 2 секунды впустую при каждом тесте.
_GAMEPAD_WARMUP_DELAY_SEC = 2.0

# Фиксированная пауза между шагами F5-теста. БЕЗ ожидания ввода от
# пользователя (обсуждали: интерактивный input() между шагами заставлял
# уходить из игры в терминал после каждого комбо, а без фокуса на игре
# часть нажатий там просто не регистрировалась — это была причина
# "кнопки не нажимаются", не сам гейм-код). 5с даёт время заметить и
# записать результат на бумаге, оставаясь в игре, не трогая клавиатуру.
_TEST_STEP_DELAY_SEC = 5.0


def _is_key_down(vk_code: int) -> bool:
    """
    Проверяет, зажата ли клавиша ПРЯМО СЕЙЧАС — через GetAsyncKeyState.

    Это ЧТЕНИЕ состояния клавиатуры (тот же принцип, что _get_cursor_pos в
    input_manager.py — чтение состояния ОС, не инъекция ввода), под запрет
    на pyautogui/keyboard/mouse не попадает. Сознательно НЕ используется
    пакет `keyboard`: он в списке запрещённых и ставит глобальный
    низкоуровневый хук на клавиатуру (SetWindowsHookEx) — это более заметный
    системный след, чем разовый опрос состояния одной клавиши.

    Почему именно GetAsyncKeyState, а не что-то завязанное на окно консоли:
    она работает ГЛОБАЛЬНО, вне зависимости от того, какое окно сейчас
    активно. Паника-кнопка, которая реагирует только когда фокус на
    консоли, а не на игре, — бесполезна: в момент, когда она реально нужна,
    активна будет игра.

    Бит 0x8000 в результате GetAsyncKeyState означает "клавиша зажата прямо
    сейчас". Есть ещё младший бит ("была нажата с прошлого опроса"), но он
    нам не нужен — фронт нажатия (переход "не зажата" -> "зажата") ловим
    сами в main(), сравнивая с состоянием на предыдущем опросе.
    """
    return bool(ctypes.windll.user32.GetAsyncKeyState(vk_code) & 0x8000)


def _make_test_logger() -> logging.Logger:
    """
    Отдельный именованный logger ("combo_test") для F5, а не запись через
    logger модуля напрямую — так у КАЖДОГО прогона теста свой файл с
    таймстампом в имени в папке logs/, не перезаписывающий предыдущие
    прогоны и не смешанный построчно с обычным логом бота.

    Дублировать настройку вывода в терминал не нужно: propagate=True —
    поведение logging по умолчанию, специально не отключаем — означает,
    что запись, отправленная в "combo_test", ВСЕГДА поднимается и до
    корневого logger тоже, а на нём уже висит консольный handler,
    поставленный logging.basicConfig() в main(). Поэтому одна строка
    test_logger.info(...) сама попадает и в файл (через handler,
    добавленный ниже), и в терминал (через уже существующий handler
    корневого logger) — без двух отдельных вызовов print()+log().
    """
    os.makedirs("logs", exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join("logs", f"combo_test_{timestamp}.txt")

    test_logger = logging.getLogger("combo_test")
    test_logger.setLevel(logging.INFO)

    # Чистим handlers от ПРОШЛОГО прогона теста перед тем, как повесить
    # новый FileHandler. Без этого повторный F5 в рамках одного запуска
    # main.py копил бы handlers один поверх другого (getLogger с тем же
    # именем возвращает ТОТ ЖЕ объект logger, не новый) — и каждая
    # следующая строка лога писалась бы сразу в файлы ВСЕХ прошлых
    # прогонов теста одновременно, а не только в свежий.
    for old_handler in list(test_logger.handlers):
        test_logger.removeHandler(old_handler)
        old_handler.close()

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
    test_logger.addHandler(file_handler)

    print(f"Лог теста пишется в файл: {log_path}")
    return test_logger


class BotController:
    """
    Обёртка над FarmBot, которая запускает/останавливает его realtime-цикл
    по команде, не пересоздавая сам объект бота.

    Архитектурный принцип: FarmBot() создаётся ОДИН РАЗ при старте
    контроллера (внутри — mss.mss() и поток InputManager, недешёвые
    ресурсы). Тоггл НЕ пересоздаёт бота — только запускает/останавливает
    поток run() в уже существующем объекте. Пересоздание на каждое
    переключение плодило бы незакрытые mss-хендлы и висящие потоки
    InputManager от предыдущих запусков.

    F4-пауза — это НЕ то же самое, что финальное завершение программы:
    _stop() использует input_manager.halt_immediately() (мгновенно, с
    прерыванием текущего действия), а close() при выходе использует
    input_manager.stop() (штатно, дожидается опустошения очереди) — это
    два разных по смыслу способа остановки ввода, см. их докстринги.
    """

    def __init__(self) -> None:
        self._bot = FarmBot()
        self._bot_thread: threading.Thread | None = None
        self._test_thread: threading.Thread | None = None

    @property
    def is_running(self) -> bool:
        return self._bot_thread is not None and self._bot_thread.is_alive()

    @property
    def is_test_running(self) -> bool:
        return self._test_thread is not None and self._test_thread.is_alive()

    def start_combo_test(self) -> None:
        """
        F5 — диагностический прогон ВСЕХ скиллов из skills_config.json по
        порядку, САМ, без остановок, с фиксированной паузой между ними
        (см. _TEST_STEP_DELAY_SEC). НЕ боевая логика — отдельный поток,
        который вызывает тот же InputManager.execute_combo(), что и
        настоящая ротация в bot.py, и логирует каждый отправленный шаг.

        Сознательно БЕЗ input() между шагами (была версия с вопросом в
        терминале после каждого комбо — отказались: чтобы ответить, нужно
        было каждый раз уходить фокусом из игры в консоль, а без фокуса на
        игре часть нажатий там не регистрировалась вообще — то, что
        выглядело как "тест не работает", было потерей фокуса окна, а не
        багом кода). Теперь смотришь в игру и записываешь результат сам,
        не отвлекаясь на терминал.

        Запуск НАМЕРЕННО заблокирован, пока бот работает (is_running): тест
        и боевая ротация используют ОДНУ очередь InputManager — если пустить
        их одновременно, команды теста перемешаются с командами боя, и
        логи перестанут что-либо доказывать (не будет понятно, какое
        нажатие откуда пришло).
        """
        if self.is_running:
            logger.warning(
                "F5 проигнорирован: сначала останови бота (F4) — тест и бой "
                "не должны слать команды в очередь InputManager одновременно."
            )
            print(">>> Сначала останови бота (F4), потом запускай тест (F5) <<<")
            return

        if self.is_test_running:
            logger.warning("F5 проигнорирован: предыдущий тест ещё не завершился.")
            return

        if not self._bot._skills:
            logger.warning(
                "F5: в skills_config.json нет ни одного валидного скилла — "
                "нечего тестировать (проверь combo-строки и лог при старте)."
            )
            print(">>> В skills_config.json нет валидных скиллов <<<")
            return

        self._test_thread = threading.Thread(
            target=self._run_combo_test, daemon=True, name="ComboTest"
        )
        self._test_thread.start()

    def _run_combo_test(self) -> None:
        test_logger = _make_test_logger()
        skills = self._bot._skills
        total = len(skills)

        test_logger.info("=== Старт теста комбо: %d скилл(ов) ===", total)

        # Часть скиллов в T&L таргетированные — без выбранной цели игра
        # молча отклоняет каст: комбо реально уходит на геймпад, но в игре
        # не происходит ничего (именно так выглядел баг с "иконка есть, но
        # не реагирует"). Поэтому ПЕРЕД тестом захватываем цель тем же
        # способом, что и SEARCH в bot.py: жмём target (R3) и проверяем
        # ЗРЕНИЕМ, что цель реально появилась — не жмём вслепую и не
        # надеемся, что она уже была выбрана.
        test_logger.info("Ищу цель перед тестом (R3)...")
        print(">>> Ищу цель (R3) — встань рядом с мобом/манекеном в игре... <<<")
        self._bot.input_manager.press_button("target")
        time.sleep(1.0)  # даём анимации захвата цели и обновлению UI время

        info = self._bot.vision.get_target_info(self._bot._target_roi)
        if not info.found:
            test_logger.warning("Цель не найдена зрением — тест остановлен.")
            print(
                ">>> ЦЕЛЬ НЕ НАЙДЕНА. Встань рядом с мобом/манекеном и нажми "
                "F5 ещё раз — без цели таргетированные скиллы не сработают. <<<"
            )
            return

        test_logger.info("Цель найдена (HP=%.1f%%), продолжаю тест.", info.hp_percent)
        print(
            f">>> Цель найдена (HP={info.hp_percent:.1f}%). ТЕСТ КОМБО: {total} "
            f"скилл(ов), пауза {_TEST_STEP_DELAY_SEC}с между каждым. Смотри в игру "
            f"и записывай сам, что сработало. <<<"
        )

        for i, skill in enumerate(skills, start=1):
            test_logger.info(
                "[%d/%d] слот '%s' -> комбо '%s' (шаги: %s)",
                i, total, skill["slot"], skill["combo"], skill["steps"],
            )
            self._bot.input_manager.execute_combo(skill["steps"])
            time.sleep(_TEST_STEP_DELAY_SEC)

        test_logger.info("=== Тест завершён ===")
        print(">>> ТЕСТ КОМБО ЗАВЕРШЁН — сверяй свои записи с logs/combo_test_*.txt <<<")

    def toggle(self) -> None:
        """Переключает состояние: запущен -> остановить, остановлен -> запустить."""
        if self.is_running:
            self._stop()
        else:
            self._start()

    def _start(self) -> None:
        # reset_abort() ПЕРЕД стартом — снимает флаг экстренной остановки,
        # оставшийся от прошлой паники (если она была). Без этого первая
        # же команда после рестарта увидела бы "устаревший" выставленный
        # флаг в InputManager и оборвалась бы, толком не начавшись.
        self._bot.input_manager.reset_abort()

        # daemon=True — тот же приём, что и с InputWorker в InputManager:
        # поток бота не должен мешать процессу завершиться, если что-то
        # пойдёт не так (это подстраховка, основной путь остановки — close()).
        self._bot_thread = threading.Thread(
            target=self._bot.run, daemon=True, name="FarmBotLoop"
        )
        self._bot_thread.start()
        logger.info("Бот ЗАПУЩЕН.")
        print(">>> БОТ ЗАПУЩЕН — F4 чтобы остановить <<<")

    def _stop(self) -> None:
        # ПОРЯДОК ВАЖЕН: halt_immediately() СНАЧАЛА — обрывает то, что
        # InputManager делает ПРЯМО СЕЙЧАС (например, на середине доворота
        # камеры), и выбрасывает всё, что ещё ждёт в очереди. Именно это
        # даёт мгновенную реакцию на F4 — без этого шага бот доиграл бы
        # всю накопленную очередь движений и нажатий уже ПОСЛЕ паники.
        self._bot.input_manager.halt_immediately()

        # Теперь останавливаем сам цикл FSM, чтобы он не ставил новых команд.
        self._bot.stop()
        # join() с таймаутом — ждём, пока текущий тик run() корректно
        # завершится (self._running проверяется в начале while в bot.py),
        # но не виснем вечно, если внутри цикла что-то застряло.
        self._bot_thread.join(timeout=2.0)
        if self._bot_thread.is_alive():
            logger.warning("Поток бота не остановился за 2 секунды — возможно завис.")
        logger.info("Бот ОСТАНОВЛЕН (ввод прерван немедленно).")
        print(">>> БОТ ОСТАНОВЛЕН — F4 чтобы запустить снова <<<")

    def close(self) -> None:
        """Полная остановка при выходе из программы (Ctrl+C)."""
        if self.is_running:
            self._stop()
        self._bot.input_manager.stop()
        self._bot.vision.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO)

    print("Инициализация FarmBot...")
    controller = BotController()

    # Виртуальный геймпад уже создан ВЫШЕ (внутри BotController() ->
    # FarmBot() -> InputManager.__init__() -> vgamepad.VX360Gamepad()).
    # Пауза здесь, ОДИН раз при старте программы — даёт Windows/игре время
    # фактически распознать новое XInput-устройство, прежде чем ты вообще
    # сможешь нажать F4 или F5 (см. докстринг _GAMEPAD_WARMUP_DELAY_SEC).
    print(f"Жду {_GAMEPAD_WARMUP_DELAY_SEC}с, чтобы игра подхватила виртуальный геймпад...")
    time.sleep(_GAMEPAD_WARMUP_DELAY_SEC)

    print(
        "Готово. F4 — запустить/остановить бота. "
        "F5 — тест-прогон комбо из skills_config.json. "
        "Ctrl+C — выйти из программы."
    )

    # Отслеживаем ФРОНТ нажатия (переход "не зажата" -> "зажата") для КАЖДОЙ
    # клавиши отдельно, а не сам факт "зажата" — иначе, пока игрок физически
    # держит F4 или F5 дольше одного опроса (50 мс), toggle()/start_combo_test()
    # вызывался бы десятки раз подряд без остановки. Два независимых флага
    # (а не один общий) — F4 и F5 могут быть отпущены/нажаты в любой
    # комбинации между собой, их фронты нельзя перепутывать в одну переменную.
    was_toggle_pressed = False
    was_test_pressed = False

    try:
        while True:
            is_toggle_pressed = _is_key_down(HOTKEY_TOGGLE_VK)
            if is_toggle_pressed and not was_toggle_pressed:
                controller.toggle()
            was_toggle_pressed = is_toggle_pressed

            is_test_pressed = _is_key_down(HOTKEY_TEST_VK)
            if is_test_pressed and not was_test_pressed:
                controller.start_combo_test()
            was_test_pressed = is_test_pressed

            time.sleep(_POLL_INTERVAL_SEC)

    except KeyboardInterrupt:
        print("\nЗавершение работы...")
    finally:
        controller.close()
        print("Все ресурсы освобождены. Пока!")


if __name__ == "__main__":
    main()
