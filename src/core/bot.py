"""
src/core/bot.py

Центральный FSM ("мозг" бота). Управляет циклом SEARCH <-> COMBAT,
опираясь на данные VisionManager и команды InputManager.

Этап 3: базовая машина состояний + доворот камеры к цели по данным зрения.

Состояние LOOT сознательно отсутствует: лут в игре подбирается автоматически
движком, отдельная логика сбора не нужна — после смерти цели бот сразу
возвращается в SEARCH.
"""

import os
import sys
import time
import random
import math
import logging
from enum import Enum, auto
from collections.abc import Callable

# Хак для песочницы: тот же приём, что уже используется в vision_manager.py —
# добавляем корень проекта в sys.path, чтобы абсолютный импорт 'src.xxx'
# работал при прямом запуске этого файла (python bot.py), а не только
# при запуске из точки входа проекта.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from src.config import vision_config
from src.core.input_manager import InputManager
from src.core.skills_config import load_skills_config
from src.core.vision.vision_manager import VisionManager, TargetInfo

# Путь к конфигу скиллов ОТНОСИТЕЛЬНО этого файла (src/core/bot.py), а не
# абсолютный и не от текущей рабочей директории — так бот стартует
# одинаково независимо от того, откуда его запустили (из корня проекта,
# из src/core или через IDE с другим cwd).
_SKILLS_CONFIG_PATH = os.path.join(
    os.path.dirname(__file__), "..", "config", "skills_config.json"
)

logger = logging.getLogger(__name__)


class State(Enum):
    """
    Состояния FSM.

    Enum, а не строки/числа напрямую — идиома Python: даёт автодополнение
    в IDE, ловит опечатки на этапе разработки (сравнение State.COMBAT
    вместо магической строки "combat"), и не даёт случайно сравнить
    состояние с произвольным числом.

    auto() вместо ручных значений (1, 2, 3) — числовое значение состояния
    нигде не используется по смыслу (мы никогда не пишем State.SEARCH <
    State.COMBAT), поэтому расставлять числа руками — лишний риск
    опечататься при добавлении ESCAPE в будущем.
    """
    SEARCH = auto()
    COMBAT = auto()


class FarmBot:
    """
    Главный класс бота. Владеет VisionManager и InputManager, хранит текущее
    состояние FSM и крутит realtime-цикл на ~60 FPS.

    Архитектурный принцип (неблокирующий таймер действий):
        Внутри run() НИКОГДА не вызывается time.sleep() дольше одного тика
        (~16.6 мс). Каждое состояние вместо блокирующего ожидания хранит
        метку времени self._next_action_at (time.monotonic()) — следующее
        действие выполняется только когда текущее время её обогнало.

        Благодаря этому бот способен среагировать на события 60 раз в
        секунду, а не "спит" секунду-две вслепую. Это станет критично,
        когда появится ESCAPE: опасность нужно заметить за один кадр,
        а не через секунду после блокирующего ожидания.

    Довoрот камеры (геймпад):
        После перехода с Interception на виртуальный геймпад (vgamepad +
        ViGEmBus) камера крутится уже не дельтой мыши, а отклонением
        правого стика — это не позиция, а СКОРОСТЬ поворота, зависящая от
        силы отклонения и времени удержания. TargetInfo.offset_x/offset_y
        (смещение HP-бара от центра ROI в пикселях кадра) передаются в
        input_manager.aim_with_stick(dx, dy) как сигнал "насколько и в
        какую сторону довернуть" — перевод пикселей в силу и длительность
        удержания стика происходит уже внутри InputManager, FSM про это
        ничего не знает и знать не должна.
    """

    _TARGET_FPS = 60
    _FRAME_TIME = 1.0 / _TARGET_FPS

    # Кулдауны действий — диапазоны (min, max), а не фиксированное число.
    # Даже на этом простом этапе нажатие ровно раз в секунду миллисекунда
    # в миллисекунду — само по себе статистический паттерн, который EAC
    # умеет ловить анализом ритма (та же логика, что уже в
    # InputManager._AFTER_CMD_MS для пауз между командами).
    _SEARCH_TAB_COOLDOWN = (0.9, 1.1)
    _COMBAT_ATTACK_COOLDOWN = (0.9, 1.1)

    # Пауза перед первым Tab после смерти цели (переход COMBAT -> SEARCH).
    # НЕ делаем этот переход мгновенным (_next_action_at = 0.0), в отличие
    # от других переходов: реакция "труп -> Tab" за один кадр (16 мс) —
    # статистически неестественный ритм, который EAC-анализ засекает
    # быстрее, чем сами нажатия. Живой игрок хотя бы долю секунды смотрит
    # на экран, прежде чем искать следующую цель.
    _POST_KILL_DELAY = (0.2, 0.6)

    # Доворот камеры: мёртвая зона (px) и кулдаун коррекции — утверждено
    # архитектором в диапазонах 15-20px / 300-500мс.
    _AIM_DEADZONE_PX = 18
    _AIM_CORRECTION_COOLDOWN = (0.3, 0.5)

    # Пауза ПОСЛЕ успешного каста скилла — грубая оценка длительности
    # анимации, чтобы FSM не пыталась тут же спамить следующее действие
    # поверх неё. Это НЕ кулдаун самого скилла (тот читается зрением
    # через self._skills[i]['roi'], см. __init__) — это отдельная,
    # более короткая пауза "не мешай текущему касту".
    _SKILL_CAST_DELAY = (0.4, 0.8)

    def __init__(self) -> None:
        self.vision = VisionManager()
        self.input_manager = InputManager()

        # ROI считается ОДИН раз в __init__, а не на каждом тике.
        # build_roi() просто складывает офсеты из конфига с центром экрана —
        # пересчитывать этот словарь 60 раз в секунду означало бы лишнюю
        # нагрузку на сборщик мусора ради значения, которое не меняется,
        # пока не сменилось разрешение экрана.
        self._target_roi = self.vision.build_roi(
            vision_config.TARGET_HP_OFFSET_X,
            vision_config.TARGET_HP_OFFSET_Y,
            vision_config.TARGET_HP_WIDTH,
            vision_config.TARGET_HP_HEIGHT,
        )

        self.state: State = State.SEARCH

        # Таймер следующего действия ТЕКУЩЕГО состояния. 0.0 — действие
        # выполнится уже на первом тике, ждать разогрева не нужно.
        self._next_action_at: float = 0.0

        # ОТДЕЛЬНЫЙ таймер для коррекции прицела — независимый от
        # _next_action_at (тот отвечает за tab/атаку). Довoрот камеры и
        # нажатие клавиш логически разные события с разной частотой.
        self._next_aim_correction_at: float = 0.0

        # Список скиллов в порядке ПРИОРИТЕТА: бот в COMBAT идёт по списку
        # сверху вниз и кастует первый скилл, чей кулдаун истёк (см.
        # _act_combat_rotation). Если ни один не готов — обычная атака.
        #
        # Источник данных — skills_config.json (src/config/), а не
        # захардкоженный список здесь: сам пользователь один раз выписывает
        # туда все свои combo-строки (например "RB+A") и кулдауны в секундах,
        # глядя на экран настроек управления в игре — код тут ничего не
        # знает про конкретную раскладку конкретного игрока.
        #
        # Сознательно вернулись к самоучёту кулдауна (cooldown + last_used
        # внутри каждого элемента списка), а не к вижну по иконке скилла,
        # который был здесь раньше: обсуждали отдельно — вижн точнее (не
        # разъезжается с реальным сокращением отката от пассивок/баффов),
        # но самоучёт сильно проще для пользователя, который просто пишет
        # число секунд в JSON, и мы сознательно выбрали простоту здесь.
        # Плата за это — если у скилла реально плавающий кулдаун (сократился
        # баффом), бот об этом не узнает и будет ждать полный интервал.
        self._skills: list[dict] = load_skills_config(_SKILLS_CONFIG_PATH)

        # Диспетчер состояний: State -> метод-обработчик.
        # Словарь, а не цепочка if/elif — одна точка расширения (новая
        # строка), когда добавится ESCAPE, вместо правки условий. Поиск O(1)
        # вместо линейного перебора — на 60 FPS эта разница накопится.
        self._handlers: dict[State, Callable[[], None]] = {
            State.SEARCH: self._handle_search,
            State.COMBAT: self._handle_combat,
        }

        self._running = False

        logger.info("FarmBot инициализирован. Стартовое состояние: %s", self.state)

    # ------------------------------------------------------------------
    # Главный цикл
    # ------------------------------------------------------------------

    def run(self) -> None:
        """
        Бесконечный цикл на ~60 FPS. На каждом тике:
          1. Диспетчер вызывает обработчик текущего состояния.
          2. Обработчик сам проверяет свой таймер и сам решает, менять ли
             self.state.
          3. Цикл спит ровно столько, сколько нужно, чтобы удержать 60 FPS.
        """
        self._running = True
        logger.info("FarmBot: цикл запущен.")

        while self._running:
            frame_start = time.perf_counter()

            handler = self._handlers[self.state]
            handler()

            # Вычисляем ОСТАТОК тика через perf_counter(), а не наивный
            # time.sleep(self._FRAME_TIME). Наивный вариант копит дрейф:
            # если обработка кадра заняла 5 мс, а мы всё равно спим полные
            # 16.6 мс, реальный FPS со временем плавно проседает ниже 60.
            # Здесь же мы спим ровно "то, что осталось" от бюджета тика.
            elapsed = time.perf_counter() - frame_start
            sleep_time = self._FRAME_TIME - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)
            else:
                # Кадр обработался дольше бюджета 16.6 мс — не фатально,
                # но сигнал, что где-то в state-логике узкое место
                # (например, зрение подвисло на тяжёлом кадре).
                logger.debug(
                    "FarmBot: тик превысил бюджет 60 FPS на %.1f мс",
                    -sleep_time * 1000,
                )

    def stop(self) -> None:
        """Останавливает цикл run() после текущего тика."""
        self._running = False

    # ------------------------------------------------------------------
    # Обработчики состояний
    # ------------------------------------------------------------------

    def _handle_search(self) -> None:
        """
        SEARCH: раз в ~секунду жмём кнопку захвата цели (R3 по дефолту
        T&L, см. InputManager.BUTTON_MAP['target']), ищем цель.
        Если зрение видит таргет — сразу довoрачиваем камеру на него
        (первый снап, а не робот-мгновенность — деталь в _maybe_correct_aim)
        и переключаемся в COMBAT.
        """
        # Один захват экрана даёт разом found/hp/offset — см. докстринг
        # класса VisionManager про то, почему это ОДИН метод, а не три.
        info = self.vision.get_target_info(self._target_roi)

        if info.found:
            logger.info("SEARCH -> COMBAT: цель обнаружена, довoрачиваем камеру.")
            self._next_aim_correction_at = 0.0  # форсируем немедленный первый доворот
            self._maybe_correct_aim(info, time.monotonic())
            self.state = State.COMBAT
            self._next_action_at = 0.0
            return

        now = time.monotonic()
        if now >= self._next_action_at:
            self.input_manager.press_button("target")
            self._next_action_at = now + random.uniform(*self._SEARCH_TAB_COOLDOWN)

    def _handle_combat(self) -> None:
        """
        COMBAT: когда таймер действия готов — идём по ротации скиллов
        (см. _act_combat_rotation), иначе просто доворачиваем прицел.
        Если таргет пропал (убит) — сразу возвращаемся в SEARCH.

        LOOT намеренно нет: в игре подбор дропа автоматический, отдельного
        нажатия/состояния для этого не требуется.
        """
        info = self.vision.get_target_info(self._target_roi)

        if not info.found:
            logger.info("COMBAT -> SEARCH: цель пропала (убита), лут автоматический.")
            self.state = State.SEARCH
            now = time.monotonic()
            # Небольшая случайная пауза перед первым Tab — см. комментарий
            # к _POST_KILL_DELAY: мгновенная реакция здесь неестественна.
            self._next_action_at = now + random.uniform(*self._POST_KILL_DELAY)
            return

        now = time.monotonic()
        if now >= self._next_action_at:
            self._act_combat_rotation(now)

        self._maybe_correct_aim(info, now)

    def _act_combat_rotation(self, now: float) -> None:
        """
        Одна попытка действия в COMBAT, когда _next_action_at уже
        разрешает действовать. Идём по self._skills В ПОРЯДКЕ СПИСКА
        (это и есть приоритет: сильные/важные скиллы — выше) и кастуем
        ПЕРВЫЙ, чей ROI зрение считает готовым. Если ни один не готов —
        обычная автоатака (RB), как и раньше.

        Проверка зрением скиллов идёт ТОЛЬКО здесь, а не в каждом тике
        run() — этот метод и так вызывается не чаще, чем позволяет
        _next_action_at (~раз в секунду или сразу после каста, см.
        _SKILL_CAST_DELAY), поэтому N дополнительных mss.grab() на
        иконки скиллов не нагружают 60 FPS цикл: они происходят редко,
        а не на каждом кадре.
        """
        for skill in self._skills:
            # Вместо get_skill_ready(roi) — просто проверяем сохранённый таймер
            if now - skill["last_used"] >= skill["cooldown"]:
                logger.info("COMBAT: кастую скилл '%s' (слот %s).", skill["combo"], skill["slot"])
                self.input_manager.execute_combo(skill["steps"])
                skill["last_used"] = now
                
                # Пауза "не спамь поверх анимации"
                self._next_action_at = now + random.uniform(*self._SKILL_CAST_DELAY)
                return

        # Ни один скилл из ротации не готов — обычная автоатака.
        self.input_manager.press_button("attack")
        self._next_action_at = now + random.uniform(*self._COMBAT_ATTACK_COOLDOWN)

    def _maybe_correct_aim(self, info: TargetInfo, now: float) -> None:
        """
        Общая логика доворота камеры — используется и при первом обнаружении
        цели в SEARCH, и периодически в COMBAT. Один метод вместо копипасты
        в оба обработчика: правило "когда и на сколько довернуть" одно и то
        же, различается только момент вызова.

        Собственный кулдаун (_next_aim_correction_at) не даёт слать команду
        коррекции чаще, чем раз в _AIM_CORRECTION_COOLDOWN: aim_with_stick
        занимает воркер InputManager на сотни мс (разгон + удержание +
        торможение стика), и если гнать новую команду каждый тик, очередь
        будет расти быстрее, чем успевает опустошаться — коррекции
        накопятся с лагом и будут применяться к уже устаревшему положению
        цели.
        """
        if now < self._next_aim_correction_at:
            return

        self._next_aim_correction_at = now + random.uniform(*self._AIM_CORRECTION_COOLDOWN)

        # Мёртвая зона: бар и так возле центра ROI (шум детекции в пару
        # пикселей неизбежен, даже если цель не двигалась) — довoрачивать
        # камеру ради 2px означало бы дёргать мышь без реальной необходимости.
        if abs(info.offset_x) <= self._AIM_DEADZONE_PX and abs(info.offset_y) <= self._AIM_DEADZONE_PX:
            return

        # Передаём offset НАПРЯМУЮ, без round() — в отличие от старой
        # мышиной версии, где offset шёл в целочисленную дельту курсора,
        # здесь offset лишь пересчитывается в float-силу стика (см.
        # InputManager._offset_to_stick_force), дробная точность не лишняя.
        self.input_manager.aim_with_stick(info.offset_x, info.offset_y)


# ----------------------------------------------------------------------
# ПЕСОЧНИЦА ДЛЯ ЛОКАЛЬНОГО ТЕСТИРОВАНИЯ FSM (без игры и без экрана)
# Запусти: python bot.py
#
# Идея: подменяем VisionManager и InputManager на мок-объекты с тем же
# интерфейсом (get_target_info, press_button, aim_with_stick), но без
# реального захвата экрана (mss) и без реального виртуального геймпада
# (vgamepad/ViGEmBus). Так можно проверить, что переходы
# SEARCH -> COMBAT -> SEARCH происходят в правильном порядке, а коррекция
# прицела срабатывает при "дрейфе" цели от центра ROI — за секунды, не
# заходя в игру.
# ----------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    class _MockVision:
        """Мок VisionManager: get_target_info управляется вручную по сценарию."""

        def __init__(self) -> None:
            self._tick = 0

        def build_roi(self, *_args, **_kwargs) -> dict:
            return {"left": 0, "top": 0, "width": 300, "height": 300}

        def get_target_info(self, _roi: dict) -> TargetInfo:
            self._tick += 1
            # Сценарий: первые ~2 сек цели нет (SEARCH ищет), затем ~3 сек
            # цель есть и "гуляет" синусоидой по X (проверяем, что COMBAT
            # периодически шлёт коррекцию прицела), потом цель пропадает.
            found = 120 < self._tick < 300
            offset_x = 40.0 * math.sin(self._tick / 15) if found else 0.0
            return TargetInfo(
                found=found,
                hp_percent=75.0 if found else 0.0,
                offset_x=offset_x,
                offset_y=0.0,
            )

    class _MockInput:
        """Мок InputManager: вместо реального ввода просто логирует."""

        def press_button(self, action: str) -> None:
            logger.info("[MOCK INPUT] press_button('%s')", action)

        def aim_with_stick(self, dx: float, dy: float) -> None:
            logger.info("[MOCK INPUT] aim_with_stick(dx=%.1f, dy=%.1f)", dx, dy)

        def stop(self) -> None:
            pass

    # FarmBot.__new__ вместо FarmBot() — сознательно обходим __init__,
    # чтобы не создавать НАСТОЯЩИЙ VisionManager (mss.mss()) и НАСТОЯЩИЙ
    # InputManager (реальный драйвер Interception). В песочнице нам нужна
    # только логика переходов состояний, а не боевые зависимости.
    bot = FarmBot.__new__(FarmBot)
    bot.vision = _MockVision()
    bot.input_manager = _MockInput()
    bot._target_roi = bot.vision.build_roi()
    bot.state = State.SEARCH
    bot._next_action_at = 0.0
    bot._next_aim_correction_at = 0.0
    # Пустой список — в песочнице ротация всегда падает на автоатаку,
    # этого достаточно, чтобы проверить переходы состояний. Реальные
    # скиллы с ROI тестируются только в игре, не в этой песочнице.
    bot._skills = []
    bot._handlers = {
        State.SEARCH: bot._handle_search,
        State.COMBAT: bot._handle_combat,
    }
    bot._running = True

    print("Тест FSM на моках (без экрана и без игры)...")
    start = time.time()
    while time.time() - start < 8:
        bot._handlers[bot.state]()
        time.sleep(1 / 60)
    print("Тест завершён. Смотри лог выше — должна быть видна цепочка "
          "SEARCH -> COMBAT (с периодическими aim_with_stick) -> SEARCH.")
