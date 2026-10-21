"""
src/core/vision_check.py — проверка зрения при старте (2026-10-12).

Зачем: на чужом мониторе (HDR, "Ночной свет", цифровая интенсивность NVIDIA, другой монитор)
бот может не видеть список целей, миникарту или панель HP — и молча стоять/бегать вслепую.
Раньше это выяснялось только по логам. Теперь в первые секунды после "Старт" бот сам
проверяет три вещи и, если что-то не видно, пишет в лог понятную причину + что поправить,
сохраняет скриншот экрана (vision_check\\*.png рядом с программой) и отдаёт подсказку
в интерфейс (get_status -> vision_warnings).

Что проверяем и когда:
  - список целей  — за первые START_WINDOW_S секунд АКТИВНОЙ игры (окно игры впереди);
  - миникарта     — то же окно;
  - панель цели   — нельзя проверить на старте (её нет, пока цель не выбрана), поэтому
                    "лениво": бот выбрал цель из списка PANEL_MAX_CLICKS раз подряд, а панель
                    с HP ни разу не появилась -> провал.

Класс — чистая логика без экрана и логгера (решает "ок/провал/ещё ждём"). Логи, скриншот
и интерфейс — в FarmBot._vision_check_tick. Так логику можно проверить без игры.

Песочница: python tools\\vision_check_test.py — подаёт искусственное время и флаги
("список виден / не виден", "клик без панели") и проверяет вердикты.
"""
from __future__ import annotations

from dataclasses import dataclass, field

LIST, MAP, PANEL = "list", "map", "panel"

TITLES = {LIST: "список целей", MAP: "миникарта", PANEL: "панель цели (HP моба)"}

# Подсказки — для человека без логов: что именно поправить в Windows/игре.
HINTS = {
    LIST: ("Список целей не найден. Проверь: список целей открыт (вкладка «Персонаж»), "
           "«Ночной свет» Windows выключен, цифровая интенсивность NVIDIA / Vibrance AMD — "
           "по умолчанию, игра на основном мониторе."),
    MAP: ("Миникарта не читается. Проверь: миникарта включена и не свёрнута, поверх её угла "
          "нет других окон, игра на основном мониторе."),
    PANEL: ("Цель выбирается, но панель с HP моба не видна. Проверь: HDR выключен (или "
            "«Яркость SDR-содержимого» ниже), яркость в игре по умолчанию, «Ночной свет» "
            "выключен, цифровая интенсивность по умолчанию."),
}


@dataclass
class VisionCheck:
    """
    Вердикт по каждой проверке: None — ещё ждём, True — видно, False — провал.
    dataclass: поля с типами и значениями по умолчанию без ручного __init__ — короче и
    сразу понятно, что хранит объект (идиома Python 3.7+).
    """
    START_WINDOW_S: float = 6.0       # список и миникарту ждём столько секунд активной игры
    MIN_LIST_READS: int = 10          # ...и не меньше стольких чтений списка (бот мог сразу уйти в бой)
    PANEL_MAX_CLICKS: int = 3         # выборов цели подряд без панели -> провал панели
    MAX_TICK_GAP_S: float = 0.25      # пауза между тиками больше — игра была свёрнута, не считаем

    status: dict = field(default_factory=lambda: {LIST: None, MAP: None, PANEL: None})
    active_s: float = 0.0
    _last: "float | None" = None
    _clicks_no_panel: int = 0

    def skip(self, key: str) -> None:
        """Проверка не нужна (функция выключена): считаем пройденной, в отчёт не идёт."""
        if self.status[key] is None:
            self.status[key] = True

    def tick(self, now: float, list_seen: bool, list_reads: int, map_ok: bool) -> "list[tuple[str, str]]":
        """
        Раз в тик бота. Возвращает события [("fail"|"fixed", ключ)] — обычно пусто.
        "fixed": после провала стало видно (игрок поправил яркость, не перезапуская бота).
        Время считаем только активное: свернул игру на минуту — окно проверки не сгорает.
        """
        if self._last is not None:
            self.active_s += min(now - self._last, self.MAX_TICK_GAP_S)
        self._last = now
        events = []
        # Таблица (ключ, видно сейчас, можно ли уже объявить провал) — один цикл вместо двух
        # копий if-ов: добавить проверку = добавить строку.
        for key, seen, may_fail in ((LIST, list_seen, list_reads >= self.MIN_LIST_READS),
                                    (MAP, map_ok, True)):
            st = self.status[key]
            if st is True:
                continue
            if seen:
                self.status[key] = True
                if st is False:
                    events.append(("fixed", key))
            elif st is None and may_fail and self.active_s >= self.START_WINDOW_S:
                self.status[key] = False
                events.append(("fail", key))
        return events

    def on_list_click(self) -> "list[str]":
        """Бот выбрал цель из списка (F+E). Панели всё нет после N выборов — провал."""
        if self.status[PANEL] is not None:
            return []
        self._clicks_no_panel += 1
        if self._clicks_no_panel >= self.PANEL_MAX_CLICKS:
            self.status[PANEL] = False
            return [PANEL]
        return []

    def on_panel(self) -> bool:
        """Панель цели нашлась (вход в бой). True — это первое подтверждение (или исправление
        после провала: игрок поправил яркость, не перезапуская бота)."""
        was = self.status[PANEL]
        self.status[PANEL] = True
        self._clicks_no_panel = 0
        return was is not True

    @property
    def all_ok(self) -> bool:
        """Всё видно — дальше тик проверки ничего не делает (ноль затрат в бою). Пока есть
        провал — тикаем дальше (это пара сравнений флагов): ловим момент, когда поправили."""
        return all(v is True for v in self.status.values())

    def warnings(self) -> "list[str]":
        """Подсказки по текущим провалам — для интерфейса."""
        return [HINTS[k] for k, v in self.status.items() if v is False]

    def summary(self) -> str:
        mark = {None: "ждёт", True: "OK", False: "НЕ ВИДНО"}
        return ", ".join("%s: %s" % (TITLES[k], mark[v]) for k, v in self.status.items())
