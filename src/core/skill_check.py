"""
Проверка "скилл реально нажался" по откату на панели скиллов (2026-10-06).

Чистая логика, без экрана и клавиатуры: на вход — нажатия (клавиша, время) от InputManager
и кадры слотов (время, {клавиша: (яркость, белых px)}) от SkillBarWatcher; на выход — события.
Поэтому весь модуль тестируется без игры (skill_check_test.py: фейковые кадры и нажатия).

Правило: нажатие подтверждено, если в окне WINDOW_S после него слот ТЕМНЕЕТ (яркость упала
до DROP_RATIO от яркости перед нажатием) или на нём появились белые цифры отката (+WHITE_GAIN
белых px). Сравниваем слот сам с собой: у каждой иконки своя яркость (от 90 до 175 на скрине),
общий порог для всех не работает.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

DROP_RATIO = 0.80            # яркость слота после нажатия <= 80% от яркости до — откат пошёл
WHITE_GAIN = 25              # или белых пикселей (цифры "5 сек.") стало на столько больше
WINDOW_S = (0.10, 1.0)       # где ищем откат после нажатия: раньше — ещё кадр до нажатия,
                             # позже 1 с — уже не "наше" нажатие (каст длиннее — скилл всё равно занят)
PRE_MAX_AGE_S = 0.35         # кадр "до нажатия" не старше этого, иначе сравнивать не с чем
ON_CD_RATIO = 0.80           # до нажатия слот уже темнее 80% своей обычной яркости -> был на откате
REF_DECAY_PER_S = 0.002      # "обычная" яркость медленно забывается вниз (~0.2%/с)
# ...и медленно растёт вверх: постоянная времени REF_RISE_TAU_S. Лог 2026-10-06 03:28: при нажатии
# иконка ВСПЫХИВАЕТ на ~0.3 с (слот 3: 190 -> 221), и "максимум" запоминал вспышку как обычную
# яркость (слот 2: 239 при настоящих 132) — готовый слот считался "на откате" (57 ложных из 68).
# С tau 2 с вспышка 0.3 с поднимает оценку лишь на ~14% своей высоты, а готовый слот за пару
# секунд выводит её на свой уровень.
REF_RISE_TAU_S = 2.0
# Скачок ярче эталона больше чем в FLASH_RATIO раз — это вспышка нажатия, её в эталон не берём,
# пока она не продержится FLASH_MAX_S (тогда это не вспышка: слот вышел из отката / первый кадр был тёмным).
FLASH_RATIO = 1.2
FLASH_MAX_S = 1.0
LEARN_PRESSES = 4            # столько нажатий подряд без отката -> у скилла нет видимого отката, не проверяем

# --- готовность по СНИМКУ готового слота (2026-10-10, tools/capture_ready_slots.py) ---
# Замер на скринах игрока: готовый слот против своего снимка — ratio 0.99-1.01, ncc >= 0.96;
# слот на откате — ratio 0.47-0.52, ncc 0.55; другие иконки панели против чужого снимка — ncc 0.2 (макс 0.63).
SNAP_READY_RATIO = 0.85      # темнее 85% снимка -> откат (запас от 1.0 вниз на мерцание фона/тень курсора)
SNAP_SAME_NCC = 0.80         # похож на снимок не меньше этого -> та же иконка без цифр отката -> готов
SNAP_FOREIGN_NCC = 0.45      # яркий, но похож меньше этого -> на слоте другая иконка (скилл сменили)
SNAP_FOREIGN_S = 2.0         # "другая иконка" столько подряд -> снимок слота устарел, на сессию — по яркости
SNAP_STUCK_S = 90.0          # бот спрашивал слот, а снимок 90 с подряд говорит "откат" -> снимок не тот
SNAP_MAX_AGE_S = 0.5         # сравнение старше этого (поток панели отстал) — не верим


def snapshot_verdict(ncc: float, ratio: float) -> "bool | None":
    """
    Готов ли слот по сравнению со снимком. True — готов, False — откат, None — на слоте не та
    иконка (снимок устарел). Порядок проверок важен: early return по яркости первым — ncc не видит
    равномерного затемнения, поэтому "тёмный" решает всё, что бы ни говорила корреляция.
    """
    if ratio < SNAP_READY_RATIO:
        return False
    if ncc >= SNAP_SAME_NCC:
        return True
    if ncc < SNAP_FOREIGN_NCC:
        return None
    return False                 # яркий, но картинка испорчена (цифры отката / рамка) — пока не готов


@dataclass
class _Press:
    key: str
    t: float
    is_retry: bool
    pre: "tuple[float, int] | None" = None
    cont: bool = False                  # следующий удар того же скилла (нажат < 2 с после прошлого)


@dataclass
class _KeyStats:
    misses_in_row: int = 0
    unverifiable: bool = False


@dataclass
class Event:
    """kind: 'ok' | 'fail' | 'was_cd' | 'no_cd_visible'. retry — можно ли нажать ещё раз."""
    kind: str
    key: str
    t_press: float
    retry: bool = False
    info: str = ""


@dataclass
class SkillVerifier:
    _pending: "list[_Press]" = field(default_factory=list)
    _hist: "deque[tuple[float, dict]]" = field(default_factory=lambda: deque(maxlen=40))
    _ref: "dict[str, float]" = field(default_factory=dict)
    _stats: "dict[str, _KeyStats]" = field(default_factory=dict)
    _retry_next: "set[str]" = field(default_factory=set)
    _last_t: "float | None" = None
    _bright_since: "dict[str, float]" = field(default_factory=dict)
    # Слоты, чью "готовую" яркость мы видели по сработавшему нажатию — только им верим в ready().
    _learned: "set[str]" = field(default_factory=set)
    _last_press_t: "dict[str, float]" = field(default_factory=dict)
    _ok_v0: "dict[str, list]" = field(default_factory=dict)   # яркость "готов" по последним нажатиям

    def expect_retry(self, key: str) -> None:
        """Следующее нажатие key — наш повтор: его проверяем, но второй раз не повторяем."""
        self._retry_next.add(key)

    def add_press(self, key: str, t: float) -> None:
        is_retry = key in self._retry_next
        self._retry_next.discard(key)
        last = self._last_press_t.get(key)
        self._last_press_t[key] = t
        self._pending.append(_Press(key, t, is_retry, cont=last is not None and t - last < 2.0))

    def add_sample(self, t: float, slots: dict) -> None:
        # "Обычная" яркость слота: максимум, который медленно забывается. Early return для
        # первого кадра — dt не с чем считать.
        dt = 0.0 if self._last_t is None else max(0.0, t - self._last_t)
        self._last_t = t
        k = 1.0 - REF_DECAY_PER_S * dt
        a = min(1.0, dt / REF_RISE_TAU_S)        # доля пути к новому значению за этот кадр (EMA)
        for key, (v, _w) in slots.items():
            r = self._ref.get(key)
            if r is None:
                self._ref[key] = v
            elif v > r * FLASH_RATIO and t - self._bright_since.setdefault(key, t) < FLASH_MAX_S:
                pass                              # короткая вспышка нажатия — не эталон
            elif v > r:
                if v <= r * FLASH_RATIO:
                    self._bright_since.pop(key, None)   # обычный рост, не вспышка — счётчик вспышки сброшен
                self._ref[key] = r + (v - r) * a  # вверх — медленно (EMA)
            else:
                self._bright_since.pop(key, None)
                self._ref[key] = r * k            # вниз — ещё медленнее: откат (тёмный) не "эталон"
        self._hist.append((t, slots))

    def _pre_sample(self, p: _Press) -> "tuple[float, int] | None":
        # Последний кадр не позже нажатия. reversed — с конца: нужный кадр почти всегда последний.
        for ts, slots in reversed(self._hist):
            if ts <= p.t:
                return slots.get(p.key) if p.t - ts <= PRE_MAX_AGE_S else None
        return None

    def update(self, now: float) -> "list[Event]":
        """Разобрать ожидающие нажатия. Звать каждый тик бота (дёшево: обычно 0-2 нажатия)."""
        events: "list[Event]" = []
        keep: "list[_Press]" = []
        for p in self._pending:
            if p.pre is None:
                p.pre = self._pre_sample(p)
            ev = self._judge(p, now)
            if ev is None:
                keep.append(p)
            elif ev.kind:                       # kind "" — нажатие без данных: молча выбрасываем
                events.append(ev)
        self._pending = keep
        return events

    def _judge(self, p: _Press, now: float) -> "Event | None":
        lo, hi = p.t + WINDOW_S[0], p.t + WINDOW_S[1]
        if p.pre is None:
            # Кадра до нажатия нет (панель ещё не найдена / поток зрения отстал): не судим.
            return Event("", p.key, p.t) if now > hi else None
        v0, w0 = p.pre
        ref = self._ref.get(p.key, v0)
        st = self._stats.setdefault(p.key, _KeyStats())
        if v0 <= ref * ON_CD_RATIO:
            return Event("was_cd", p.key, p.t, info="до нажатия слот уже на откате (%.0f из %.0f)" % (v0, ref))
        for ts, slots in self._hist:
            if lo <= ts <= hi and p.key in slots:
                v, w = slots[p.key]
                if v <= v0 * DROP_RATIO or w >= w0 + WHITE_GAIN:
                    st.misses_in_row = 0
                    st.unverifiable = False
                    # Нажатие сработало -> кадр ДО него был "скилл готов": это и есть обычная яркость
                    # слота. Без этого (лог 2026-10-10 00:44) эталон выучивался по первому кадру, когда
                    # слот был НА ОТКАТЕ (тёмный, 80 вместо 149), и тёмный слот считался готовым.
                    # Эталон "готов" = МЕДИАНА яркости до последних сработавших нажатий, а не максимум
                    # (лог 2026-10-10 02:33: у "2" второй удар нажат на вспышке первого — 194 вместо
                    # 132, максимум запомнил 194, и готовый слот (132) потом считался "на откате":
                    # "2" больше не жалась). Удары подряд того же скилла (cont) не учим — там вспышка.
                    if not p.cont:
                        hist = self._ok_v0.setdefault(p.key, [])
                        hist.append(v0)
                        del hist[:-7]
                        self._ref[p.key] = sorted(hist)[len(hist) // 2]
                        self._learned.add(p.key)
                    return Event("ok", p.key, p.t, info="откат через %.2f с (яркость %.0f -> %.0f, белых %d -> %d)"
                                 % (ts - p.t, v0, v, w0, w))
        if now <= hi:
            return None                          # окно ещё открыто — ждём
        st.misses_in_row += 1
        if st.unverifiable:
            return Event("", p.key, p.t)
        if st.misses_in_row >= LEARN_PRESSES:
            st.unverifiable = True
            return Event("no_cd_visible", p.key, p.t,
                         info="%d нажатий подряд без видимого отката — у скилла нет отката на панели, не проверяю"
                         % st.misses_in_row)
        return Event("fail", p.key, p.t, retry=not p.is_retry,
                     info="за %.1f с отката нет (яркость до %.0f)" % (WINDOW_S[1], v0))


class PressCheckLoop:
    """
    Проводка между потоками: забирает нажатия из InputManager.skill_presses и кадры из
    SkillBarWatcher, кормит SkillVerifier, на провал — ОДИН повтор нажатия (если разрешил бот).
    Повтор идёт через очередь ввода (execute_combo_now) — то есть с теми же человеческими
    задержками удержания и паузами, что и обычный скилл, а не мгновенным "кликом-дублем".

    Как проверить без игры: skill_check_test.py (фейковые watcher и input_manager).
    """
    RETRY_MIN_GAP_S = 0.5            # повторы не чаще этого: два провала подряд не дают "пулемёт"
    RETRY_MAX_AGE_S = 1.6            # провал старше этого (бот был занят) — уже не повторяем

    def __init__(self, input_manager, watcher, slot_keys, log=None) -> None:
        self._im = input_manager
        self._w = watcher
        self.v = SkillVerifier()
        self._log = log or (lambda msg: None)
        # Имя клавиши ввода -> подпись слота ("minus" -> "-"): ввод и панель называют клавиши по-разному.
        self._slot_of = dict(slot_keys)
        self._last_sample_t = float("-inf")
        self._last_retry_at = float("-inf")
        self.stats = {"ok": 0, "fail": 0, "was_cd": 0, "retry": 0}
        # Снимки готовых слотов: какие выключены на эту сессию (не совпали) и с какого времени
        # слот "чужой" / "на откате" подряд — для этих правил.
        self._snap_off: "set[str]" = set()
        self._snap_foreign_since: "dict[str, float]" = {}
        self._snap_false_since: "dict[str, tuple[float, float]]" = {}   # слот -> (с какого кадра, последний опрос)

    def _slot(self, key: str) -> "str | None":
        """
        Клавиша ввода -> слот панели. Панель найдена по форме (LayoutGeometry) — слоты и есть клавиши
        ("x", "minus"), прочитанные с табличек: у каждого аккаунта свои бинды. Иначе — старая таблица
        "клавиша -> позиция" (минус -> "-") для панели по шаблону.
        """
        keys = getattr(getattr(self._w, "bar", None), "layout", None)
        if keys is not None:
            return key if key in keys.rects else None
        return self._slot_of.get(key)

    def _key_of(self, slot: str) -> "str | None":
        if getattr(getattr(self._w, "bar", None), "layout", None) is not None:
            return slot
        return next((k for k, s in self._slot_of.items() if s == slot), None)

    def ready(self, action: str) -> "bool | None":
        """
        Готов ли скилл к нажатию по панели (для InputManager.skill_ready_fn — зовётся из потока
        ввода). True/False по последнему кадру слота против его обычной яркости; None — не знаем
        (панель не найдена, кадр старый, яркость ещё не выучена) — тогда жмём как раньше.
        """
        key = action[6:] if action.startswith("press_") else action
        slot = self._slot(key)
        if slot is None or getattr(self._w, "bar", None) is None:
            return None
        snap = self._snap_ready(slot)
        if snap is not None:
            return snap                   # есть снимок готового слота — учиться не нужно
        samples = self._w.samples_since(float("-inf"))
        if not samples:
            return None
        ts, slots = samples[-1]
        import time as _time
        if getattr(self._w, "_clock", _time.monotonic)() - ts > 0.5 or slot not in slots:
            return None
        ref = self.v._ref.get(slot)
        if ref is None or slot not in self.v._learned:
            return None                   # готовую яркость слота ещё не видели — не гадаем
        return slots[slot][0] > ref * ON_CD_RATIO

    def _snap_scores(self, slot: str) -> "tuple[float, float, float] | None":
        """(время кадра, ncc, ratio) свежего сравнения слота со снимком, или None."""
        snaps = getattr(self._w, "snaps", None)
        if snaps is not getattr(self, "_snaps_seen", None):
            # Снимки перечитаны (новый "Старт" после tools/capture_ready_slots.py) — прошлые выключения не в счёт.
            self._snaps_seen = snaps
            self._snap_off.clear()
            self._snap_foreign_since.clear()
            self._snap_false_since.clear()
        sc = getattr(self._w, "snap_scores", None)
        if sc is None or slot in self._snap_off:
            return None
        ts, by_slot = sc
        got = by_slot.get(slot)
        if got is None:
            return None
        import time as _time
        if getattr(self._w, "_clock", _time.monotonic)() - ts > SNAP_MAX_AGE_S:
            return None
        return ts, got[0], got[1]

    def _snap_ready(self, slot: str) -> "bool | None":
        """
        Готовность по снимку + два предохранителя от устаревшего снимка (сменил скилл и не
        переснял): слот яркий, но "чужой" SNAP_FOREIGN_S подряд, или бот SNAP_STUCK_S подряд
        спрашивал слот, а снимок всё время говорит "откат" (новая иконка темнее старой). В обоих
        случаях снимок слота выключается до конца сессии — дальше по выученной яркости, как раньше.
        """
        got = self._snap_scores(slot)
        if got is None:
            return None
        ts, ncc, ratio = got
        verdict = snapshot_verdict(ncc, ratio)
        if verdict is None:
            since = self._snap_foreign_since.setdefault(slot, ts)
            if ts - since >= SNAP_FOREIGN_S:
                self._snap_disable(slot, "на слоте другая иконка (похожесть %.2f)" % ncc)
            return None
        self._snap_foreign_since.pop(slot, None)
        if verdict:
            self._snap_false_since.pop(slot, None)
            return True
        since, last = self._snap_false_since.get(slot, (ts, ts))
        if ts - last > 1.5:
            since = ts                    # бот долго не спрашивал (вне боя / был готов другой скилл) — счёт заново
        self._snap_false_since[slot] = (since, ts)
        if ts - since >= SNAP_STUCK_S:
            self._snap_disable(slot, "%.0f с подряд 'на откате' (яркость %.2f от снимка)" % (ts - since, ratio))
            return None
        return False

    def _snap_disable(self, slot: str, why: str) -> None:
        self._snap_off.add(slot)
        self._snap_foreign_since.pop(slot, None)
        self._snap_false_since.pop(slot, None)
        self._log("SNAP слот %s: %s — снимок не подходит (сменил скилл?). Перезапусти "
                  "tools/capture_ready_slots.py; до конца сессии слот — по яркости" % (slot, why))

    def ready_detail(self, action: str) -> str:
        """Для лога: яркость слота сейчас / эталон "готов" (или почему не знаем)."""
        key = action[6:] if action.startswith("press_") else action
        slot = self._slot(key)
        got = self._snap_scores(slot) if slot is not None else None
        if got is not None:
            return "снимок %.2f/яр %.2f" % (got[1], got[2])
        samples = self._w.samples_since(float("-inf")) if slot is not None else []
        if not samples or slot not in samples[-1][1]:
            return "нет кадра"
        ref = self.v._ref.get(slot)
        v = samples[-1][1][slot][0]
        if ref is None or slot not in self.v._learned:
            return "%.0f/не выучен" % v
        return "%.0f/%.0f" % (v, ref)

    def tick(self, now: float, can_retry) -> None:
        presses = getattr(self._im, "skill_presses", None)
        while presses:
            key, t = presses.popleft()
            slot = self._slot(key)
            if slot is not None and getattr(self._w, "bar", None) is not None:
                self.v.add_press(slot, t)
        for ts, slots in self._w.samples_since(self._last_sample_t):
            self.v.add_sample(ts, slots)
            self._last_sample_t = ts
        for ev in self.v.update(now):
            self.stats[ev.kind] = self.stats.get(ev.kind, 0) + 1
            if ev.kind == "ok":
                self._log("SKILLCHK %s нажат: %s" % (ev.key, ev.info))
                cb = getattr(self, "on_ok", None)
                if cb is not None:
                    try:
                        cb(ev.key, ev.t_press)
                    except Exception:
                        pass
                continue
            self._log("SKILLCHK %s: %s" % (ev.key, ev.info))
            if ev.kind != "fail" or not ev.retry:
                continue
            if now - ev.t_press > self.RETRY_MAX_AGE_S or now - self._last_retry_at < self.RETRY_MIN_GAP_S:
                continue
            if not can_retry():
                continue
            key = self._key_of(ev.key)
            if key is None:
                continue
            self._last_retry_at = now
            self.v.expect_retry(ev.key)
            self.stats["retry"] += 1
            self._log("SKILLCHK %s не нажался — жму ещё раз" % ev.key)
            self._im.execute_combo_now(["press_%s" % key])
