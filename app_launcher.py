"""
app_launcher.py

Мост между React-интерфейсом (EasyFarm Bot Suite, папка dist/ после
`npm run build` в репозитории фронтенда) и реальным ботом (FarmBot из
src/core/bot.py) — через PyWebView. Каждый публичный метод класса Api()
становится доступен из JS как window.pywebview.api.<имя>(...), ровно как
договаривались в требованиях к фронтенду.

РАСПОЛОЖЕНИЕ ФАЙЛА: положил в корень проекта (рядом с src/ и с тем местом,
куда `npm run build` кладёт dist/) — по аналогии с примером в модалке
".EXE" самого приложения и с тем, как обычно организуют точку входа для
PyInstaller (--onefile ожидает один файл рядом со всем, что он упаковывает).
Итоговая структура проекта ещё не зафиксирована (см. Claude.md), так что
если она в итоге другая — просто скажи, куда переложить, это не поменяет
ничего внутри самого файла, кроме путей ниже.

ВАЖНО ПРО ПОТОКИ (реалтайм-производительность): bot.run() — БЕСКОНЕЧНЫЙ
цикл на 60 FPS (см. докстринг FarmBot.run в bot.py). Если вызвать его
прямо из обработчика JS-кнопки, GUI-поток PyWebView зависнет на всё время
боя — окно перестанет перерисовываться и реагировать на клики, а это уже
не "микрофриз", а полная заморозка интерфейса. Поэтому start_bot() НЕ
вызывает run() напрямую, а стартует ОТДЕЛЬНЫЙ поток-демон с run() внутри —
тот же принцип разделения потоков, что уже есть в InputManager (поток FSM
отдельно от воркер-потока ввода).
"""

import os
import sys
import json
import time
import base64
import ctypes
import logging
import shutil
import threading

import webview

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.core.bot import FarmBot
from src.core.skills_config import load_skills_config
from src.core.input_manager import InputManager, gamepad_debug_logger

logger = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "src", "config", "skills_config.json")
_ICONS_DIR = os.path.join(os.path.dirname(__file__), "src", "config", "icons")
_DIST_INDEX = os.path.join(os.path.dirname(__file__), "dist", "index.html")

# Копия иконок ВНУТРИ dist/ — pywebview у нас реально раздаёт страницу не как
# file://, а через встроенный HTTP-сервер (Bottle) с корнем ровно в dist/
# (см. лог "HTTP server root path: ...\dist" при запуске) — путь за пределами
# этого корня сервер никогда не отдаст, даже если файл физически есть на
# диске. _ICONS_DIR (выше) остаётся ЕДИНСТВЕННЫМ настоящим источником —
# именно он переживает перенос папки EZF на другой ПК; _DIST_ICONS_DIR — это
# одноразовая копия для раздачи, которую мы сами пересоздаём при каждом
# запуске (см. _sync_icons_to_dist), потому что npm run build перезаписывает
# всю папку dist/ целиком и стирает всё, что там лежало раньше.
_DIST_ICONS_DIR = os.path.join(os.path.dirname(__file__), "dist", "icons")
# Срок таймера автоостановки — файлом рядом с программой (переживает перезапуск окна).
_SHUTDOWN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shutdown_timer.json")
# Настройки окна, которые переживают перезапуск (окно в private_mode — localStorage не годится):
# сейчас только выключатель "Чат-бот".
_UI_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui_state.json")

# Старт/стоп бота — клавиша End (WinAPI VK-таблица: VK_END = 0x23). 2026-10-05: раньше
# были F4 и F5, но F1-F8 теперь бинды выбора цели в игре (бот сам жмёт их) — старт/стоп
# переехал на End, которую ни игра, ни бот не используют.
_HOTKEY_TOGGLE_VK = 0x23

# Других глобальных хоткеев нет (2026-10-05): F1-F8 — бинды выбора цели в игре, и любой
# F-хоткей приложения срабатывал бы от них. Тест комбо — только кнопкой "Тест" в окне.

# Как часто опрашиваем клавишу. 50мс с большим запасом достаточно для
# реакции человека и никак не связано с 60 FPS циклом самого бота — тот
# крутится в своём собственном потоке (см. FarmBot.run).
_HOTKEY_POLL_INTERVAL_SEC = 0.05


def _sync_icons_to_dist() -> None:
    """
    Копирует все *.png из _ICONS_DIR (постоянное хранилище) в _DIST_ICONS_DIR
    (то, что реально может отдать встроенный HTTP-сервер, см. комментарий у
    _DIST_ICONS_DIR выше). Вызывается ОДИН раз при старте приложения, ДО
    открытия окна — чтобы иконки, загруженные в прошлых сессиях, были видны
    сразу на первом рендере, а не только после следующей загрузки фото.

    os.makedirs(..., exist_ok=True) — а не проверка "если папки нет, создать":
    так короче и нет гонки между проверкой и созданием (пусть и не критичной
    тут, в однопоточном месте на старте, но это общепринятая идиома в Python
    именно по этой причине — просить прощения, а не разрешения).
    """
    if not os.path.isdir(_ICONS_DIR):
        return
    os.makedirs(_DIST_ICONS_DIR, exist_ok=True)
    for filename in os.listdir(_ICONS_DIR):
        if not filename.lower().endswith(".png"):
            continue
        try:
            shutil.copy2(
                os.path.join(_ICONS_DIR, filename),
                os.path.join(_DIST_ICONS_DIR, filename),
            )
        except Exception:
            logger.exception("_sync_icons_to_dist: не смог скопировать '%s'.", filename)


def _set_dpi_aware() -> None:
    """
    Явно сообщаем Windows, что процесс сам умеет работать с масштабированием
    экрана (DPI). Без этого вызова Windows по умолчанию считает Python-
    процесс "DPI-неосведомлённым" и подменяет ему координаты на
    виртуализированные — а mss/BitBlt (см. VisionManager._capture) работают
    именно в НАСТОЯЩИХ системных пикселях. На мониторах с масштабом больше
    100% (частый дефолт на современных экранах) это расхождение приводило
    к падению самого BitBlt с невнятной "no error provided".

    PROCESS_PER_MONITOR_DPI_AWARE (2) — не PROCESS_SYSTEM_DPI_AWARE (1):
    именно per-monitor вариант корректно ведёт себя, если у пользователя
    несколько мониторов с РАЗНЫМ масштабом (важно, раз бот уходит и к
    другим покупателям с непредсказуемым железом).

    shcore.SetProcessDpiAwareness — API из Windows 8.1+; на более старой
    Windows (7 и ниже) этой библиотеки нет, там ловим исключение и
    откатываемся на user32.SetProcessDPIAware() — более грубый, "все
    мониторы разом" вариант, но он есть начиная с Vista и лучше, чем
    вообще ничего.
    """
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            logger.warning(
                "_set_dpi_aware: ни shcore, ни user32 DPI-API недоступны "
                "(очень старая Windows?) — захват экрана может ловить "
                "BitBlt-ошибки на масштабированных экранах."
            )


def _is_key_down(vk_code: int) -> bool:
    """
    Проверяет, зажата ли клавиша ПРЯМО СЕЙЧАС — через GetAsyncKeyState.
    Это ЧТЕНИЕ состояния клавиатуры (тот же принцип, что чтение позиции
    курсора мыши), а не отправка ввода — под запрет на
    pyautogui/keyboard/mouse не попадает. Пакет `keyboard` сознательно не
    используется: он в списке запрещённых и ставит системный хук на
    клавиатуру — это более заметный след, чем разовый опрос одной клавиши.

    Главное отличие от F4 внутри самого React-интерфейса
    (window.addEventListener('keydown')): та версия слышит нажатие ТОЛЬКО
    пока фокус ОС стоит на окне EasyFarm. Но пока бот реально работает в
    бою, фокус обязан быть на окне ИГРЫ — иначе виртуальный геймпад до неё
    не долетит. GetAsyncKeyState же глобальная: видит нажатие F4 всегда,
    в каком бы окне ни стоял фокус — единственный вариант, при котором
    паника-кнопка реально работает в момент, когда она нужна.
    """
    return bool(ctypes.windll.user32.GetAsyncKeyState(vk_code) & 0x8000)


class Api:
    """
    Один экземпляр на всё время жизни окна. FarmBot создаётся ЛЕНИВО — не
    трогаем VisionManager/InputManager (реальный виртуальный геймпад) до
    первого нажатия "Старт", чтобы окно открывалось мгновенно даже без
    запущенной игры, и повторные Старт/Стоп не плодили новые виртуальные
    геймпады при каждом клике.
    """

    def __init__(self) -> None:
        self._bot: FarmBot | None = None
        self._bot_thread: threading.Thread | None = None

        # ВРЕМЕННО, для диагностики: отдельный InputManager ТОЛЬКО для
        # F6-теста, который переживает НЕСКОЛЬКО нажатий F6 подряд — в
        # отличие от прежней версии test_rotation(), которая создавала
        # новый vg.VX360Gamepad() (то есть новое виртуальное устройство)
        # на КАЖДОЕ нажатие. Нужен, чтобы проверить гипотезу: теряется ли
        # именно первый скилл С НОВОГО устройства (тогда 2-е и следующие
        # нажатия F6 на ЭТОМ ЖЕ InputManager должны отыграть все 5 скиллов
        # без потерь), или дело в чём-то другом.
        self._test_input_manager: InputManager | None = None

        # Глобальный F4-хоткей стартует СРАЗУ при создании Api — то есть
        # ещё до того, как игрок вообще нажмёт "Старт" мышкой. Живёт всё
        # время работы окна, отдельным потоком-демоном (тот же приём, что
        # и с self._bot_thread ниже) — не мешает GUI перерисовываться.
        self._hotkey_thread = threading.Thread(
            target=self._global_hotkey_watcher, daemon=True, name="GlobalHotkeyEnd"
        )
        self._hotkey_thread.start()

        # Таймер автоостановки (часики слева от "Старт", 2026-10-10). Срок храним ЗДЕСЬ, а не
        # только в браузере: (1) WebView2 почти усыпляет таймеры JS, когда окно свёрнуто, —
        # остановка опоздала бы до минуты; (2) окно у нас в private_mode — localStorage не
        # переживает перезапуск, а файл переживает.
        self._window = None                       # main() подставит окно — для уведомления интерфейса
        self._shutdown_lock = threading.Lock()
        # Два режима (2026-10-10): "duration" — "бот работает N минут": срок взводится, отсчёт
        # идёт С МОМЕНТА СТАРТА бота (кнопкой или End), на Стоп отсчёт снимается, а N минут
        # остаются взведены до отмены; "clock" — "остановить в ЧЧ:ММ" — по часам.
        self._shutdown_mode: str | None = None
        self._shutdown_armed_s: float | None = None
        self._shutdown_deadline: float | None = None
        self._load_shutdown_state()
        threading.Thread(target=self._shutdown_watcher, daemon=True, name="ShutdownTimer").start()

        # Итог последней сессии (окно "Итог сессии"): интерфейс мог перезагрузиться и пропустить
        # событие — тогда заберёт его через get_last_session().
        self._last_session: dict | None = None
        self._last_session_start: float | None = None
        # Снимок скиллов: один за раз (двойной клик не запустит вторую съёмку поверх первой).
        self._capture_lock = threading.Lock()
        # Чат-бот: выключатель в интерфейсе. Включён = работает вместе с ботом (стартует на "Старт",
        # засыпает на "Стоп"); выключен — не стартует, а работающий останавливается.
        self._chat_enabled = self._load_ui_state().get("chat_enabled", True)
        # Режим фарма (2026-10-20): "pull" — агр мобов тычками и бой у дома, "normal" — бить всех без агра.
        # Хранится в ui_state.json, как чат; применяется на каждом "Старт" (bot.pull_mode).
        self._farm_mode = self._initial_farm_mode()
        self._chat_thread: threading.Thread | None = None
        self._chat_stop: threading.Event | None = None

    def _toggle_bot(self, key_label: str) -> None:
        """
        Общая логика "старт/стоп" для хоткея-переключателя (сейчас это End,
        см. _HOTKEY_TOGGLE_VK). Вынесено
        в отдельный метод, а не продублировано в watcher-цикле дважды под
        каждую клавишу — одна точка правки, если логика переключения
        когда-нибудь изменится.

        key_label только для лога (какая именно клавиша сработала) —
        на само поведение не влияет, обе клавиши равноправны.
        """
        if self._bot_thread is not None and self._bot_thread.is_alive():
            logger.info("%s (глобально): бот работает — останавливаю.", key_label)
            self.stop_bot()
        else:
            logger.info("%s (глобально): бот не запущен — запускаю.", key_label)
            self.start_bot()

    def _bot_pressed(self, key: str) -> bool:
        """Эту клавишу только что нажал сам бот (InputManager.injected_recently), а не человек."""
        bot = getattr(self, "_bot", None)
        im = getattr(bot, "input_manager", None) if bot is not None else None
        fn = getattr(im, "injected_recently", None)
        return bool(fn is not None and fn(key))

    def _global_hotkey_watcher(self) -> None:
        """
        Крутится в фоне всё время жизни окна и раз в
        _HOTKEY_POLL_INTERVAL_SEC проверяет, не нажали ли End (старт/стоп) —
        работает, даже когда фокус ОС стоит на окне игры, а не на EasyFarm
        (см. docstring _is_key_down выше, там объяснение ПОЧЕМУ это важно).

        Ловим именно ФРОНТ нажатия (переход "не зажата" -> "зажата"), а не
        сам факт "зажата прямо сейчас" — иначе, пока игрок физически
        держит клавишу дольше одного опроса (50мс), действие сработало бы
        десятки раз подряд без остановки. was_pressed_end — это
        "снимок" состояния клавиши на прошлом опросе, с которым сравниваем текущий.
        """
        # CoInitializeEx(None, 0) — COINIT_MULTITHREADED. Этот поток создан
        # голым threading.Thread, а не через механизм pywebview — то есть
        # Windows никогда не инициализировала для него COM (Component
        # Object Model). GUI-поток pywebview получает COM-инициализацию
        # неявно изнутри WebView2/Chromium; наш поток — нет. F6 в итоге
        # вызывает test_rotation() -> InputManager() -> vg.VX360Gamepad(),
        # а vgamepad внутри дёргает ViGEmBus через COM-совместимый слой —
        # без явной инициализации COM на текущем потоке это может уйти в
        # неопределённое поведение на уровне native-кода (не Python-
        # исключение, поэтому наш except Exception его никогда не поймает),
        # что и похоже на наблюдаемый крэш всего процесса. Зовём это ОДИН
        # раз в начале потока, до цикла — COM-инициализация держится на
        # весь срок жизни потока, повторный вызов внутри while не нужен.
        ctypes.windll.ole32.CoInitializeEx(None, 0)

        was_pressed_end = False
        while True:
            is_pressed_end = _is_key_down(_HOTKEY_TOGGLE_VK)
            if is_pressed_end and not was_pressed_end:
                self._toggle_bot("End")
            was_pressed_end = is_pressed_end
            time.sleep(_HOTKEY_POLL_INTERVAL_SEC)

    # ------------------------------------------------------------------
    # Управление ботом
    # ------------------------------------------------------------------

    def start_bot(self) -> dict:
        if self._bot_thread is not None and self._bot_thread.is_alive():
            logger.warning("start_bot: бот уже запущен, повторный запуск игнорирован.")
            return {"ok": False, "error": "already_running"}

        try:
            if self._bot is None:
                # Первый запуск за сессию окна — поднимаем VisionManager
                # (mss) и InputManager (vgamepad/ViGEmBus) взаправду.
                self._bot = FarmBot()
            else:
                # Повторный Старт после Стопа: тот же объект, снимаем флаг
                # паники и перечитываем конфиг — пользователь мог поменять
                # цепочки между запусками, не перезапуская всё приложение
                # (а значит, и не создавая новый виртуальный геймпад).
                self._bot.input_manager.reset_abort()
                self._bot._chains = load_skills_config(_CONFIG_PATH)
        except Exception as e:
            logger.exception("start_bot: не удалось инициализировать FarmBot.")
            return {"ok": False, "error": str(e)}

        # (Холостой повтор первого скилла при Старте убран 2026-10-03 по просьбе
        # пользователя: это была заплатка от виртуального геймпада — клавиатуре
        # она не нужна, а скилл зря уходил в откат ещё до боя.)

        # Кнопка "Старт" в программе: активно окно программы, и нажатия бота ушли бы ЕЙ, а не игре
        # (лог друга 2026-10-11). Игру — вперёд, как у "Снять скилы". Зовём ДО запуска потока и пока
        # окно программы ещё активно: Windows даёт сменить фокус только активному процессу.
        # По End из игры она уже спереди — focus_game просто ничего не меняет.
        try:
            from src.core.game_window import focus_game
            game_ok, game_why, _ = focus_game()
            logger.info("start_bot: %s", game_why)
        except Exception as e:                    # фокус — удобство, не повод не запускать бота
            logger.warning("start_bot: не смог вывести игру вперёд: %s", e)

        self._bot.pull_mode = self._farm_mode == "pull"   # режим из переключателя — на каждый Старт
        logger.info("start_bot: режим фарма — %s", "пул (агр мобов)" if self._bot.pull_mode else "обычный")
        self._bot_thread = threading.Thread(
            target=self._bot.run, daemon=True, name="FarmBotLoop"
        )
        self._bot_thread.start()
        self._shutdown_on_start()          # таймер "N минут" — отсчёт со старта

        if self._chat_enabled:
            self._chat_spawn()                # чат-бот — только если включён выключателем в интерфейсе
        logger.info("start_bot: бот запущен в фоновом потоке.")
        return {"ok": True}

    def stop_bot(self, reason: str = "user") -> dict:
        """reason: "user" — кнопка Стоп / End, "timer" — сработал таймер выключения (для окна итога)."""
        if self._bot is None:
            return {"ok": True}

        self._bot.stop()
        # halt_immediately — та же экстренная остановка, что и по
        # F4-панике: отпускает всё зажатое НЕМЕДЛЕННО, не дожидаясь, пока
        # run() сам заметит self._running = False на следующем тике.
        # Лишний вызов reset() дешёвый, а зажатая кнопка/стик после
        # "Стоп" — это персонаж, который продолжает крутить камеру или
        # долбить атаку уже после того, как пользователь решил, что бот
        # остановлен.
        self._bot.input_manager.halt_immediately()
        self._shutdown_on_stop()
        self._send_session_summary(reason if reason in ("user", "timer") else "user")
        logger.info("stop_bot: остановлен.")
        return {"ok": True}

    # ------------------------------------------------------------------
    # События в интерфейс, итог сессии
    # ------------------------------------------------------------------

    def _emit(self, name: str, detail: dict) -> None:
        """CustomEvent в окне: интерфейс ловит window.addEventListener(name, e => e.detail).
        json.dumps — и экранирование кавычек, и готовый JS-литерал объекта: строку руками не склеиваем."""
        w = self._window
        if w is None:
            return
        try:
            w.evaluate_js("window.dispatchEvent(new CustomEvent(%s, {detail: %s}))"
                          % (json.dumps(name), json.dumps(detail, ensure_ascii=False)))
        except Exception:
            logger.debug("событие %s не доставлено в окно", name, exc_info=True)

    def _send_session_summary(self, reason: str) -> None:
        """Один раз на сессию: Стоп может прийти дважды (кнопка + End/таймер) — окно не дублируем."""
        bot = self._bot
        ses = getattr(bot, "_session", None) if bot is not None else None
        fn = getattr(bot, "session_stats", None)
        if ses is None or fn is None or ses.get("start") == self._last_session_start:
            return
        stats = fn()
        if stats is None:
            return
        stats["stop_reason"] = reason
        self._last_session, self._last_session_start = stats, ses.get("start")
        self._emit("easyfarm:session-summary", stats)

    def get_last_session(self) -> dict | None:
        """JS: итог последней сессии (тот же объект, что в событии easyfarm:session-summary) или null."""
        return self._last_session

    # ------------------------------------------------------------------
    # Снимки готовых скиллов (кнопка "Снять скиллы")
    # ------------------------------------------------------------------

    _CAPTURE_SECONDS = 4.0          # столько снимаем панель: досиживающий откат скилл успеет выйти из него
    _CAPTURE_MINIMIZE_WAIT_S = 1.0  # окно свернулось, игра перерисовалась — только потом снимаем

    def capture_skills(self) -> dict:
        """
        JS: await window.pywebview.api.capture_skills() — ~6 с. Выводит окно игры вперёд
        (src/core/game_window.py), сворачивает окно программы, находит панель по форме (любой
        аккаунт и бинды — клавиши читаются с табличек), снимает её (src/core/vision/ready_capture.py),
        разворачивает окно. Прогресс —
        событием easyfarm:capture-progress. Бот подхватит снимки на следующем "Старт".
        Ничего не нажимает — только смотрит на экран.
        """
        fail = {"ok": False, "preview_png": None, "slots": []}
        if self._bot_running():
            return dict(fail, message="Останови бота перед снимком скиллов")
        if not self._capture_lock.acquire(blocking=False):
            return dict(fail, message="Снимок уже идёт")
        w = self._window
        try:
            # Логика снимка — в ядре (src/core/vision/ready_capture.py): та же у автоснимка при "Старт".
            from src.core.vision.ready_capture import build_snapshots, grab_frames
            from src.core.game_window import focus_game
            total = int(self._CAPTURE_MINIMIZE_WAIT_S + self._CAPTURE_SECONDS + 1)
            self._emit("easyfarm:capture-progress", {"stage": "minimize", "seconds_left": total})
            # Сначала игру ВПЕРЁД, потом своё окно свернуть: Windows даёт сменить активное окно только
            # активному процессу, а после сворачивания активными мы уже не будем.
            game_ok, game_why, game_was_min = focus_game()
            logger.info("Снимок скиллов: %s", game_why)
            if w is not None:
                w.minimize()
            # Игра была свёрнута — ей нужно больше времени перерисоваться после разворота.
            time.sleep(self._CAPTURE_MINIMIZE_WAIT_S + (0.5 if game_was_min else 0.0))
            frames, bar, origin = grab_frames(
                self._CAPTURE_SECONDS,
                on_second=lambda n: self._emit("easyfarm:capture-progress", {"stage": "capture", "seconds_left": n}))
            self._emit("easyfarm:capture-progress", {"stage": "save", "seconds_left": 0})
            res = build_snapshots(frames, source="кнопка в программе", bar=bar, origin=origin)
            if not res["ok"] and not game_ok:
                res["message"] += " (%s)" % game_why     # подсказка, почему на экране не было игры
            res.pop("preview_path", None)
            res.pop("scale", None)
            bad = [x["key"] for x in res.get("slots", []) if not x["ok"]]
            logger.info("Снимок скиллов: %s%s", res["message"], (" (проверь: %s)" % " ".join(bad)) if bad else "")
            return res
        except Exception as e:
            logger.exception("capture_skills упал")
            return dict(fail, message="Ошибка снимка: %s" % e)
        finally:
            if w is not None:
                try:
                    w.restore()           # окно программы обратно — с результатом снимка
                except Exception:
                    pass
            self._capture_lock.release()

    # ------------------------------------------------------------------
    # Чат-бот (выключатель в интерфейсе)
    # ------------------------------------------------------------------

    _FARM_MODES = ("pull", "normal")

    def _initial_farm_mode(self) -> str:
        """Переменная EZF_PULL (tools/pull_test.py, откат) важнее сохранённого выбора; иначе — из
        ui_state.json; по умолчанию — пул."""
        env = os.getenv("EZF_PULL")
        if env in ("0", "1"):
            return "pull" if env == "1" else "normal"
        mode = self._load_ui_state().get("farm_mode", "pull")
        return mode if mode in self._FARM_MODES else "pull"

    def set_farm_mode(self, mode: str) -> dict:
        """
        JS: window.pywebview.api.set_farm_mode("pull" | "normal") — переключатель режима фарма.
        Бот работает — режим сменится на следующем "Старт" (на ходу не переключаем: пул посреди
        сбора бросил бы паровоз). Ответ: {"ok", "farm_mode", "applies": "now" | "next_start", "message"}.
        """
        if mode not in self._FARM_MODES:
            return {"ok": False, "error": "bad_mode", "farm_mode": self._farm_mode}
        self._farm_mode = mode
        self._save_ui_state()
        name = "агр мобов (пул)" if mode == "pull" else "обычный (бить всех без агра)"
        later = self._bot_running()
        logger.info("Режим фарма: %s%s", name, " — со следующего Старта" if later else "")
        return {"ok": True, "farm_mode": mode, "applies": "next_start" if later else "now",
                "message": "Режим: %s%s" % (name, " — включится после Стоп/Старт" if later else "")}

    def get_farm_mode(self) -> dict:
        """JS: window.pywebview.api.get_farm_mode() -> {"ok": True, "farm_mode": "pull" | "normal"}."""
        return {"ok": True, "farm_mode": self._farm_mode}

    def _load_ui_state(self) -> dict:
        try:
            with open(_UI_STATE_FILE, encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_ui_state(self, **extra) -> None:
        """Дописываем, а не перезаписываем: в файле живут и чат, и размер окна (window_size) —
        запись одного не должна стирать другое."""
        d = self._load_ui_state()
        d["chat_enabled"] = self._chat_enabled
        d["farm_mode"] = getattr(self, "_farm_mode", "pull")
        d.update(extra)
        try:
            with open(_UI_STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(d, f)
        except OSError:
            logger.warning("не смог сохранить %s", _UI_STATE_FILE)

    def _chat_alive(self) -> bool:
        return self._chat_thread is not None and self._chat_thread.is_alive()

    def _chat_spawn(self) -> bool:
        """Поднять поток чат-бота при работающем боте. Свой Event на каждый поток: stop_chat гасит
        именно его, а новый старт не "оживит" поток, которому уже сказали выйти."""
        if self._bot is None:
            return False
        old = self._chat_thread
        if old is not None and old.is_alive():
            if self._chat_stop is not None and not self._chat_stop.is_set():
                return True                    # уже работает
            old.join(timeout=2.0)              # прошлый ещё выходит (ждал ответ LLM) — не плодим два
        try:
            from tools.chat_bot_test import start_chat_thread
            self._chat_stop = threading.Event()
            self._chat_thread = start_chat_thread(self._bot, self._chat_stop)
            logger.info("Чат-бот с ИИ запущен в фоне.")
            return True
        except Exception as e:
            logger.exception("Не удалось запустить чат-бота")
            gamepad_debug_logger.debug("CHAT    не запустился: %s", e)
            return False

    def start_chat(self) -> dict:
        """JS: включить чат-бота. Бот работает — чат стартует сразу, нет — вместе со "Старт"."""
        self._chat_enabled = True
        self._save_ui_state()
        if not self._bot_running():
            return {"ok": True, "message": "Чат-бот включится вместе с ботом"}
        if self._chat_spawn():
            return {"ok": True, "message": "Чат-бот включён (распознавание текста грузится до минуты)"}
        self._chat_enabled = False
        self._save_ui_state()
        return {"ok": False, "message": "Чат-бот не запустился — подробности в консоли"}

    def stop_chat(self) -> dict:
        """JS: выключить чат-бота (бот продолжает фармить)."""
        self._chat_enabled = False
        self._save_ui_state()
        if self._chat_stop is not None:
            self._chat_stop.set()
        logger.info("Чат-бот выключен.")
        return {"ok": True, "message": "Чат-бот выключен"}

    # ------------------------------------------------------------------
    # Таймер автоостановки (часики в интерфейсе)
    # ------------------------------------------------------------------

    def _load_shutdown_state(self) -> None:
        try:
            with open(_SHUTDOWN_FILE, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            return
        mode = d.get("mode") or ("clock" if d.get("deadline") else None)
        if mode == "duration" and float(d.get("armed_s") or 0) > 0:
            self._shutdown_mode, self._shutdown_armed_s = "duration", float(d["armed_s"])
            # Отсчёт не идёт: программа только открылась, бот ещё не запущен — начнётся со Старта.
        elif mode == "clock" and float(d.get("deadline") or 0) > time.time():
            self._shutdown_mode, self._shutdown_deadline = "clock", float(d["deadline"])

    def _save_shutdown_state(self) -> None:
        try:
            if self._shutdown_mode is None:
                if os.path.exists(_SHUTDOWN_FILE):
                    os.remove(_SHUTDOWN_FILE)
            else:
                with open(_SHUTDOWN_FILE, "w", encoding="utf-8") as f:
                    json.dump({"mode": self._shutdown_mode, "armed_s": self._shutdown_armed_s,
                               "deadline": self._shutdown_deadline}, f)
        except Exception:
            logger.exception("Таймер: не смог сохранить состояние в %s", _SHUTDOWN_FILE)

    def _bot_running(self) -> bool:
        return self._bot_thread is not None and self._bot_thread.is_alive() \
            and bool(getattr(self._bot, "_running", False))

    def _log_deadline(self, why: str) -> None:
        d = self._shutdown_deadline
        logger.info("Таймер: %s — бот остановится в %s.", why, time.strftime("%H:%M:%S", time.localtime(d)))
        gamepad_debug_logger.debug("TIMER   %s — остановка в %s", why, time.strftime("%H:%M:%S", time.localtime(d)))

    def set_shutdown_duration(self, minutes) -> dict:
        """
        JS: window.pywebview.api.set_shutdown_duration(60) — "бот работает 60 минут".
        Отсчёт начинается при старте бота; если бот уже работает — с этой секунды.
        """
        try:
            secs = float(minutes) * 60.0
        except (TypeError, ValueError):
            return {"ok": False, "error": "bad_minutes"}
        if not (60.0 <= secs <= 24 * 3600.0):
            return {"ok": False, "error": "minutes_out_of_range"}
        with self._shutdown_lock:
            self._shutdown_mode, self._shutdown_armed_s = "duration", secs
            self._shutdown_deadline = time.time() + secs if self._bot_running() else None
            self._save_shutdown_state()
        if self._shutdown_deadline is not None:
            self._log_deadline("взведено на %.0f мин, бот уже работает" % (secs / 60))
        else:
            logger.info("Таймер: взведено на %.0f мин — отсчёт начнётся со Старта бота.", secs / 60)
        return self.get_status()

    def set_shutdown_timer(self, deadline_ms) -> dict:
        """
        JS: window.pywebview.api.set_shutdown_timer(ms) — "остановить в ЧЧ:ММ": момент остановки
        (мс с 1970, как Date.now()), по часам, независимо от того, когда запущен бот.
        """
        try:
            deadline = float(deadline_ms) / 1000.0
        except (TypeError, ValueError):
            return {"ok": False, "error": "bad_deadline"}
        if deadline <= time.time():
            return {"ok": False, "error": "deadline_in_past"}
        with self._shutdown_lock:
            self._shutdown_mode, self._shutdown_armed_s, self._shutdown_deadline = "clock", None, deadline
            self._save_shutdown_state()
        self._log_deadline("по часам")
        return self.get_status()

    def cancel_shutdown_timer(self) -> dict:
        """JS: window.pywebview.api.cancel_shutdown_timer() — снять таймер (оба режима)."""
        with self._shutdown_lock:
            had = self._shutdown_mode is not None
            self._shutdown_mode = self._shutdown_armed_s = self._shutdown_deadline = None
            self._save_shutdown_state()
        if had:
            logger.info("Таймер: отменён.")
        return self.get_status()

    def get_status(self) -> dict:
        """
        JS: опрашивать раз в 1-2 с.
          running               — бот работает (в т.ч. запущен клавишей End);
          shutdown_mode         — null | "duration" | "clock";
          shutdown_minutes      — взведённые минуты (режим duration) или null;
          shutdown_deadline_ms  — когда остановится (идёт отсчёт) или null (не идёт: бот не запущен);
          chat_running          — выключатель "Чат-бот" включён (работает вместе с ботом).
          farm_mode             — "pull" (агр мобов, бой у дома) | "normal" (бить всех без агра).
          vision_warnings       — список строк-подсказок: бот чего-то не видит на экране (пусто — ок).
        """
        d = self._shutdown_deadline
        a = self._shutdown_armed_s
        return {"ok": True, "running": self._bot_running(), "shutdown_mode": self._shutdown_mode,
                "shutdown_minutes": round(a / 60.0, 2) if a else None,
                "shutdown_deadline_ms": int(d * 1000) if d else None,
                "chat_running": bool(self._chat_enabled),
                "farm_mode": self._farm_mode,
                # Проверка зрения при старте (bot._vision_check_*): подсказки, что поправить, если бот
                # не видит список целей / миникарту / панель HP. Пусто — всё в порядке.
                "vision_warnings": list(getattr(self._bot, "vision_warnings", None) or []) + self._chat_warnings()}

    @staticmethod
    def _chat_warnings() -> "list[str]":
        """Чат-бот не может отвечать (битый ключ, лимит) — подсказка в ту же плашку, что и зрение.
        sys.modules.get: модуль чата грузится только при включении чата — не импортируем его ради статуса."""
        cb = sys.modules.get("tools.chat_bot_test")
        fn = getattr(cb, "llm_problem", None)
        msg = fn() if callable(fn) else None
        return [msg] if msg else []

    def _shutdown_on_start(self) -> None:
        """Бот стартовал: режим "N минут" — отсчёт пошёл с этой секунды."""
        with self._shutdown_lock:
            if self._shutdown_mode == "duration" and self._shutdown_armed_s:
                self._shutdown_deadline = time.time() + self._shutdown_armed_s
            else:
                return
        self._log_deadline("бот запущен, отсчёт %.0f мин пошёл" % (self._shutdown_armed_s / 60))

    def _shutdown_on_stop(self) -> None:
        """Бот остановлен: режим "N минут" — отсчёт снят (минуты остаются взведены до отмены)."""
        with self._shutdown_lock:
            if self._shutdown_mode == "duration":
                self._shutdown_deadline = None

    def _shutdown_watcher(self) -> None:
        """Раз в 0.5 с: срок вышел -> стоп бота (как кнопка "Стоп") и событие в интерфейс."""
        while True:
            time.sleep(0.5)
            with self._shutdown_lock:
                d = self._shutdown_deadline
                fire = d is not None and time.time() >= d
                if fire:
                    self._shutdown_deadline = None
                    if self._shutdown_mode == "clock":
                        self._shutdown_mode = None      # "в ЧЧ:ММ" — одноразовый
                    self._save_shutdown_state()
            if not fire:
                continue
            try:
                self.stop_bot("timer")
            except Exception:
                logger.exception("Таймер: stop_bot упал")
            stamp = time.strftime("%H:%M", time.localtime())
            logger.info("Таймер: время вышло — бот остановлен в %s.", stamp)
            gamepad_debug_logger.debug("TIMER   время вышло — бот остановлен")
            w = self._window
            if w is not None:
                try:
                    # Интерфейс ловит: window.addEventListener("easyfarm:shutdown-fired", e => ...)
                    w.evaluate_js("window.dispatchEvent(new CustomEvent('easyfarm:shutdown-fired', "
                                  "{detail: {time: '%s'}}))" % stamp)
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Сохранение конфигурации из интерфейса
    # ------------------------------------------------------------------

    def save_config(self, config_json: str) -> dict:
        """
        JS передаёт JSON.stringify(configObject) целиком — просто пишем
        строку на диск как есть, без валидации формата тут. Валидация и
        разбор — работа load_skills_config(): она произойдёт на следующем
        start_bot()/test_rotation(). Дублировать эту логику здесь означало
        бы два места, которые могут разойтись в понимании формата файла.
        """
        try:
            os.makedirs(os.path.dirname(_CONFIG_PATH), exist_ok=True)
            with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
                f.write(config_json)
        except Exception as e:
            logger.exception("save_config: ошибка записи файла.")
            return {"ok": False, "error": str(e)}
        self._reload_running_bot()
        return {"ok": True}

    def _reload_running_bot(self) -> None:
        """
        Моментальное применение (2026-10-03): бот уже работает -> отдаём ему
        свежие цепочки сразу после сохранения, без Стоп/Старт. Интерфейс
        сохраняет через ~0.6 с после правки, бот подхватывает на следующем
        тике. Ошибка разбора не роняет сохранение: файл уже на диске, бот
        просто продолжает со старыми цепочками (в логе — причина).
        """
        bot = self._bot
        if bot is None or self._bot_thread is None or not self._bot_thread.is_alive():
            return                     # бот не запущен — конфиг прочитает start_bot()
        try:
            bot.request_chains_reload(load_skills_config(_CONFIG_PATH))
            logger.info("save_config: цепочки применены к работающему боту.")
        except Exception:
            logger.exception("save_config: не смог применить цепочки на лету.")

    def load_config(self) -> dict:
        """
        Обратная операция к save_config(): при старте интерфейса читаем
        уже сохранённый skills_config.json и отдаём его на фронт как есть
        (сырой dict), чтобы React восстановил слоты и цепочки — БЕЗ
        привязки к localStorage браузера, который живёт только в профиле
        WebView2 конкретного компьютера. Файл лежит рядом с приложением
        (см. _CONFIG_PATH), поэтому переживает перенос папки EZF целиком
        на другой ПК — то, что нужно для продажи.

        Отсутствие файла — это НЕ ошибка, а нормальный первый запуск (ещё
        ни разу не жали "Старт"/"Тест" и не было автосохранения). В этом
        случае просто говорим фронту "нечего восстанавливать", и он
        остаётся на дефолтном каталоге DEFAULT_HOTKEY_SLOTS/
        DEFAULT_COMBO_CHAINS — тут та же логика, что и раньше.
        """
        if not os.path.exists(_CONFIG_PATH):
            return {"ok": False, "error": "not_found"}
        try:
            with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                raw = f.read()
            config = json.loads(raw)
            return {"ok": True, "config": config}
        except Exception as e:
            logger.exception("load_config: ошибка чтения/разбора файла.")
            return {"ok": False, "error": str(e)}

    def save_icon(self, slot: str, data_url: str) -> dict:
        """
        data_url — строка вида "data:image/png;base64,AAAA..." из
        FileReader.readAsDataURL на стороне React. Отрезаем префикс до
        запятой (метаданные MIME-типа) и декодируем остаток как base64 —
        единственное место в проекте, которое разбирает data URL, поэтому
        не выносим в отдельный модуль ради одного вызова.
        """
        try:
            os.makedirs(_ICONS_DIR, exist_ok=True)
            _header, _, b64_data = data_url.partition(",")
            raw_bytes = base64.b64decode(b64_data)
            safe_name = slot or "unknown"
            path = os.path.join(_ICONS_DIR, f"{safe_name}.png")
            with open(path, "wb") as f:
                f.write(raw_bytes)

            # Сразу дублируем в dist/icons — не ждём следующего перезапуска
            # (когда сработал бы _sync_icons_to_dist из main()). Без этого
            # только что загруженное фото в ЭТОЙ ЖЕ сессии показывалось бы
            # через customIcon из React-состояния (в память, работает), но
            # пропало бы сразу после закрытия и открытия окна заново, пока
            # процесс не перезапустят повторно — плохой UX ради экономии
            # одной строчки.
            os.makedirs(_DIST_ICONS_DIR, exist_ok=True)
            shutil.copy2(path, os.path.join(_DIST_ICONS_DIR, f"{safe_name}.png"))

            return {"ok": True, "path": path}
        except Exception as e:
            logger.exception("save_icon: ошибка сохранения иконки слота '%s'.", slot)
            return {"ok": False, "error": str(e)}

    # ------------------------------------------------------------------
    # Массовая подгрузка иконок с диска
    # ------------------------------------------------------------------

    def get_saved_icons(self) -> dict:
        """
        Отдаёт интерфейсу {skillId: data_url} для ВСЕХ .png-файлов, уже
        лежащих в _ICONS_DIR — интерфейс вызывает это ОДИН раз при
        открытии окна, чтобы подставить картинки, которые попали в папку
        не через ручную загрузку по одной штуке в этой же сессии (сейчас
        так лежат заранее пачкой скопированные иконки лука/посоха).

        Ключ — это имя файла БЕЗ расширения (skill_1, skill_2, ...),
        совпадает с полем "id" в каталоге умений на фронтенде — именно
        по нему интерфейс потом ищет, какую картинку куда подставить.
        """
        result: dict[str, str] = {}
        if not os.path.isdir(_ICONS_DIR):
            return result

        for filename in os.listdir(_ICONS_DIR):
            if not filename.lower().endswith(".png"):
                continue
            skill_id = filename[:-4]
            try:
                with open(os.path.join(_ICONS_DIR, filename), "rb") as f:
                    b64 = base64.b64encode(f.read()).decode("ascii")
                result[skill_id] = f"data:image/png;base64,{b64}"
            except Exception:
                logger.exception("get_saved_icons: не смог прочитать '%s'.", filename)

        logger.info("get_saved_icons: отдаю %d готовых иконок.", len(result))
        return result

    # ------------------------------------------------------------------
    # Тестовый прогон (F5)
    # ------------------------------------------------------------------

    def test_rotation(self) -> dict:
        """
        Прогоняет все цепочки по разу вхолостую — чтобы руками увидеть на
        joy.cpl, что все combo-строки реально нажимают то, что задумано,
        не заходя в игру и не тратя время на поиск цели. Специально
        независим от FarmBot/VisionManager (mss/OpenCV) — тестовый прогон
        не должен требовать запущенную игру или даже установленный OpenCV.

        Отдельная проверка "бот уже бежит" — то же самое уже не даёт
        сделать интерфейс (кнопка задизейблена, пока isBotRunning), но
        бэкенд не должен полагаться только на дисциплину фронтенда: два
        независимых InputManager с двумя виртуальными геймпадами,
        дерущимися за одни и те же кнопки во время реального боя, было бы
        куда хуже, чем просто отказать в тесте.
        """
        if self._bot_thread is not None and self._bot_thread.is_alive():
            logger.warning("test_rotation: бот сейчас активен, тест отклонён.")
            return {"ok": False, "error": "bot_is_running"}

        try:
            chains = load_skills_config(_CONFIG_PATH)

            # ВРЕМЕННО: переиспользуем ОДИН InputManager между нажатиями F6
            # (см. комментарий у self._test_input_manager в __init__), а не
            # создаём новый на каждый вызов. wait_idle() — НЕ stop(): не
            # гасит воркер, следующее нажатие F6 сможет снова ставить
            # команды в ту же очередь того же устройства.
            if self._test_input_manager is None:
                logger.info("test_rotation: создаю тестовый InputManager (первый F6 за сессию).")
                self._test_input_manager = InputManager()
            input_manager = self._test_input_manager

            # (Холостой WARMUP первого скилла убран 2026-10-03 вместе с таким же в
            # start_bot: заплатка от геймпада, F6 жал первый скилл дважды.)

            # ПОЛНОЦЕННЫЙ тест ротации: та же самая боевая
            # _act_combat_rotation() (см. bot.py) — не копия её логики, а
            # прямое переиспользование продового метода, вызванного в
            # отдельном тестовом цикле. Это принципиально: расхождение
            # тестовой и боевой логики уже один раз стало причиной бага с
            # интерливингом цепочек (см. историю is_idle()) — раз тест
            # дёргает ТУ ЖЕ функцию, такое расхождение больше невозможно
            # в принципе, а не только "мы больше так не будем".
            #
            # FarmBot.__new__(FarmBot) — тот же приём, что уже используется
            # в песочнице самого bot.py (блок if __name__ == "__main__":):
            # создаёт экземпляр FarmBot В ОБХОД __init__(), то есть без
            # второго VisionManager/mss — тест не должен требовать
            # запущенную игру. "Одалживаем" у него только то, что реально
            # читает _act_combat_rotation(): self._chains и
            # self.input_manager (последний — тот же переиспользуемый
            # self._test_input_manager с настоящими нажатиями).
            fake_bot = FarmBot.__new__(FarmBot)
            fake_bot._chains = chains
            fake_bot.input_manager = input_manager
            # _next_action_at — тот самый "тормоз темпа действий" из
            # _handle_combat() (0.4-0.8с после каста цепочки, 0.9-1.1с
            # после автоатаки — см. _SKILL_CAST_DELAY/_COMBAT_ATTACK_COOLDOWN
            # в bot.py). is_idle() внутри _act_combat_rotation() защищает
            # ТОЛЬКО от наложения цепочек друг на друга — это ДРУГАЯ,
            # более короткая пауза "очередь InputManager ещё не опустела",
            # а не полноценный интервал между решениями FSM. Без этого
            # поля тестовый цикл дёргал бы автоатаку так часто, как только
            # успевает освободиться очередь после одного нажатия — заметно
            # чаще, чем в реальном бою, и выглядело это как "зависания и
            # повторные нажатия подряд".
            fake_bot._next_action_at = 0.0
            # _autoattack_engaged — новое поле FarmBot: RB жмётся один раз,
            # пока автоатака "не запущена", а не на каждой проверке
            # готовности (см. докстринг в bot.py._act_combat_rotation).
            # Заводим и здесь по той же причине, что и _next_action_at
            # выше — fake_bot создан в обход __init__(), сам себя не
            # проинициализирует.
            fake_bot._autoattack_engaged = False

            # Длительность теста — не константа "от балды", а посчитанная
            # от кулдаунов самих цепочек: берём самый долгий кулдаун (с
            # учётом верхней границы джиттера ±10%) и умножаем на 4, чтобы
            # гарантированно застать несколько повторов КАЖДОЙ цепочки и их
            # чередование между собой. min 30 / max 300 — только защита от
            # крайностей (совсем короткого теста или теста на полчаса при
            # очень большом кулдауне), а не часть основной формулы — если
            # позже поменяешь периодичность цепочек в интерфейсе, тест сам
            # подстроит свою длину под новые цифры, ничего в коде трогать
            # не придётся.
            slowest_cooldown = max(c["cooldown_max"] for c in chains)
            test_duration_s = min(300.0, max(30.0, slowest_cooldown * 4))

            gamepad_debug_logger.debug(
                "########## РОТАЦИЯ-ТЕСТ: %.1f сек (самый долгий кулдаун "
                "%.1fс x4) ##########",
                test_duration_s, slowest_cooldown,
            )
            logger.info(
                "test_rotation: запускаю полноценную ротацию на %.1f сек "
                "(самый долгий кулдаун %.1fс).",
                test_duration_s, slowest_cooldown,
            )

            # Тик каждые 0.1с: тест — не боевой 60 FPS поток, а отдельный
            # синхронный вызов из обработчика кнопки F6, так что
            # time.sleep() здесь безопасен (в отличие от настоящего
            # FSM-цикла в bot.py, который спать не имеет права). 0.1с
            # достаточно часто, чтобы не "проспать" момент, когда
            # is_idle() освобождается или у очередной цепочки истекает
            # кулдаун — сам by is_idle() гейт внутри _act_combat_rotation()
            # и есть тот самый "стой, подожди, пока другая цепочка
            # доиграет", о котором ты спрашивал: ни одна цепочка не
            # начнёт кастоваться, пока предыдущая физически не доиграла в
            # очереди InputManager, даже если её собственный кулдаун уже
            # истёк.
            _TEST_TICK_S = 0.1
            deadline = time.monotonic() + test_duration_s
            while time.monotonic() < deadline:
                now = time.monotonic()
                # Тот же гейт, что и в _handle_combat() в бою — без него
                # _act_combat_rotation() вызывалась бы каждый тик и
                # автоатака (когда ни одна цепочка не готова) лупила бы
                # заметно чаще положенных 0.9-1.1с.
                if now >= fake_bot._next_action_at:
                    fake_bot._act_combat_rotation(now)
                time.sleep(_TEST_TICK_S)

            input_manager.wait_idle()
            gamepad_debug_logger.debug(
                "########## РОТАЦИЯ-ТЕСТ ЗАВЕРШЁН ##########"
            )

            return {
                "ok": True,
                "chains_tested": len(chains),
                "duration_s": test_duration_s,
            }
        except Exception as e:
            logger.exception("test_rotation: ошибка тестового прогона.")
            return {"ok": False, "error": str(e)}


# Размер окна при первом запуске (логические пиксели), пока свой размер не запомнен.
# 2026-10-11: высота 400 -> 420 (+20) по просьбе пользователя.
_DEFAULT_WINDOW = (555, 420)
_MIN_WINDOW = (420, 260)          # меньше — интерфейс не помещается; защита от "схлопнутого" окна


def _window_size(state: dict) -> "tuple[int, int]":
    """Размер окна из ui_state.json (последний, что был у пользователя), иначе по умолчанию.
    Границы проверяем: файл могли испортить, или размер запомнили на большом мониторе, а запускают
    на маленьком — тогда ужимаем до 95% экрана, чтобы заголовок окна не уехал за край.
    Проверить без игры: положи в ui_state.json {"window_size": [5000, 10]} — окно откроется
    шириной почти во весь экран и не ниже минимальной высоты."""
    w, h = _DEFAULT_WINDOW
    saved = state.get("window_size")
    if isinstance(saved, (list, tuple)) and len(saved) == 2:
        try:
            w, h = int(saved[0]), int(saved[1])
        except (TypeError, ValueError):
            w, h = _DEFAULT_WINDOW
    try:
        u32 = ctypes.windll.user32
        # Процесс DPI-aware (_set_dpi_aware): экран в физических пикселях, окно в логических.
        # Логические <= физических, поэтому 95% физического экрана — безопасная верхняя граница.
        max_w, max_h = int(u32.GetSystemMetrics(0) * 0.95), int(u32.GetSystemMetrics(1) * 0.95)
    except Exception:                    # не Windows (тесты) — без верхней границы
        max_w = max_h = 10**5
    return min(max(w, _MIN_WINDOW[0]), max_w), min(max(h, _MIN_WINDOW[1]), max_h)


def main() -> None:
    logging.basicConfig(level=logging.INFO)

    # Самое первое, до вообще чего-либо ещё: пока не создан ни VisionManager
    # (mss), ни окно — Windows уже должна знать, что мы сами разбираемся с
    # масштабированием. Позже это дороже/бессмысленнее звать — DPI-режим
    # процесса фиксируется на всё время его жизни при первом же обращении
    # к GDI, повторный вызов среди рабочего цикла ничего не изменит.
    _set_dpi_aware()

    if not os.path.exists(_DIST_INDEX):
        print(
            f"Не найден {_DIST_INDEX} — сначала собери фронтенд командой "
            f"'npm run build' в папке репозитория интерфейса."
        )
        return

    # ДО открытия окна: свежий dist/ (после npm run build) не содержит
    # ранее сохранённых иконок — dist/icons/ туда ещё не попали, пока мы это
    # не сделаем сами. Порядок важен: если сделать это ПОСЛЕ create_window,
    # первый рендер уже успеет уйти в браузер без иконок и словит те же 404,
    # что мы только что чинили.
    _sync_icons_to_dist()

    api = Api()
    width, height = _window_size(api._load_ui_state())
    window = webview.create_window(
        "EasyFarm — Throne and Liberty",
        _DIST_INDEX,
        js_api=api,
        width=width,
        height=height,
    )
    # Окно запоминает свой размер: потянул мышкой за край — при следующем запуске такое же.
    # resized приходит десятки раз за одно перетаскивание, поэтому здесь только запоминаем число
    # в памяти (дёшево), а на диск пишем один раз — при закрытии окна (_on_closed ниже).
    # pywebview отдаёт размер в логических пикселях — тех же, что принимает create_window,
    # так что на экране с масштабом 125-150% окно не "растёт" от запуска к запуску.
    last_size = {"w": width, "h": height}

    def _on_resized(w, h):
        last_size["w"], last_size["h"] = int(w), int(h)

    window.events.resized += _on_resized

    # os._exit() — жёсткий выход из процесса в обход обычной Python-очистки
    # (atexit, сборщик мусора). Нужен именно он, а не полагаться на то, что
    # pywebview сам закроет процесс: внутри него крутятся потоки WebView2 и
    # встроенного HTTP-сервера (Bottle, раздаёт dist/), и на части машин они
    # не daemon-потоки — процесс python.exe тогда не умирает вместе с
    # закрытым окном, а виснет в фоне, держа занятым тот же профиль WebView2
    # и тот же порт. Именно так ловился баг с пустым окном при повторном
    # запуске (сначала думали на Ctrl+C в терминале, но окном крестиком
    # закрывали — та же дыра). Терять на жёстком выходе нечего: save_config/
    # save_icon уже пишут на диск сразу по действию пользователя, а не
    # "на закрытии" — дописывать в момент выхода нечего.
    def _on_closed():
        try:
            api._save_ui_state(window_size=[last_size["w"], last_size["h"]])
        finally:
            os._exit(0)            # try/finally: даже если запись упала, процесс всё равно закроется

    window.events.closed += _on_closed
    api._window = window          # таймер автоостановки шлёт в интерфейс событие "сработал"

    # private_mode=False пробовали — окно вообще переставало отрисовываться
    # и принимать клики, без единой ошибки в консоли (похоже на сбой
    # инициализации WebView2 при попытке создать постоянный профиль на этой
    # машине). Не нужен: слоты/цепочки/иконки уже переживают перезапуск
    # через файлы на диске (save_config/save_icon/load_config выше), а не
    # через профиль браузера.
    #
    # debug=False (обычный режим) — DevTools обычному пользователю не
    # нужен, а отладка через него была временной мерой на время поиска
    # багов (см. историю правок). Если понадобится снова покопаться в
    # консоли браузера — верни debug=True здесь на время диагностики.
    webview.start()


if __name__ == "__main__":
    main()
