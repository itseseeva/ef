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
from src.core.input_manager import InputManager, gamepad_debug_logger
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

    Два источника зрения, две разные роли (2026-10-02):
        found/hp_percent (жива ли цель, сколько у неё HP — то, что решает
        переходы SEARCH<->COMBAT) теперь читаются ИЗ ПАНЕЛИ ЦЕЛИ
        (self._panel_roi, VisionManager.get_panel_target_info) — это
        фиксированный маленький элемент интерфейса, который появляется
        ТОЛЬКО когда реально есть выбранная цель, и физически не может
        зацепить декорацию мира (в отличие от широкой мировой зоны).

        offset_x/offset_y для доворота камеры по-прежнему читаются из
        летающего бара НАД ГОЛОВОЙ моба (self._target_roi,
        VisionManager.get_target_info) — панель не привязана к положению
        моба на экране, этот сигнал только она дать не может.

        Это значит: на каждом тике ДВА отдельных TargetInfo — panel_info
        (решает found/hp, см. _handle_search/_handle_combat) и aim_info
        (решает offset, см. _maybe_correct_aim). Они МОГУТ временно
        расходиться (например, aim_info.found=False на миг, пока моб
        скрыт деревом, а panel_info.found всё ещё True) — это ожидаемо и
        не баг: _maybe_correct_aim() просто пропускает коррекцию в такие
        тики, а panel_info остаётся единственным источником правды про
        "жив ли таргет".

    Довoрот камеры (геймпад):
        После перехода с Interception на виртуальный геймпад (vgamepad +
        ViGEmBus) камера крутится уже не дельтой мыши, а отклонением
        правого стика — это не позиция, а СКОРОСТЬ поворота, зависящая от
        силы отклонения и времени удержания. TargetInfo.offset_x/offset_y
        (смещение HP-бара от центра ROI в пикселях кадра, из aim_info выше)
        передаются в input_manager.aim_with_stick(dx, dy) как сигнал
        "насколько и в какую сторону довернуть" — перевод пикселей в силу
        и длительность удержания стика происходит уже внутри InputManager,
        FSM про это ничего не знает и знать не должна.

        С этого этапа aim_with_stick() — не команда "выполни довод", а
        мгновенное обновление позиции цели для НЕПРЕРЫВНОГО фонового
        потока камеры (InputManager._aim_worker, свой поток, отдельный от
        очереди боевых комбо). FSM здесь просто репортит зрение каждый
        тик — когда двигать стик, когда держать паузу, решает уже сам
        _aim_worker. Подробности и обоснование архитектуры — в докстринге
        класса InputManager. Единственная обязанность FSM-стороны —
        вызвать input_manager.clear_aim_target(), когда цель пропала (см.
        _handle_combat), чтобы фоновый поток не пытался доворачивать
        камеру к уже неактуальной последней позиции.
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

    # Сколько ПОДРЯД времени бар цели должен отсутствовать в ROI, прежде
    # чем мы поверим, что это настоящая смерть, а не момент, когда моб
    # увернулся/отбежал и его плывущий HP-бар на миг вышел за границы
    # (маленькой, статичной) зоны поиска. Фиксированное число, не диапазон
    # (min, max), как у остальных задержек — это внутренний порог принятия
    # решения, а не тайминг нажатия кнопки, наружу через ввод он никак не
    # проявляется, рандомизировать нечего.
    _TARGET_LOST_GRACE_S = 0.35

    # Доворот камеры: мёртвая зона и вся логика "двигать/ждать/трясти"
    # переехали в InputManager (_AIM_DEADZONE_PX и параметры непрерывной
    # модели прицеливания — см. докстринг InputManager._aim_worker) — с
    # переходом на непрерывный фоновый _aim_worker именно InputManager, а
    # не FSM, решает "двигать или ждать" на основе самой свежей цели.

    # Пауза ПОСЛЕ успешного каста скилла — грубая оценка длительности
    # анимации, чтобы FSM не пыталась тут же спамить следующее действие
    # поверх неё. Это НЕ кулдаун самого скилла (тот считается отдельно,
    # числом секунд в self._skills[i]['cooldown']/['last_used'], см.
    # __init__ и skills_config.json) — это отдельная, более короткая
    # пауза "не мешай текущему касту".
    _SKILL_CAST_DELAY = (0.4, 0.8)

    # Как часто пишем в gamepad_debug.log текущее состояние зрения
    # (found/hp/offset) — единственный способ УВИДЕТЬ вживую во время
    # реального прогона (F5), что VisionManager реально что-то находит и
    # какие числа считает, не поднимая отдельное cv2.imshow-окно (то окно
    # живёт только в песочнице vision_manager.py — поднимать его из
    # фонового потока бота конфликтовало бы с окном pywebview, оба тянут
    # на себя оконный цикл ОС). Раз в секунду, а не каждый тик — иначе
    # 60 строк в секунду утопили бы полезные AIM/COMBO записи в том же файле.
    _VISION_LOG_INTERVAL_S = 1.0

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

        # Панель цели (2026-10-02) — ВТОРАЯ, отдельная зона захвата, см.
        # докстринг класса "Два источника зрения, две разные роли". Тоже
        # считается один раз здесь, а не на каждом тике — те же причины,
        # что и у self._target_roi выше.
        self._panel_roi = self.vision.build_roi(
            vision_config.TARGET_PANEL_OFFSET_X,
            vision_config.TARGET_PANEL_OFFSET_Y,
            vision_config.TARGET_PANEL_WIDTH,
            vision_config.TARGET_PANEL_HEIGHT,
        )

        self.state: State = State.SEARCH

        # Таймер следующего действия ТЕКУЩЕГО состояния. 0.0 — действие
        # выполнится уже на первом тике, ждать разогрева не нужно.
        self._next_action_at: float = 0.0

        # Таймер троттлинга VISION-строк в gamepad_debug.log — см.
        # _VISION_LOG_INTERVAL_S и _log_vision_status().
        self._next_vision_log_at: float = 0.0

        # Момент (time.monotonic()), когда бар цели ПЕРВЫЙ раз пропал из
        # ROI подряд, None — если сейчас бар виден или отсчёт ещё не
        # начинался. См. _TARGET_LOST_GRACE_S и _handle_combat().
        self._target_lost_since: float | None = None

        # Флаг "автоатака уже запущена персонажем и продолжает идти сама".
        # В игре одно нажатие RB запускает цикл автоатаки, который крутится
        # без повторных нажатий — жать RB на каждой проверке готовности (как
        # было раньше, ~раз в секунду) не нужно и физически неверно
        # воспроизводит поведение игры. Сбрасываем в False при КАЖДОМ касте
        # цепочки скиллов (см. _act_combat_rotation) — предполагаем, что
        # каст скилла прерывает текущую автоатаку, поэтому её придётся
        # запускать заново — и при входе в COMBAT из SEARCH (см.
        # _handle_search): новая цель — прошлая автоатака (если была) уже не
        # актуальна.
        self._autoattack_engaged: bool = False

        # Список ЦЕПОЧЕК в порядке ПРИОРИТЕТА: бот в COMBAT идёт по списку
        # сверху вниз и кастует первую цепочку, чей кулдаун истёк (см.
        # _act_combat_rotation). Цепочка — это несколько скиллов, кастуемых
        # подряд одним залпом; кулдаун висит на ЦЕЛОЙ цепочке (диапазоном
        # min/max), а не на отдельном скилле внутри неё — сознательная смена
        # модели по сравнению с более ранней версией, где кулдаун считался
        # per-skill. Если ни одна цепочка не готова — обычная атака.
        #
        # Источник данных — skills_config.json (src/config/), экспортируемый
        # React-интерфейсом, а не захардкоженный список здесь: пользователь
        # собирает цепочки мышкой, глядя на реальный экран умений в игре —
        # код тут ничего не знает про конкретную раскладку конкретного игрока.
        #
        # Самоучёт кулдауна (cooldown_min/max + next_ready_at внутри каждой
        # цепочки), а не вижн по иконке скилла: тот же выбор простоты для
        # пользователя, что обсуждали раньше — плата та же самая, если у
        # цепочки реально плавающий кулдаун (сократился баффом), бот об этом
        # не узнает и будет ждать полный интервал.
        self._chains: list[dict] = load_skills_config(_SKILLS_CONFIG_PATH)

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

    # Аналог Tab с компа на геймпаде T&L — НЕ R3 (тот "Set Lock-On/Cancel":
    # хватает то, на что СЕЙЧАС смотрит камера, требует точного прицела).
    # Настоящий переключатель цели — "Change Target": RB (удержание) + Y
    # (источник — тот же гайд Game8, что и весь остальной BUTTON_MAP).
    # Формат шагов — тот же, что возвращает InputManager.parse_combo_string
    # для строки "RB+Y": удержали RB как модификатор, нажали Y, отпустили RB.
    _SEARCH_TARGET_COMBO = ["hold_RB", "press_Y", "release_RB"]

    def _handle_search(self) -> None:
        """
        SEARCH: раз в ~секунду жмём "Change Target" (RB+Y — см.
        _SEARCH_TARGET_COMBO), ищем цель. "Нашли ли цель" решает ПАНЕЛЬ
        (panel_info.found, см. докстринг класса) — это и есть переход в
        COMBAT. Мировой бар (aim_info) используется ТОЛЬКО если он тоже
        виден в этот самый тик — довoрачиваем камеру сразу при обнаружении,
        но его отсутствие в конкретном кадре не мешает переходу в COMBAT.
        """
        panel_info = self.vision.get_panel_target_info(self._panel_roi)
        aim_info = self.vision.get_target_info(self._target_roi)
        now = time.monotonic()
        self._log_vision_status(panel_info, aim_info, now)

        if panel_info.found:
            logger.info("SEARCH -> COMBAT: цель обнаружена (панель).")
            # Дублируем в gamepad_debug.log (2026-10-02): logger.info выше
            # идёт только в консоль — её не видно постфактум без доступа к
            # живому терминалу. Переходы состояний нужны в ОДНОМ файле
            # вместе с VISION/COMBO/ROTATION, иначе при разборе лога после
            # игровой сессии невозможно понять, какое именно состояние
            # породило тот или иной нажатый комбо (ровно это и мешало
            # диагностике бага "обрывается цепочка/застревает SEARCH").
            gamepad_debug_logger.debug("STATE   SEARCH -> COMBAT (панель нашла цель)")
            if aim_info.found:
                self._maybe_correct_aim(aim_info)
            self.state = State.COMBAT
            self._next_action_at = 0.0
            # Новая цель — если раньше уже была запущена автоатака (по
            # старой цели), она больше не актуальна. Форсируем свежее
            # нажатие RB в _act_combat_rotation() при первой же проверке.
            self._autoattack_engaged = False
            return

        if now >= self._next_action_at:
            self.input_manager.execute_combo(self._SEARCH_TARGET_COMBO)
            self._next_action_at = now + random.uniform(*self._SEARCH_TAB_COOLDOWN)

    def _handle_combat(self) -> None:
        """
        COMBAT: когда таймер действия готов — идём по ротации скиллов
        (см. _act_combat_rotation), иначе просто доворачиваем прицел.
        Если таргет пропал — уходим в SEARCH, но не по ОДНОМУ пропущенному
        кадру (см. self._target_lost_since и _TARGET_LOST_GRACE_S ниже),
        а только если пропажа держится дольше грейс-периода.

        "Жива ли цель" теперь решает ПАНЕЛЬ (panel_info.found, см.
        докстринг класса про два источника зрения) — мировой бар
        (aim_info) используется только для доворота камеры, когда он сам
        виден в конкретном кадре; его временное исчезновение (моб на миг
        скрылся за деревом) больше НЕ считается сигналом смерти.

        LOOT намеренно нет: в игре подбор дропа автоматический, отдельного
        нажатия/состояния для этого не требуется.
        """
        panel_info = self.vision.get_panel_target_info(self._panel_roi)
        aim_info = self.vision.get_target_info(self._target_roi)
        now = time.monotonic()
        self._log_vision_status(panel_info, aim_info, now)

        if not panel_info.found:
            # ПЕРВЫЙ кадр без панели — ещё не факт смерти. Панель куда
            # стабильнее мирового бара (фиксированная зона, не привязана
            # к положению моба), но один сбойный кадр захвата (mss)/кадр
            # посреди анимации смены цели в самой игре всё ещё возможен —
            # грейс-период остаётся дешёвой страховкой на этот случай, а
            # не основной защитой, как было раньше с мировым баром.
            # Настоящая смерть даёт found=False СТАБИЛЬНО много кадров
            # подряд (панель пропадает совсем) — грейс-период отличает эти
            # два случая по длительности пропажи, а не по одному кадру.
            if self._target_lost_since is None:
                self._target_lost_since = now
            if now - self._target_lost_since < self._TARGET_LOST_GRACE_S:
                # Ждём, не трогая ни ротацию, ни прицел — по свежему
                # мгновенному "офсету" несуществующего бара (0,0) камера
                # дёрнулась бы к центру ROI на пустом месте.
                return

            missing_for_s = now - self._target_lost_since
            logger.info("COMBAT -> SEARCH: цель пропала (убита), лут автоматический.")
            # Та же причина, что и у STATE-строки в _handle_search() выше —
            # но здесь ЕЩЁ важнее: именно это место чистит очередь ротации
            # (cancel_pending_actions() ниже), и единственный способ отличить
            # в логе "обвал цепочки из-за настоящей пропажи панели" от
            # "это SEARCH сам по себе спамит тот же физический комбо" —
            # увидеть ИМЕННО эту строку с реальной длительностью пропажи.
            gamepad_debug_logger.debug(
                "STATE   COMBAT -> SEARCH (панель отсутствовала %.2fс подряд, "
                "грейс-период %.2fс) -> cancel_pending_actions()",
                missing_for_s, self._TARGET_LOST_GRACE_S,
            )
            self.state = State.SEARCH
            # Выбрасываем ещё не начатые шаги ротации, оставшиеся в
            # очереди с момента ДО смерти цели (например, цепочка из
            # 4 скиллов, а моб умер после второго) — без этого бот
            # доигрывает их уже в пустоту, выглядит как "бьёт воздух/труп".
            # НЕ halt_immediately() — та ещё и резко обнуляет геймпад,
            # это для панической F4-остановки, не для обычного килла.
            self.input_manager.cancel_pending_actions()
            # Гасим цель фонового _aim_worker — без этого он продолжил бы
            # пытаться довoрачивать камеру к последней позиции мёртвой
            # цели вплоть до следующего успешного aim_with_stick().
            self.input_manager.clear_aim_target()
            # И "память" зрения о позиции бара — см. VisionManager.
            # reset_tracking(): без сброса поиск новой цели сравнивал бы
            # кандидатов с координатами уже мёртвой цели, вместо того
            # чтобы честно взять самого широкого заново.
            self.vision.reset_tracking()
            self._target_lost_since = None
            # Небольшая случайная пауза перед первым Tab — см. комментарий
            # к _POST_KILL_DELAY: мгновенная реакция здесь неестественна.
            self._next_action_at = now + random.uniform(*self._POST_KILL_DELAY)
            return

        # Панель снова видна — сбрасываем счётчик грейс-периода, даже
        # если он был запущен секунду назад: цель жива, отсчёт больше не
        # нужен.
        self._target_lost_since = None

        if now >= self._next_action_at:
            self._act_combat_rotation(now)

        if aim_info.found:
            self._maybe_correct_aim(aim_info)

    def _act_combat_rotation(self, now: float) -> None:
        """
        Одна попытка действия в COMBAT, когда _next_action_at уже
        разрешает действовать. Идём по self._chains В ПОРЯДКЕ СПИСКА (это
        и есть приоритет: важные цепочки — выше, см. поле "order" в
        skills_config.json) и кастуем ПЕРВУЮ, чей собственный дедлайн
        next_ready_at уже прошёл. Если ни одна не готова — обычная
        автоатака (RB), как и раньше.

        Кастуем ВСЮ цепочку одним махом — вызываем execute_combo() для
        каждого скилла цепочки подряд, без пауз между вызовами со стороны
        FSM. Это НЕ блокирует 60 FPS цикл: execute_combo() внутри
        InputManager только кладёт задачу в очередь воркера
        (queue.Queue.put — микросекунды) и сразу возвращается, а сам ввод с
        рандомизированными паузами между командами исполняется отдельным
        потоком-воркером (см. InputManager._worker) — FSM-поток тут ничем
        не рискует, даже если в цепочке 5+ скиллов подряд.

        now передаётся параметром, а не считается заново через
        time.monotonic() внутри метода — тот же принцип, что и с
        _next_action_at: один "снимок времени" на весь тик, чтобы кулдауны
        разных цепочек сравнивались с ОДНИМ и тем же моментом.

        Важная деталь (баг, найденный и подтверждённый логами
        InputManager): execute_combo() кладёт команды в очередь и сразу
        возвращается — САМИ нажатия могут физически доигрывать ещё
        несколько секунд после этого. _SKILL_CAST_DELAY ниже (0.4-0.8с) —
        это короткая пауза "не спамь ПОВЕРХ анимации", а не оценка полного
        времени цепочки. Без проверки is_idle() FSM успевала бы решить
        кастовать ДРУГУЮ цепочку (или снова эту же после её собственного
        кулдауна) ДО того, как предыдущая на самом деле доиграла —
        команды обеих цепочек сваливались в одну очередь и перемешивались
        в реальных нажатиях. is_idle() — дешёвая неблокирующая проверка,
        не завершённые задачи очереди InputManager, безопасна внутри
        60 FPS цикла.
        """
        if not self.input_manager.is_idle():
            return

        for chain in self._chains:
            if now >= chain["next_ready_at"]:
                logger.info(
                    "COMBAT: кастую цепочку '%s' (%d скилл(ов): %s).",
                    chain["name"], len(chain["gamepad_steps"]), chain["sequence"],
                )
                # cast_times — параллельный gamepad_steps список (та же
                # длина и порядок, см. skills_config.py) с реальным временем
                # каста каждого шага или None, если у шага нет кастомного
                # времени (тогда execute_combo сам возьмёт обычную короткую
                # межскилльную паузу). zip(), а не индекс по range(len()) —
                # идиома Python для "иду по двум спискам синхронно
                # попарно", читается как сама мысль "шаг + его время каста",
                # без риска рассинхронизировать индексы вручную.
                for steps, cast_time_s in zip(
                    chain["gamepad_steps"], chain["cast_times"]
                ):
                    self.input_manager.execute_combo(steps, cast_time_s=cast_time_s)

                # Кулдаун — диапазон, разыгрываем КОНКРЕТНОЕ значение один
                # раз здесь и запоминаем как абсолютный дедлайн (тот же
                # приём, что и у _next_action_at ниже) — а не перегенерируем
                # случайное число на каждой проверке готовности, что дало бы
                # "мерцающий" кулдаун вместо стабильного значения на цикл.
                cooldown_s = random.uniform(
                    chain["cooldown_min"], chain["cooldown_max"]
                )
                chain["next_ready_at"] = now + cooldown_s
                # Каст скилла прерывает текущую автоатаку персонажа — после
                # цепочки автоатаку придётся запускать заново одним свежим
                # нажатием RB, а не полагаться на то, что она сама
                # продолжится после чужого действия поверх неё.
                self._autoattack_engaged = False
                # ВАЖНО (источник путаницы при чтении лога вручную):
                # каждая цепочка считает СВОЙ кулдаун от СВОЕГО собственного
                # предыдущего каста — независимо от других цепочек. Если
                # цепочка 2 откастовалась между двумя кастами цепочки 1,
                # это НЕ сдвигает и НЕ сбрасывает таймер цепочки 1. Поэтому
                # "цепочка 1 началась почти сразу после конца цепочки 2"
                # может быть абсолютно верным поведением — печатаем здесь
                # явный дедлайн, чтобы это было видно по секундам в логе, а
                # не приходилось прикидывать на глаз по временным меткам
                # COMBO START/END.
                gamepad_debug_logger.debug(
                    "ROTATION: цепочка '%s' кастуется, следующий повтор "
                    "через %.1fс (диапазон %.1f-%.1fс, независимо от других "
                    "цепочек).",
                    chain["name"], cooldown_s,
                    chain["cooldown_min"], chain["cooldown_max"],
                )
                # Пауза "не спамь поверх анимации" — НЕ кулдаун цепочки (тот
                # отдельно лежит в next_ready_at и продолжает тикать
                # независимо от этой паузы).
                self._next_action_at = now + random.uniform(*self._SKILL_CAST_DELAY)
                return

        # Ни одна цепочка из ротации не готова. РАНЬШЕ здесь на каждой такой
        # проверке (~раз в секунду, по _COMBAT_ATTACK_COOLDOWN) заново жалось
        # RB — а в игре одно нажатие уже запускает цикл автоатаки, который
        # крутится сам без повторных нажатий. Спам RB тут был лишним и не
        # соответствовал реальному поведению персонажа (и это лишние
        # нажатия конкретно на ту же кнопку, что уже отслеживает EAC).
        # Поэтому жмём RB ТОЛЬКО если автоатака ещё не запущена
        # (_autoattack_engaged == False) — а не готовую цепочку всё равно
        # продолжаем проверять на каждом тике, просто без лишнего нажатия.
        if not self._autoattack_engaged:
            gamepad_debug_logger.debug(
                "ROTATION: ни одна цепочка не готова -> запускаю автоатаку "
                "(RB). Остаток кулдауна: %s",
                ", ".join(
                    f"'{c['name']}'={max(0.0, c['next_ready_at'] - now):.1f}с"
                    for c in self._chains
                ),
            )
            self.input_manager.press_button("attack")
            self._autoattack_engaged = True

        self._next_action_at = now + random.uniform(*self._COMBAT_ATTACK_COOLDOWN)

    def _maybe_correct_aim(self, aim_info: TargetInfo) -> None:
        """
        Репортит СВЕЖУЮ позицию цели в InputManager — вызывается КАЖДЫЙ
        тик, когда МИРОВОЙ бар виден (aim_info.found — см. докстринг
        класса про два источника зрения; это НЕ то же самое, что "жива ли
        цель", за то отвечает панель). Раньше этот метод сам решал "пора
        ли доворачивать" (свой кулдаун _next_aim_correction_at + мёртвая
        зона) и слал aim_with_stick() как разовую команду в очередь
        InputManager — теперь это просто мгновенная запись в "почтовый
        ящик" фонового _aim_worker (см. InputManager.aim_with_stick()),
        дешёвая операция, которую можно (и нужно) звать каждый тик.
        Решение "двигать стик сейчас или ждать" (мёртвая зона, сглаживание,
        тремор, реализм) принимает уже сам _aim_worker в своём потоке —
        FSM здесь только поставляет самые свежие координаты, ничего не
        решая сама.
        """
        self.input_manager.aim_with_stick(aim_info.offset_x, aim_info.offset_y)

    def _log_vision_status(
        self, panel_info: TargetInfo, aim_info: TargetInfo, now: float
    ) -> None:
        """
        Троттлированная строка VISION в gamepad_debug.log — раз в
        _VISION_LOG_INTERVAL_S, а не каждый тик (иначе 60 строк в секунду
        утопили бы полезные AIM/COMBO записи в том же файле). Единственный
        способ УВИДЕТЬ вживую во время реального прогона (F5), что
        VisionManager реально что-то находит и какие числа считает, не
        поднимая отдельное cv2.imshow-окно (то живёт только в песочнице
        vision_manager.py).

        Печатает ОБА источника раздельно (панель — источник правды про
        found/hp, мировой бар — источник offset для прицела) — иначе по
        логу было бы не видно случаев их расхождения (см. докстринг
        класса), а это самое интересное место для отладки.
        """
        if now < self._next_vision_log_at:
            return
        self._next_vision_log_at = now + self._VISION_LOG_INTERVAL_S
        if panel_info.found:
            gamepad_debug_logger.debug(
                "VISION  panel: found=True hp=%.1f%%  |  aim: found=%s offset=(%+.1f, %+.1f)",
                panel_info.hp_percent, aim_info.found, aim_info.offset_x, aim_info.offset_y,
            )
        else:
            gamepad_debug_logger.debug(
                "VISION  panel: found=False (цели нет)  |  aim: found=%s", aim_info.found
            )


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
        """
        Мок VisionManager: ДВА метода зрения (панель + мировой бар), см.
        докстринг класса FarmBot про "два источника зрения, две разные
        роли". bot.py вызывает get_panel_target_info() ПЕРВЫМ на каждом
        тике — счётчик self._tick увеличивается именно там, а
        get_target_info() просто читает уже обновлённое значение, чтобы
        оба мок-метода оставались синхронны внутри одного тика FSM.
        """

        def __init__(self) -> None:
            self._tick = 0

        def build_roi(self, *_args, **_kwargs) -> dict:
            return {"left": 0, "top": 0, "width": 300, "height": 300}

        def reset_tracking(self) -> None:
            logger.info("[MOCK VISION] reset_tracking()")

        def get_panel_target_info(self, _roi: dict) -> TargetInfo:
            self._tick += 1
            # Сценарий: первые ~2 сек цели нет (SEARCH ищет), затем ~3 сек
            # цель есть (панель её видит), потом цель пропадает.
            found = 120 < self._tick < 300
            return TargetInfo(
                found=found,
                hp_percent=75.0 if found else 0.0,
                offset_x=0.0,
                offset_y=0.0,
            )

        def get_target_info(self, _roi: dict) -> TargetInfo:
            # Мировой бар: тот же found-сценарий (для простоты мока — в
            # реальности они могут на миг расходиться, см. докстринг
            # класса), но ещё и "гуляет" синусоидой по X — проверяем, что
            # COMBAT периодически шлёт коррекцию прицела.
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

        def execute_combo(self, actions: list, cast_time_s: float | None = None) -> None:
            logger.info("[MOCK INPUT] execute_combo(%s)", actions)

        def cancel_pending_actions(self) -> None:
            logger.info("[MOCK INPUT] cancel_pending_actions()")

        def aim_with_stick(self, dx: float, dy: float) -> None:
            logger.info("[MOCK INPUT] aim_with_stick(dx=%.1f, dy=%.1f)", dx, dy)

        def clear_aim_target(self) -> None:
            logger.info("[MOCK INPUT] clear_aim_target()")

        def is_idle(self) -> bool:
            # _act_combat_rotation() гейтится на is_idle() ПЕРЕД тем, как
            # решить, кастовать ли цепочку/автоатаку — реальный
            # InputManager держит здесь состояние очереди, мок всегда
            # "свободен", этого достаточно для проверки переходов FSM.
            return True

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
    bot._panel_roi = bot.vision.build_roi()
    bot.state = State.SEARCH
    bot._next_action_at = 0.0
    bot._next_vision_log_at = 0.0
    bot._target_lost_since = None
    bot._autoattack_engaged = False
    # Пустой список — в песочнице ротация всегда падает на автоатаку,
    # этого достаточно, чтобы проверить переходы состояний. Реальные
    # цепочки с ROI тестируются только в игре, не в этой песочнице.
    bot._chains = []
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