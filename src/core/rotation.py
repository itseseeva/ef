"""
Ротация ПО КД (2026-10-11): что жать — решается в момент нажатия, по таймерам КД, которые
пользователь вписал каждому скиллу в приложении. Фото панели скиллов больше нет.

Зачем так. Раньше "готов ли скилл" бот узнавал по снимку панели: у другого человека (HDR,
другое разрешение, яркость) снимки не совпадали, и готовые скиллы считались "на откате".
Теперь бот сам ведёт часы каждого скилла: нажал -> скилл занят ровно на свой КД (+ запас на
пинг и задержку ввода). Это стандартная схема "cooldown tracker + action priority list" из
боевых ботов MMO, и у неё есть плюс, которого не было у фото: бот знает не только "готов / нет",
но и "через сколько откатится" — и может планировать (см. choose).

Как выбирается действие, когда ввод свободен (прошлое нажатие и его пауза/каст закончились):
  1) "Постоянно (по КД)" (например "6" раз в 5 с) — готов -> жмём сразу, раньше всего;
  2) защита точного КД: действие, которое займёт ввод дольше, чем осталось до отката скилла
     "по КД" (зажатие "8" 5 с перед "6"), не берём — сначала короткие, которые успевают.
     Многоударный скилл ("2" x3) разрываем: часть ударов сейчас, остальные — сразу после "6";
  3) "Обычный (в очереди)" — первый готовый по порядку цепочек (как записаны в приложении);
  4) защита от голодания: готовый скилл, который всё время обходят, через STARVE_S идёт первым;
  5) ничего не готово, но что-то откатится через доли секунды (<= WAIT_AHEAD_S) — ЖДЁМ (WAIT),
     а не начинаем автоатаку: её анимация задержала бы этот скилл;
  6) длинный каст/зажатие (поле "Время каста") — пауза после нажатия в самом вводе: пока она
     идёт, ввод занят, и следующее действие не выбирается (каст не обрывается).

Одинаковые шаги подряд ("2 2 2" или "Нажимать N раз подряд") — ОДНО действие из N нажатий.
Одна клавиша в нескольких цепочках — один скилл с одним таймером; его настройки (КД, каст,
зажатие) берутся из ПЕРВОЙ цепочки, где он встретился (верхняя в приложении главнее), а
расхождения пишутся в лог (conflicts). Цена схемы: бот не видит, сработало ли нажатие — если игра
его "съела" (оглушение, анимация), скилл пропустится до следующего отката.

Чистая логика без экрана и клавиатуры: на вход — цепочки (как их отдаёт skills_config), на
выход — какое действие жать. Как проверить без игры: rotation_test.py (симуляция боя).
"""
from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass
class Action:
    combo: "tuple[str, ...]"            # шаги ввода одного нажатия, например ("press_2",)
    repeat: int = 1                     # сколько раз подряд (удары одного скилла)
    cast_s: "float | None" = None       # пауза каста после последнего нажатия (None — обычная)
    own_cd: bool = False                # "Постоянно (по КД)" — самый высокий приоритет
    cd_s: float = 0.0                   # КД скилла из приложения, с
    source: str = ""                    # из какой цепочки (для лога)
    order: int = 0                      # место в общем порядке (для лога)
    last_fire: float = float("-inf")
    next_by_timer: float = 0.0          # раньше этого не жмём: прошлое нажатие + КД + запас
    ready_since: "float | None" = None  # с какого момента готов, но не нажат (защита от "голодания")
    next_hits: int = 0                  # сколько ударов жать В ЭТОТ раз (0 — все repeat); ставит choose()
    pending: int = 0                    # ударов осталось после разрыва (скилл по КД вклинился посередине)
    pending_until: float = 0.0          # до какого времени доигрываем оставшиеся удары

    @property
    def label(self) -> str:
        a = self.action_name
        key = a[6:] if a.startswith("press_") else a
        if any(s.startswith("holdfor_") for s in self.combo):
            key += "(зажать)"
        return key if self.repeat == 1 else "%s×%d" % (key, self.repeat)

    @property
    def action_name(self) -> str:
        """Главная клавиша действия ("press_2") — по ней узнаём один и тот же скилл. Зажатие — та же клавиша."""
        for s in self.combo:
            if s.startswith("press_"):
                return s
            if s.startswith("holdfor_"):
                return "press_" + s.split("_", 2)[2]
        return self.combo[-1]


# Запас к КД: нажатие доходит до игры с задержкой (очередь ввода, пинг), и скилл в игре уходит
# в откат чуть позже, чем бот нажал. Без запаса бот жал бы за миг до отката — игра съедала бы
# нажатие, а таймер считал бы скилл нажатым. Плюс случайные доли секунды: ровный ритм выдаёт бота.
CD_MARGIN_S = 0.15
CD_JITTER_S = 0.10
# Тот же скилл не жмём чаще (защита, если КД вписан 0 или очень маленький).
REFIRE_GUARD_S = 1.2
GAP_S = 0.85               # пауза ввода после обычного нажатия (InputManager: 0.77-0.87 с)
STRICT_SLACK_S = 0.3       # на столько скилл "по КД" может опоздать из-за чужого действия — не страшно
# Готовый скилл, которого всё время обходят более приоритетные, через столько идёт первым.
STARVE_S = 2.0
# Многоударный скилл разорвали скиллом "по КД" — оставшиеся удары доигрываем не позже этого
# срока, иначе забываем (окно комбо в игре короткое).
PENDING_S = 3.0
# Ничего не готово, но скилл откатится не позже чем через столько — ждём его, а не автоатаку.
WAIT_AHEAD_S = 0.35
DEFAULT_CD_S = 6.0         # КД не вписан (старый конфиг) — как у фронта по умолчанию
# Важные скиллы (2026-10-14, от игрока: "8 и 9 — важные, жду их по 10 с"; по логу — до 27 с). Откатился и
# влезает до отката "Постоянно (по КД)" — жмём первым, без очереди по порядку цепочки.
PRIORITY_KEYS = frozenset({"8", "9"})
# После "Старт" важные не жмём столько секунд (2026-10-14, от игрока: "в начале не сразу, подожди ~29 с").
PRIORITY_START_DELAY_S = 29.0

# Вернуть из choose(): ничего не жать и подождать — скилл вот-вот откатится.
WAIT = object()


class PriorityRotation:
    def __init__(self, actions: "list[Action] | None" = None, conflicts: "list[str] | None" = None) -> None:
        self.actions: "list[Action]" = list(actions or [])
        self.skips: "list[tuple[str, str]]" = []
        self.conflicts: "list[str]" = list(conflicts or [])   # один скилл с разными настройками в разных цепочках
        self.priority: "frozenset[str]" = PRIORITY_KEYS       # важные клавиши (см. PRIORITY_KEYS)

    @staticmethod
    def _merge(steps, own, source) -> "list[Action]":
        """Шаги цепочки (combo, каст, КД) -> действия; одинаковые шаги подряд — одно действие из N нажатий."""
        acts = []
        i = 0
        while i < len(steps):
            combo, cast, cd = tuple(steps[i][0]), steps[i][1], steps[i][2]
            j = i + 1
            while j < len(steps) and tuple(steps[j][0]) == combo:
                cast = steps[j][1] if steps[j][1] is not None else cast
                j += 1
            acts.append(Action(combo=combo, repeat=j - i, cast_s=cast, own_cd=own, cd_s=cd, source=source))
            i = j
        return acts

    @classmethod
    def from_chains(cls, chains: "list[dict]") -> "PriorityRotation":
        """
        Список приоритетов из цепочек skills_config: сначала "Постоянно (по КД)", потом остальные
        в порядке цепочек; одна клавиша — один скилл (повтор в другой цепочке не добавляем).
        """
        ordered = [c for c in chains if c.get("independent")] + [c for c in chains if not c.get("independent")]
        acts: "list[Action]" = []
        by_key: "dict[str, Action]" = {}
        conflicts: "list[str]" = []
        for c in ordered:
            gs = c.get("gamepad_steps") or []
            casts = c.get("cast_times") or [None] * len(gs)
            # КД каждого шага (новый фронт: "КД скилла" в редакторе шага). Старый конфиг без него —
            # КД цепочки, и только если нет и его — DEFAULT_CD_S.
            chain_cd = float(c.get("cooldown_max") or 0.0) or DEFAULT_CD_S
            cds = c.get("step_cds") or [chain_cd] * len(gs)
            steps = list(zip(gs, casts, cds))
            own = bool(c.get("independent"))
            if own and len({tuple(st[0]) for st in steps}) == 1:
                steps = steps[:1] * len(steps)    # скилл по своему КД, развёрнутый в N одинаковых шагов — это удары
            for a in cls._merge(steps, own, str(c.get("name", ""))):
                prev = by_key.get(a.action_name)
                if prev is not None:
                    # Тот же скилл уже есть выше: второй раз не добавляем, настройки — от верхнего
                    # (лог 2026-10-12: новая цепочка с настоящими КД сверху, старые с КД 6 ниже —
                    # "больший КД" брал бы старые 6 вместо настоящих 5).
                    diff = []
                    if abs(prev.cd_s - a.cd_s) > 1e-6:
                        diff.append("КД %g / %g" % (prev.cd_s, a.cd_s))
                    if prev.combo != a.combo or (prev.cast_s or 0) != (a.cast_s or 0):
                        diff.append("действие %s / %s" % (prev.label, a.label))
                    if prev.repeat != a.repeat:
                        diff.append("нажатий %d / %d" % (prev.repeat, a.repeat))
                    if diff:
                        conflicts.append("'%s' в '%s' и '%s': %s — беру из '%s'" % (
                            prev.label, prev.source, a.source, ", ".join(diff), prev.source))
                    continue
                a.order = len(acts)
                by_key[a.action_name] = a
                acts.append(a)
        return cls(acts, conflicts)

    def describe(self) -> str:
        return " → ".join("%s%s(%gс)" % (a.label, "*" if a.own_cd else "", a.cd_s) for a in self.actions)

    @staticmethod
    def busy_s(a: Action) -> float:
        """Сколько ввод будет занят этим действием: удары + каст или зажатие (holdfor_<сек>_<клавиша>)."""
        hold = 0.0
        for st in a.combo:
            if st.startswith("holdfor_"):
                try:
                    hold = max(hold, float(st.split("_", 2)[1]))
                except ValueError:
                    pass
        tail = max(hold, a.cast_s or 0.0, GAP_S)
        return (a.repeat - 1) * GAP_S + tail

    def busy_now(self, a: Action) -> float:
        """Сколько ввод будет занят выбранным сейчас действием (с учётом разрыва ударов)."""
        n = self.hits(a)
        return self.busy_s(a) if n >= a.repeat else n * GAP_S

    @staticmethod
    def hits(a: Action) -> int:
        """Сколько нажатий сделать по выбранному действию прямо сейчас (бот жмёт столько раз)."""
        return a.next_hits or a.repeat

    @staticmethod
    def _action_ok(a: Action, now: float, ready_fn=None) -> "str | None":
        """Можно ли жать сейчас: "КД" — откатился по таймеру, None — нельзя. ready_fn не нужен (фото нет)."""
        if now - a.last_fire < REFIRE_GUARD_S:
            return None
        return "КД" if now >= a.next_by_timer else None

    def strict_due(self, now: float) -> "float | None":
        """Через сколько секунд откатится ближайший скилл "по КД" (его таймер), или None — уже готов/не нажимали."""
        due = None
        for a in self.actions:
            if a.own_cd and a.next_by_timer > now:
                d = a.next_by_timer - now
                due = d if due is None else min(due, d)
        return due

    @staticmethod
    def _split_hits(a: Action, hits_fit) -> int:
        """Сколько ударов многоударного скилла влезает сейчас (0 — никак или скилл нельзя разрывать).
        Разрываем только простые удары: у скилла с кастом/зажатием пауза после последнего — часть
        самого скилла, её не перенести."""
        if a.repeat <= 1 or a.cast_s or any(st.startswith("holdfor_") for st in a.combo):
            return 0
        n = a.repeat - 1
        while n > 0 and not hits_fit(a, n):
            n -= 1
        return n

    def choose(self, now: float, ready_fn=None, own_only: bool = False,
               prefer: "set[str] | frozenset[str] | None" = None,
               avoid: "set[str] | frozenset[str] | None" = None) -> "tuple[Action, str] | object | None":
        """
        Что жать сейчас: (действие, почему); None — жать нечего (бот бьёт автоатакой);
        WAIT — ничего не жать, скилл вот-вот откатится. own_only — до первого удара по цели:
        только скиллы "по КД". ready_fn оставлен для совместимости вызова — не используется.
        prefer — клавиши, которые сейчас важнее порядка (режим пул: у дома куча мобов -> скиллы по
        площади "3", "4", "="): готовый и влезающий из них жмётся раньше обычных и "голодающих".
        "Постоянно (по КД)" и доигрывание разорванных ударов — всё равно первыми.
        avoid — клавиши, которые сейчас НЕ жать (режим пул, сбор: скиллы по площади бережём до дома).
        """
        self.skips = []
        for a in self.actions:
            a.next_hits = 0
        for a in self.actions:
            if a.own_cd and self._action_ok(a, now) is not None:
                return a, "КД"
        if own_only:
            return None
        due = self.strict_due(now)
        # Самое большое "окно" между нажатиями скилла по КД. Действие длиннее окна (зажатие "8" 5 с
        # при КД "6" 5 с) не влезет НИКОГДА — его берём в самом начале окна, сразу после скилла
        # по КД: тот опоздает на доли секунды, зато ничего не зависает навсегда.
        windows = [a.cd_s - GAP_S for a in self.actions if a.own_cd and a.cd_s > 0]
        window = min(windows) if windows else None

        def fits(a: Action) -> bool:
            if due is None:
                return True
            busy = self.busy_s(a)
            if busy <= due + STRICT_SLACK_S:
                return True
            return window is not None and busy > window + STRICT_SLACK_S and due >= window - 0.2

        def hits_fit(a: Action, n: int) -> bool:
            """n ударов БЕЗ каста (разрыв допустим только у таких) успеют до отката скилла по КД."""
            return due is None or n * GAP_S <= due + STRICT_SLACK_S

        # Доигрываем удары, разорванные скиллом по КД — раньше остального списка. Готовность тут не
        # проверяем: скилл уже начат, это его продолжение.
        for a in self.actions:
            if a.pending > 0:
                if now > a.pending_until:
                    a.pending = 0
                    continue
                n = a.pending
                while n > 0 and not hits_fit(a, n):
                    n -= 1
                if n > 0:
                    a.next_hits = n
                    return a, "доигрываю удары %d из %d" % (a.repeat - a.pending + n, a.repeat)

        # Важные (PRIORITY_KEYS) — первыми из обычных, как только откатились и влезают до отката "6".
        # Долгое зажатие "8" (дольше окна между "6") влезает в начале окна, сразу после "6" (см. fits) —
        # раньше это окно забирал первый по порядку скилл, и "8" ждала до 27 с (лог 01:30). Теперь окно
        # её. Порядок важных между собой — как в цепочке; avoid (пул, сбор) действует и на них.
        for a in self.actions:
            key = a.action_name[6:]
            if a.own_cd or a.pending > 0 or key not in self.priority or (avoid and key in avoid):
                continue
            why = self._action_ok(a, now)
            if why is not None and fits(a):
                return a, why + ", важный — приоритет"

        # Обычные — по порядку; голодающий (готов дольше STARVE_S и влезает) — первым.
        first = None
        starving = None
        preferred = None
        for a in self.actions:
            if a.own_cd or a.pending > 0:
                continue
            if avoid and a.action_name[6:] in avoid:
                continue                           # бережём (не жмём и не считаем "голодающим")
            why = self._action_ok(a, now)
            if why is None:
                a.ready_since = None               # на откате — счётчик ожидания сначала
                continue
            if a.ready_since is None:
                a.ready_since = now
            if not fits(a):
                n = self._split_hits(a, hits_fit)
                if n == 0:
                    self.skips.append((a.label, "не влезает до отката скилла по КД (через %.1f с)" % (due or 0.0)))
                    continue
                a.next_hits = n                    # влезает часть ударов — жмём их, остальные после "по КД"
                why += ", %d из %d ударов до скилла по КД" % (n, a.repeat)
            if first is None:
                first = (a, why)
            if prefer and preferred is None and a.action_name[6:] in prefer:
                preferred = (a, why + ", по площади — приоритет")
            if now - a.ready_since >= STARVE_S and (starving is None or a.ready_since < starving[0].ready_since):
                starving = (a, why + ", ждал %.0f с" % (now - a.ready_since))
        if preferred is not None:
            return preferred
        if starving is not None:
            return starving
        if first is not None:
            return first
        # Готового нет. Если какой-то скилл откатится через доли секунды и успеет до "по КД" — ждём
        # его (WAIT): начатая сейчас автоатака задержала бы его на свою анимацию.
        soon = [a.next_by_timer - now for a in self.actions
                if a.next_by_timer > now and now - a.last_fire >= REFIRE_GUARD_S - WAIT_AHEAD_S
                and (a.own_cd or fits(a))]
        if soon and min(soon) <= WAIT_AHEAD_S:
            return WAIT
        return None

    def delay_keys(self, keys, until: float) -> "list[str]":
        """Клавиши keys не жать до момента until (как будто на откате) — таймер КД сдвигаем вперёд,
        не назад: уже идущий более длинный откат не трогаем. Возвращает подписи сдвинутых."""
        moved = []
        for a in self.actions:
            key = a.action_name[6:]
            if key in keys and not a.own_cd and a.next_by_timer < until:
                a.next_by_timer = until
                moved.append(a.label)
        return moved

    def blocked_report(self, now: float, ready_fn=None, detail_fn=None) -> str:
        """Для лога, когда жать нечего: через сколько откатится каждый скилл."""
        out = []
        for a in self.actions:
            if now - a.last_fire < REFIRE_GUARD_S:
                why = "только что"
            else:
                why = "КД %.1f с" % max(0.0, a.next_by_timer - now)
            out.append("%s:%s" % (a.label, why))
        return ", ".join(out)

    def fired(self, a: Action, now: float) -> None:
        n = self.hits(a)
        if a.pending > 0:                          # доиграли (часть) оставшихся ударов
            a.pending = max(0, a.pending - n)
            a.next_hits = 0
            a.last_fire = now
            return
        if n < a.repeat:                           # разорвали: остальные удары — после скилла по КД
            a.pending = a.repeat - n
            a.pending_until = now + PENDING_S
        a.next_hits = 0
        a.last_fire = now
        a.ready_since = None
        # КД считаем от ПЕРВОГО нажатия скилла (удары одного скилла — внутри его КД).
        a.next_by_timer = now + a.cd_s + CD_MARGIN_S + random.uniform(0.0, CD_JITTER_S)

    def confirmed(self, action_name: str, t_press: float) -> None:
        """Совместимость: раньше панель подтверждала нажатие. Без фото подтверждать нечем."""
        return None
