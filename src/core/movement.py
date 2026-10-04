"""
src/core/movement.py — бот всегда в движении (2026-10-04): и в поиске, и в бою.

Откуда движения: tools/record_movement.py записал, как бегает игрок, а
tools/build_movement_model.py нарезал запись на ~1000 коротких "фраз" (0.5-3 с) в
src/config/movement_model.json. Контроллер складывает из них непрерывное движение
(motion matching): каждая фраза — настоящий кусок человеческого бега со всеми его
таймингами, а порядок, темп (+-10%) и зеркальность (A<->D) каждый раз новые. Запись
целиком не проигрывается: повторяющийся маршрут — классический признак бота.

Режимы (выбирает bot.py каждый тик, set_mode):
    off      — стоим: бот кликает по списку (Alt) — клавиш нет; жест камеры "на цель"
               ждёт, пока отпустят Alt (camera_nudge сам отказывает при видимом курсоре);
    approach — персонаж САМ бежит к цели после клика по списку: WASD не жмём (оборвали
               бы этот бег), но цель дальше 20 м -> держим Shift (бег в шифте, просьба
               2026-10-04), а камеру можно поворачивать (request_turn — "смотреть, куда бежим");
    select   — бот ведёт мышь к строке списка (Alt+ПКМ): только стрейфы A/D, "живее"
               (просьба 2026-10-04). Перед самим кликом InputManager отпускает их сам —
               иначе зажатая клавиша оборвала бы бег к цели, который запускает клик;
    search   — поиск между целями: только мелкие шаги и стрейфы (как в бою) — бот не
               убегает от места, где через миг кликнет следующую цель;
    combat — бой: только короткие фразы без прыжка/рывка, не уходим от моба
             (вперёд = к цели: камера в бою смотрит на неё);
    roam   — прогулка (SearchPhase.ROAM, список пуст): фразы с бегом вперёд, а КУДА
             бежать, решает navigation.py — поворачивает камеру жестами мыши из той же
             записи (request_turn) и вытаскивает из застревания (unstuck).

Где держим себя (search/combat): позицию считаем грубо, в "секундах бега" по клавишам
(W вперёд, S назад x0.7, A/D вбок, диагональ x0.707) — этого хватает, чтобы выбирать
фразы, возвращающие к центру. В roam точное место знает одометр миникарты (navigation.py).

Безопасность:
    - клавиши жмёт InputManager.movement_key: он не даёт нажать новую клавишу во время
      паники и пока бот держит Alt/Ctrl (клик по списку) — под тем же замком, что и Стоп;
    - во время долгого каста (movement_blocked() == "каст") всё отпускаем: движение
      сбило бы каст;
    - поток движения живёт отдельно от FSM: тик бота ничего не ждёт (60 FPS не страдают).

Как проверить без игры: python -m src.core.movement — 30 с "виртуального" движения
(20 с поиска + 10 с боя) на фальшивых часах: печатает, какие клавиши и сколько держал бы
бот, ничего не нажимая.
"""

from __future__ import annotations

import json
import logging
import math
import os
import random
import threading
import time
from collections import deque
from dataclasses import dataclass

input_debug_logger = logging.getLogger("input_debug")
logger = logging.getLogger(__name__)

MODEL_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "movement_model.json")

MODE_OFF, MODE_SEARCH, MODE_COMBAT, MODE_ROAM, MODE_APPROACH = "off", "search", "combat", "roam", "approach"
MODE_SELECT = "select"
# Какие клавиши фраз разрешены в режиме (нет в словаре — все из фразы).
_MODE_KEYS = {MODE_SELECT: frozenset({"a", "d"})}
_SPRINT_REACTION_S = (0.15, 0.35)   # "заметил, что далеко" -> зажал Shift
_MOVE_KEYS = frozenset({"w", "a", "s", "d"})
# Жест поворота: вертикаль руки берём на 30% — человеческая дрожь вверх-вниз остаётся, а
# камера за минуты прогулки не "уползает" в небо или в землю.
_TURN_DY_SCALE = 0.3
# Жест растягиваем по величине не больше чем в 1.8 раза (иначе скорость руки выйдет
# неестественной); поворот больше — несколькими жестами подряд с короткой паузой.
_TURN_MAX_SCALE = 1.8
_TURN_PAUSE_S = (0.06, 0.16)
_MIRROR = {"a": "d", "d": "a"}
_BACK_SPEED = 0.7               # S обычно медленнее W (так же считает build_movement_model)


@dataclass(frozen=True, slots=True)
class Phrase:
    """Фраза движения: steps — ((секунда от начала, зажатые клавиши), ...)."""
    id: int
    dur: float
    steps: "tuple[tuple[float, frozenset[str]], ...]"
    fwd: float                  # куда сдвигает: + вперёд / − назад (секунды бега)
    side: float                 # + вправо / − влево
    combat_ok: bool
    back: float = 0.0           # сколько секунд зажата S
    mirrored: bool = False

    def mirror(self) -> "Phrase":
        """A<->D: та же фраза в другую сторону — разнообразие x2 без новых записей."""
        steps = tuple((t, frozenset(_MIRROR.get(k, k) for k in st)) for t, st in self.steps)
        return Phrase(self.id, self.dur, steps, self.fwd, -self.side, self.combat_ok, self.back, not self.mirrored)


@dataclass(frozen=True, slots=True)
class Turn:
    """Жест поворота камеры из записи: pts — ((секунда, dx, dy), ...), dx — итог по горизонтали."""
    id: int
    dur: float
    dx: int
    pts: "tuple[tuple[float, int, int], ...]"


def load_phrases(path: str = MODEL_PATH) -> "list[Phrase] | None":
    """Модель из build_movement_model.py; None — файла нет (бот просто не двигается)."""
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    out = []
    for p in data.get("phrases", []):
        # Shift из фраз вырезаем (2026-10-04): рывок/бег в шифте посреди поиска уводил
        # персонажа "непонятно куда". Shift теперь только целевой — бег к далёкой цели.
        raw = [(ms / 1000.0, frozenset(k for k in name.split("+") if k and k != "shift")) for ms, name in p["steps"]]
        steps_l: "list[tuple[float, frozenset[str]]]" = []
        for t, st in raw:
            if not steps_l or steps_l[-1][1] != st:      # соседние одинаковые состояния — одно
                steps_l.append((t, st))
        steps = tuple(steps_l)
        out.append(Phrase(p["id"], float(p["dur"]), steps, float(p["fwd"]), float(p["side"]),
                          bool(p["combat_ok"]), float(p.get("back", 0.0))))
    return out or None


def load_turns(path: str = MODEL_PATH) -> "list[Turn]":
    """Жесты поворота (модель старше ROAM их не содержит — тогда пусто, поворотов не будет)."""
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return [Turn(t["id"], float(t["dur"]), int(t["dx"]), tuple((ms / 1000.0, dx, dy) for ms, dx, dy in t["pts"]))
            for t in data.get("turns", []) if t.get("dx")]


def load_hold_stats(path: str = MODEL_PATH) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f).get("hold_stats", {})


@dataclass
class MoveTuning:
    # Поводок в поиске: 4 с бега, забываем за ~10 с. Подобрано по записи: с этими числами
    # удержание W у бота ~0.75 с, как у игрока (0.77); тесный поводок (2.5 с, 40 с) обрезал
    # бег вперёд до 0.5 с — бот "семенил". Короткая память честна: в поиске камера всё
    # время поворачивает, и "вперёд" минуту назад — уже другое направление.
    search_radius_s: float = 4.0
    combat_radius_s: float = 0.9     # в бою — держимся рядом с мобом
    combat_min_fwd_s: float = -0.4   # и не пятимся от него дальше этого
    search_decay_tau_s: float = 10.0
    combat_decay_tau_s: float = 8.0   # в бою камера всё время смотрит на моба: центр = он, забываем быстро
    tempo: "tuple[float, float]" = (0.9, 1.1)   # темп фразы: +-10%
    mirror_p: float = 0.5
    recent_n: int = 20               # столько последних фраз не повторяем
    candidates: int = 40             # из скольких случайных фраз выбираем лучшую (быстро и не предсказуемо)
    noise: float = 0.6               # случайная добавка к "цене" фразы: не всегда самая выгодная
    match_cost: float = 0.8          # за каждую клавишу, которой начало фразы не совпадает с тем, что
                                     # зажато сейчас (суть motion matching: следующий кусок начинается
                                     # с той же "позы" — W, зажатая на стыке, не обрывается)
    idle_poll_s: float = 0.05


class MovementController:
    """
    Проигрыватель фраз. Логика — в step(now) (чистая, гоняется в тестах с фальшивыми
    часами), поток только зовёт step() и спит до следующей смены клавиш.
    """

    def __init__(self, input_manager, phrases: "list[Phrase]", tuning: "MoveTuning | None" = None,
                 rng: "random.Random | None" = None, clock=time.monotonic,
                 turns: "list[Turn] | None" = None, hold_stats: "dict | None" = None) -> None:
        self._im = input_manager
        self._all = phrases
        self._turns = [t for t in (turns or []) if abs(t.dx) >= 1]
        self._hold_stats = hold_stats or {}
        self._combat = [p for p in phrases if p.combat_ok] or phrases
        self.t = tuning or MoveTuning()
        self._rng = rng or random.Random()
        self._clock = clock
        self._mode = MODE_OFF
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: "threading.Thread | None" = None
        self._pressed: "set[str]" = set()       # что зажали МЫ (и что реально зажато)
        self._cur: "Phrase | None" = None
        self._cur_mode = MODE_OFF
        self._cur_t0 = 0.0
        self._cur_tempo = 1.0
        self._recent: "deque[int]" = deque(maxlen=self.t.recent_n)
        self.pos_fwd = 0.0
        self.pos_side = 0.0
        self._last_t: "float | None" = None
        self._pos_mode = MODE_OFF
        self.blocked: "str | None" = None
        self.phrases_played = 0
        # ROAM: запрошенный поворот (счётчики мыши, + вправо), играемый жест и остаток.
        self._turn_req: "float | None" = None
        self._turn: "dict | None" = None
        self._turn_left = 0.0
        self._turn_next_at = 0.0
        self.turns_played = 0
        # ROAM: манёвр "выбраться" (подменяет текущую фразу один раз).
        self._override: "Phrase | None" = None
        # Сдвиг в теле персонажа БЕЗ забывания (секунды бега) — navigation.py по нему отличает
        # "бежал вперёд" от "стрейфил вправо", когда считает, куда смотрит камера.
        self.body_fwd = 0.0
        self.body_side = 0.0
        self._body_hist: "deque[tuple[float, float, float]]" = deque()
        self._hist_lock = threading.Lock()
        # С какого момента без перерыва зажата хоть одна из WASD (None — не зажата). Читает
        # поток FSM для "застрял": float присваивается атомарно, лок не нужен.
        self.moving_since: "float | None" = None
        # Бег в шифте к далёкой цели (режим approach): хотим ли и с какого момента жать.
        self._sprint = False
        self._sprint_at: "float | None" = None

    # --- управление из FSM ---
    def set_mode(self, mode: str) -> None:
        """Зовётся каждый тик FSM: дёшево, если режим тот же."""
        if mode != self._mode:
            self._mode = mode
            self._wake.set()                # поток проснётся сразу: off -> отпустить без задержки

    @property
    def mode(self) -> str:
        return self._mode

    def set_sprint(self, on: bool) -> None:
        """Режим approach: держать Shift (цель далеко). Зовётся каждый тик FSM — дёшево."""
        if on != self._sprint:
            self._sprint = on
            self._sprint_at = None
            self._wake.set()

    def cancel_turn(self) -> None:
        """Цель нашлась в кадре — дальше её ведёт поток камеры, жест доигрывать не нужно."""
        self._drop_turn()

    def request_turn(self, counts: float) -> bool:
        """
        ROAM: повернуть камеру на counts счётчиков мыши (+ вправо). Играется жестами из
        записи параллельно с бегом. Новый запрос заменяет недоигранный остаток.
        """
        if not self._turns or not hasattr(type(self._im), "camera_nudge") or abs(counts) < 1:
            return False
        self._turn_req = float(counts)
        self._wake.set()
        return True

    @property
    def turn_active(self) -> bool:
        return self._turn is not None or self._turn_req is not None or abs(self._turn_left) >= 1

    def unstuck(self) -> None:
        """ROAM, застряли: назад, вбок с прыжком — тайминги из статистики записи игрока."""
        def hold(key: str, lo: float, hi: float) -> float:
            st = self._hold_stats.get(key)
            a, b = (st["p10"], st["p90"]) if st else (lo, hi)
            return self._rng.uniform(max(lo, a), max(lo + 0.05, b))
        side = self._rng.choice("ad")
        t_back = hold("s", 0.3, 0.8)
        t_side = hold(side, 0.5, 1.2)
        t_jump = t_back + self._rng.uniform(0.12, 0.3)
        jump = hold("space", 0.12, 0.25)
        steps = ((0.0, frozenset({"s"})), (t_back, frozenset({side})),
                 (t_jump, frozenset({side, "space"})), (t_jump + jump, frozenset({side})),
                 (t_back + t_side, frozenset()))
        self._override = Phrase(-1, t_back + t_side + 0.05, steps, 0.0, 0.0, False)
        self._wake.set()

    def body_delta(self, window_s: float, now: float) -> "tuple[float, float] | None":
        """Сдвиг в теле (вперёд, вбок) за последние window_s секунд; None — мало истории."""
        with self._hist_lock:
            hist = list(self._body_hist)
        if not hist:
            return None
        _, f_now, s_now = hist[-1]
        for t, f, s in hist:
            if now - t <= window_s:
                return f_now - f, s_now - s
        return None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="MoveWorker")
        self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        self._release_all()

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                now = self._clock()
                try:
                    nxt = self.step(now)
                except Exception as e:                # движение не должно ронять бота
                    logger.error("Движение: ошибка %s — отпускаю клавиши", e)
                    self._abandon()
                    nxt = now + 0.2
                self._wake.wait(max(0.0, min(nxt - self._clock(), self.t.idle_poll_s)))
                self._wake.clear()
        finally:
            self._release_all()                       # что бы ни случилось — клавиши не залипнут

    # --- один шаг ---
    def step(self, now: float) -> float:
        """Привести клавиши к нужному состоянию на момент now. Возвращает, когда звать снова."""
        mode = self._mode
        self._integrate(now, mode)
        if mode == MODE_OFF:
            # Ноги стоят (клик по списку), а камера может доигрывать жест "на цель": пока
            # зажат Alt, camera_nudge откажет, и жест просто подождёт конца клика.
            self._abandon()
            return min(self._step_turn(now), now + self.t.idle_poll_s)
        blocked = self._im.movement_blocked() if hasattr(type(self._im), "movement_blocked") else None
        if blocked:
            if self.blocked != blocked:
                input_debug_logger.debug("MOVE    пауза: %s — клавиши движения отпущены", blocked)
            self.blocked = blocked
            self._abandon()
            self._drop_turn()
            return now + 0.03
        self.blocked = None
        if mode == MODE_APPROACH:
            # Персонаж бежит сам: из клавиш — только Shift (если цель далеко), камера — жестами.
            self._cur = None
            self._apply(frozenset({"shift"}) if self._sprint_now(now) else frozenset())
            return min(self._step_turn(now), now + self.t.idle_poll_s)
        if self._override is not None:
            ph, self._override = self._override, None
            self._begin(ph, mode, now, remember=False)
            input_debug_logger.debug("MOVE    выбираюсь: назад %.1f с, вбок с прыжком", ph.steps[1][0])
        # Фраза кончилась — или сменился режим (бой начался посреди фразы поиска: берём боевую).
        if self._cur is not None and (self._cur_mode != mode or now >= self._phrase_end()):
            self._cur = None
        if self._cur is None:
            self._begin(self._choose(mode), mode, now)
        k, next_t = self._state_at(now)
        allowed = _MODE_KEYS.get(mode)
        if allowed is not None:
            k = k & allowed                       # select: из фразы берём только стрейфы
        self._apply(k)
        turn_next = self._step_turn(now)
        return min(next_t, turn_next, now + self.t.idle_poll_s)

    def _sprint_now(self, now: float) -> bool:
        if not self._sprint:
            return False
        if self._sprint_at is None:
            self._sprint_at = now + self._rng.uniform(*_SPRINT_REACTION_S)
        return now >= self._sprint_at

    # --- выбор фразы ---
    def _choose(self, mode: str) -> Phrase:
        # Поиск между целями — те же мелкие боевые шаги (2026-10-04): раньше бот бегал
        # длинными фразами и убегал от места, где через долю секунды кликал новую цель.
        small = mode in (MODE_COMBAT, MODE_SEARCH, MODE_SELECT)
        pool = self._combat if small else self._all
        radius = self.t.combat_radius_s if small else self.t.search_radius_s
        best, best_cost = None, float("inf")
        for _ in range(min(self.t.candidates, len(pool))):
            ph = self._rng.choice(pool)
            if self._rng.random() < self.t.mirror_p:
                ph = ph.mirror()
            if mode == MODE_ROAM:
                # Прогулка: бежим ВПЕРЁД (куда — решает камера), стрейфы и шаги назад — редко.
                # Поводка здесь нет: место знает одометр миникарты, а не счёт клавиш.
                cost = (-1.5 * ph.fwd + 0.8 * abs(ph.side) + 2.0 * ph.back) / max(ph.dur, 0.3)
            else:
                f, s = self.pos_fwd + ph.fwd, self.pos_side + ph.side
                r = math.hypot(f, s)
                cost = 10.0 * max(0.0, r - radius) ** 2           # выходит за поводок — дорого
                if small and f < self.t.combat_min_fwd_s:
                    cost += 10.0 * (self.t.combat_min_fwd_s - f) ** 2   # пятится от моба — дорого
            cost += self.t.match_cost * len(ph.steps[0][1] ^ self._pressed)
            if ph.id in self._recent:
                cost += 3.0                                    # недавно было — не повторяем
            cost += self._rng.random() * self.t.noise
            if cost < best_cost:
                best, best_cost = ph, cost
        return best

    def _begin(self, ph: Phrase, mode: str, now: float, remember: bool = True) -> None:
        self._cur, self._cur_mode, self._cur_t0 = ph, mode, now
        self._cur_tempo = self._rng.uniform(*self.t.tempo) if remember else 1.0
        if not remember:
            return
        self._recent.append(ph.id)
        self.phrases_played += 1
        input_debug_logger.debug("MOVE    %s фраза #%d%s %.1f с | позиция вперёд %+.1f вбок %+.1f",
                                 {MODE_COMBAT: "бой", MODE_ROAM: "прогулка"}.get(mode, "поиск"), ph.id,
                                 " (зерк.)" if ph.mirrored else "", ph.dur * self._cur_tempo,
                                 self.pos_fwd, self.pos_side)

    def _phrase_end(self) -> float:
        return self._cur_t0 + self._cur.dur * self._cur_tempo

    def _state_at(self, now: float) -> "tuple[frozenset[str], float]":
        """Какие клавиши должны быть зажаты сейчас и когда следующая смена."""
        rel = (now - self._cur_t0) / self._cur_tempo
        steps = self._cur.steps
        cur = steps[0][1]
        nxt = self._cur.dur
        for t, st in steps:              # фраза — 5-15 шагов, линейный проход дешевле бисекции на таком размере
            if t <= rel:
                cur = st
            else:
                nxt = t
                break
        return cur, self._cur_t0 + nxt * self._cur_tempo

    # --- клавиши ---
    def _apply(self, want: "frozenset[str]") -> None:
        """
        Разница "что зажато" -> "что нужно": отпускаем лишнее, жмём недостающее. По
        разнице, а не "отпустить всё и нажать заново": на стыке фраз, где W зажата и
        там и там, она так и остаётся зажатой — без дребезга, который выдал бы бота.
        """
        for k in self._pressed - want:
            self._im.movement_key(k, False)
            self._pressed.discard(k)
        for k in want - self._pressed:
            if self._im.movement_key(k, True):   # отказ (держим Alt) — повторим на следующем шаге
                self._pressed.add(k)

    def _abandon(self) -> None:
        self._cur = None
        self._release_all()

    # --- поворот камеры жестом (ROAM) ---
    def _drop_turn(self) -> None:
        self._turn = None
        self._turn_req = None
        self._turn_left = 0.0

    def _pick_turn(self, counts: float, now: float) -> None:
        """Жест похожей величины (из 5 ближайших — случайный), растянутый до нужной."""
        target = abs(counts)
        near = sorted(self._turns, key=lambda g: abs(math.log(abs(g.dx) / target)))[:5]
        g = self._rng.choice(near)
        scale = counts / g.dx                           # знак: жест влево играем вправо и наоборот
        if abs(scale) > _TURN_MAX_SCALE:
            scale = math.copysign(_TURN_MAX_SCALE, scale)
        self._turn_left = counts - g.dx * scale         # не влезло в один жест — следующим
        self._turn = {"g": g, "scale": scale, "t0": now, "i": 0, "cx": 0.0, "cy": 0.0}
        self.turns_played += 1
        input_debug_logger.debug("MOVE    поворот камеры: жест #%d x%.2f (%+.0f из %+.0f счётчиков)",
                                 g.id, scale, g.dx * scale, counts)

    def _step_turn(self, now: float) -> float:
        """Доиграть жест до момента now. Возвращает время следующей точки жеста."""
        if self._turn_req is not None:
            self._pick_turn(self._turn_req, now)
            self._turn_req = None
        if self._turn is None:
            if abs(self._turn_left) >= 1 and now >= self._turn_next_at:
                self._pick_turn(self._turn_left, now)
            else:
                return self._turn_next_at if abs(self._turn_left) >= 1 else now + self.t.idle_poll_s
        tr = self._turn
        pts = tr["g"].pts
        rel = now - tr["t0"]
        while tr["i"] < len(pts) and pts[tr["i"]][0] <= rel:
            _, dx, dy = pts[tr["i"]]
            tr["cx"] += dx * tr["scale"]
            tr["cy"] += dy * _TURN_DY_SCALE
            tr["i"] += 1
        ix, iy = int(tr["cx"]), int(tr["cy"])          # дробный остаток копим: сумма жеста сходится точно
        if (ix or iy) and self._im.camera_nudge(ix, iy):
            tr["cx"] -= ix
            tr["cy"] -= iy
        if tr["i"] >= len(pts) and abs(tr["cx"]) < 1 and abs(tr["cy"]) < 1:
            self._turn = None
            self._turn_next_at = now + self._rng.uniform(*_TURN_PAUSE_S)
            return self._turn_next_at
        return now + 0.01 if tr["i"] >= len(pts) else tr["t0"] + pts[tr["i"]][0]

    def _release_all(self) -> None:
        for k in list(self._pressed):
            try:
                self._im.movement_key(k, False)
            except Exception as e:
                logger.error("Движение: не отпустилась %s: %s", k, e)
        self._pressed.clear()

    # --- оценка позиции ---
    def _integrate(self, now: float, mode: str) -> None:
        """Сдвиг за прошедшее время по РЕАЛЬНО зажатым клавишам + медленное забывание."""
        if mode != self._pos_mode:
            # Новый режим — новый центр: поиск начался с нового места, бой — у нового моба.
            self.pos_fwd = self.pos_side = 0.0
            self._pos_mode = mode
        if self._last_t is not None and now > self._last_t:
            dt = now - self._last_t
            p = self._pressed
            f = (1.0 if "w" in p else 0.0) - (_BACK_SPEED if "s" in p else 0.0)
            s = (1.0 if "d" in p else 0.0) - (1.0 if "a" in p else 0.0)
            norm = 0.707 if f and s else 1.0
            self.pos_fwd += f * norm * dt
            self.pos_side += s * norm * dt
            self.body_fwd += f * norm * dt
            self.body_side += s * norm * dt
            tau = self.t.combat_decay_tau_s if mode == MODE_COMBAT else self.t.search_decay_tau_s
            k = math.exp(-dt / tau)
            self.pos_fwd *= k
            self.pos_side *= k
        self._last_t = now
        with self._hist_lock:
            if not self._body_hist or now - self._body_hist[-1][0] >= 0.05:
                self._body_hist.append((now, self.body_fwd, self.body_side))
                while self._body_hist and now - self._body_hist[0][0] > 5.0:
                    self._body_hist.popleft()
        if self._pressed & _MOVE_KEYS:
            if self.moving_since is None:
                self.moving_since = now
        else:
            self.moving_since = None


if __name__ == "__main__":
    # Песочница: фальшивый ввод и фальшивые часы — ничего не нажимается.
    class _PrintIM:
        def __init__(self):
            self.t = 0.0
            self.log: "list[tuple[float, str, bool]]" = []

        def movement_key(self, k, down):
            self.log.append((self.t, k, down))
            return True

        def movement_blocked(self):
            return None

    phrases = load_phrases()
    if phrases is None:
        raise SystemExit("Нет модели: сначала python tools/build_movement_model.py")
    im = _PrintIM()
    mc = MovementController(im, phrases, rng=random.Random(1))
    for mode, seconds in ((MODE_SEARCH, 20.0), (MODE_COMBAT, 10.0)):
        mc.set_mode(mode)
        end = im.t + seconds
        while im.t < end:
            mc.step(im.t)
            im.t += 0.005
    mc.set_mode(MODE_OFF)
    mc.step(im.t)
    downs: "dict[str, float]" = {}
    holds: "dict[str, list[float]]" = {}
    for t, k, d in im.log:
        if d:
            downs[k] = t
        elif k in downs:
            holds.setdefault(k, []).append(t - downs.pop(k))
    print("Фраз сыграно: %d" % mc.phrases_played)
    for k, v in sorted(holds.items()):
        v.sort()
        print("  %-5s нажатий %3d, держал медиана %.2f с, от %.2f до %.2f с" % (k, len(v), v[len(v) // 2], v[0], v[-1]))
    print("После 'off' зажато: %s" % (sorted(mc._pressed) or "ничего"))
