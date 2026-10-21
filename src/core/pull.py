"""
src/core/pull.py — режим "пул" (ТЕСТ, 2026-10-13): сбор мобов тычками -> бег домой -> добивание у дома.

Зачем: бот фармит для группы, лут падает там, где моб УМЕР, а подбирается в ~35 м. Люди стоят на
месте (дом = где запустили бота). Обычный фарм уводил бота за мобами на 40-80 м — лут мимо.
Здесь бой всегда у дома: ближние мобы прибегают вплотную, дальнобойные встают на своей дистанции
от ДОМА (~15-25 м) — оба внутри 35 м.

Цикл (фаза в self.phase):
  KILL   — у дома: бьём ТОЛЬКО тычкнутых (HP < full_hp — прибежали за нами), не дальше
           kill_zone_m от дома. Целых мобов у дома не трогаем — их тоже сначала тычка на сборе (просьба
           2026-10-13: "давать тычку и приводить домой", без обычного фарма). Пусто kill_idle_s -> GATHER.
  GATHER — сбор: бегаем не дальше run_m от дома, каждому мобу с полным HP — тычка (F+E, ждём
           первого падения HP) и сразу следующий. Уже тычкнутых (HP < full_hp) не берём.
           max_tags тычек / gather_s секунд / некого тыкать -> RETURN.
  RETURN — домой с Shift, без скиллов, мобы бегут следом. Дошёл / return_timeout_s -> KILL.

Класс — чистая логика (без экрана и ввода): решает "кого можно брать" и "какая фаза дальше".
Нажатия, движение, лог — в FarmBot (_pull_*). Включение: переменная окружения EZF_PULL=1
(её ставит tools/pull_test.py), в обычном боте и exe режим выключен.

Песочница: python tools/pull_logic_test.py — фазы и фильтр целей на искусственных строках списка.
"""
from __future__ import annotations

from dataclasses import dataclass, field

KILL, GATHER, RETURN = "добивание", "сбор", "домой"

# Память тычкнутых строк (PullPlanner.track). Номера строк меняет только R (автообновление в игре
# выключено), а R бывает лишь в начале сбора — поэтому "скачок метров = перенумерация" больше не
# проверяем (2026-10-14: одно ложное чтение "№4 — 5 м" вместо 17 м стирало память о тычке).
TAG_LOST_S = 2.0        # строки с этим номером нет дольше — моб умер/вышел из скана, номер свободен


@dataclass
class TagAttempt:
    """Одна попытка тычки: когда начали, ближе всего к цели (м) и когда последний раз сблизились.
    dataclass с значениями по умолчанию — вместо кортежа (key, start_dist): поля по имени, не по индексу."""
    entered_at: float
    best_m: "int | None" = None
    best_t: float = 0.0


@dataclass
class PullTuning:
    """Все числа режима — здесь: поменять = одна цифра. dataclass: значения по умолчанию + тип,
    без ручного __init__ (идиома Python 3.7+)."""
    run_m: float = 60.0           # дальше этого от дома бот на сборе сам не убегает
    tag_m: float = 80.0           # моб (по худшей оценке) не дальше этого от дома
    reach_m: float = 15.0         # дистанция удара (лог: "RANGE ... дистанция удара ~15 м")
    max_tags: int = 4             # тычек за один сбор (2026-10-13: пробовали 3 и 8, остановились на 4).
                                  # Потолок — 8: в списке целей 8 строк (F1-F8). Своё HP бот не видит —
                                  # больше паровоз = больше урона по персонажу
    gather_s: float = 30.0        # сбор не дольше (с первой тычки): 4 тычки по ~3-5 с + запас
    return_timeout_s: float = 20.0
    kill_zone_m: float = 30.0     # добивание: цели — только в этой зоне вокруг ДОМА (по худшей оценке: бот от
                                  # дома + моб от бота). Привезённые стоят в ~20 м, нападающие — в 20-30 м
                                  # (2026-10-13: "бить наагренных и тех, кто атакует, в 20-30 м от дома")
    home_kill_m: float = 20.0     # моб у самого дома (худшая оценка: бот от дома + моб от бота <= этого) — не тычем
                                  # и не водим, а сразу бьём (2026-10-14, от игрока: "рядом с домиком — сразу бить").
                                  # Худшая оценка, т.к. направления на моба таблица не знает: 20 м — точно у дома
    kill_near_m: float = 10.0     # у дома строку БЕЗ прочитанного HP добиваем, если она ближе этого: полоска
                                  # под именем читается не всегда, а вплотную к нам — почти наверняка наш моб
    chaser_m: float = 6.0         # на сборе строки ближе этого к боту — бегущие за нами (уже тычкнуты): мимо.
                                  # Тест 2026-10-13: "ближе другая цель" уводила на моба в 0 м с HP 86%,
                                  # его HP в строке не прочиталось — и он шёл как новая тычка
    tag_bot_m: float = 40.0       # на сборе дальше этого ОТ БОТА не бежим: один дальний моб = 6-9 с бега,
                                  # быстрее вернуться с тремя и собрать следующий цикл
    kill_leash_m: float = 15.0    # добивание: бот дальше этого от дома -> назад к дому, мобы бегут следом
                                  # (2026-10-13: было 25; игрок: "ограничь движение у дома 15 м")
    kill_move_free_m: float = 8.0   # добивание: дальше этого от дома боевые шаги/стрейфы — с уклоном К ДОМУ:
                                    # бот дерётся и по пути плавно идёт к людям (2026-10-13: "стоять плохо")
    # Скиллы по площади (2026-10-13, от игрока: "по площади бьют 3, 4, ="). У дома, когда рядом
    # aoe_min_mobs мобов и больше (ближе aoe_near_m к боту), ротация жмёт их первыми: привезённая
    # куча ближников стоит вплотную, один удар по площади = урон всем сразу.
    aoe_keys: "tuple[str, ...]" = ("3", "4", "=")
    gather_save_keys: "tuple[str, ...]" = ("8",)   # на сборе не жмём и их: "8" — зажатие 6 с, бот стоял бы
                                                    # на одном мобе вместо тычек; бережём для дома (2026-10-14)
    aoe_min_mobs: int = 2
    aoe_near_m: float = 8.0
    topup_enabled: bool = False   # 2026-10-13, от игрока: "в таблице < 4 — пулит тех, кто есть, и убивает" —
                                  # добора у дома нет (лишнее R и время на каждый неполный цикл)
    topup_zone_m: float = 30.0    # добор у дома: недобранный паровоз (тычек < max_tags) добираем мобами, которые
                                  # (по худшей оценке) не дальше этого от дома — бот за ними почти не отходит
    tag_timeout_s: float = 7.0    # сбор: потолок одной тычки. Было 5 с "без тычки" независимо от бега — лог
                                  # 00:32:40: бросили №2 в 12 м, когда он уже подбегал (3 моба вместо 4).
                                  # Теперь главное правило — "не сближаемся" (tag_stall_s), потолок — страховка
    # Орлиное зрение (2026-10-13): автообновление таблицы в игре ВЫКЛЮЧЕНО, таблицу обновляет бот
    # клавишей R — когда ему нужно. Между нажатиями номера строк стабильны, бот доверяет им без
    # перепроверок. Без КД (от игрока), применяется мгновенно — ждём eagle_wait_s и читаем таблицу.
    eagle_key: str = "r"
    eagle_wait_s: float = 0.2
    eagle_floor: bool = True          # перед R камера вниз на персонажа: скан идёт там, где прицел (от игрока)
    eagle_floor_max_s: float = 2.0    # поза не встала за столько — жмём R как есть (было 1.2: без калибровки
                                      # камере ехать вниз 200° ~1.3 с — лог 01:28:37 "камера не встала")
    eagle_home_m: float = 5.0         # R — только у самого дома: дальше этого сначала к центру дома
                                      # (2026-10-14, от игрока: скан вокруг курсора = вокруг персонажа)
    eagle_home_trip_s: float = 6.0    # к центру дома не дошли за столько — жмём R где стоим
    eagle_floor_hold_s: float = 0.15  # после R камера стоит столько: нажатию дойти до игры в этой позе
    # Цикл (2026-10-13, от игрока): дома R -> снимок таблицы -> тычем только мобов из снимка (ближний
    # первым, до max_tags) -> домой -> добиваем -> снова R. Посреди сбора R НЕ жмём: каждое R
    # перенумеровывает строки, бот забывал тычкнутых и кликал их второй раз (лог 07:59:48, ~1 с впустую).
    eagle_settle_max_s: float = 1.0   # после R таблица ~0.1-0.8 с ПУСТАЯ (лог 07:59:18, 01:03:45: строки через 0.8 с) — ждём до этого
    eagle_stable_s: float = 0.12      # строки появились — ещё одно-два чтения, пока дочитаются метры ("1:? 3:29м" -> полная)
    eagle_empty_retry_s: float = 2.0  # после R таблица ПУСТАЯ — стоим дома и жмём R снова не чаще этого
    eagle_rows_retry_s: float = 5.0   # мобы в таблице есть, но все недоступны — R реже: метры строк и так живые,
                                      # R нужен лишь чтобы увидеть НОВЫХ (лог 01:06:34-47: R каждые 2 с, 6 раз)
    tag_stall_s: float = 2.6      # сбор: столько с последнего сближения на tag_stall_min_m — персонаж не бежит,
                                  # (было 1.5: бросали раньше, чем успевал повтор E — первый E часто не ведёт,
                                  # второй через ~0.9 с ведёт; брошенная ближняя цель уходила в чёрный список,
                                  # и бот бежал к дальней — лог 07:51:50)
    tag_stall_min_m: float = 2.0  # а она дальше удара — "E не повёл" (лог 06:54:50: 4 с повторов E на месте), бросаем
    tag_drop_m: float = 50.0
    drop_blacklist_s: float = 3.0     # брошенную на сборе цель не берём столько (было 8 с — дольше всего сбора)      # сбор: цель дальше этого от бота (убегает) — бросаем сразу
    kill_drop_slack_m: float = 3.0
    # "Дом зачищен" (2026-10-13: бот довёл моба до половины HP и ушёл на сбор). Пока рядом есть кто-то,
    # кто бьёт нас (мечи) или ранен нами, — сбор не начинаем, бьём их. Дальше этого от бота таких
    # не считаем (ушли на спавн/это чужие).
    kill_clear_m: float = 40.0
    kill_drop_bot_m: float = 25.0 # бросаем цель, только если она НЕ бьёт нас и уже дальше этого от бота  # цель ушла за kill_zone_m на столько (отагрилась, бежит на спавн) — бросаем
    follow_s: float = 1.5         # сбор: строка не дальше chaser_m от бегущего бота столько подряд — моб бежит за
                                  # нами (прицепился сам/от "6"/брошенная цель) — он в паровозе, в счёт max_tags
                                  # (лог 00:24:26-33: брошенный №3 держался в 0-5 м — пятый моб сверх 4)
    stranger_s: float = 1.5       # у дома: выбранная цель с полным HP и без мечей столько — не наша, из боя выходим
                                  # (лог 00:25:23: игра сама выделила свежего моба, бот 10 с за ним бегал, R не было)
    kill_idle_s: float = 0.5      # у дома некого добивать столько — новый сбор (меньше нельзя: список
                                  # мигает пустым на 1-2 кадра, а 1 с — это ~6 чтений подряд)
    full_hp: float = 0.99         # HP строки списка не ниже — моб не тронут. Было 0.97: тычка сносит
                                  # ~2-3% (тест 2026-10-13: 100% -> 97.8%), и тычкнутый считался "целым"
    loot_m: float = 35.0          # радиус подбора лута — для итога "сколько убито в зоне"


@dataclass
class PullStats:
    cycles: int = 0
    tags: int = 0
    kills: int = 0
    kills_in_zone: int = 0
    lost: int = 0                 # тычкнули, но у дома не убили (не добежал / сбросился)
    worst_kill_m: float = 0.0
    topups: int = 0               # сколько раз дома добирали паровоз до max_tags
    leash_returns: int = 0        # сколько раз в добивании отходили назад к дому (поводок kill_leash_m)


@dataclass
class PullPlanner:
    t: PullTuning = field(default_factory=PullTuning)
    phase: str = KILL             # старт — как конец цикла: "дома пусто?" -> R -> мобов у дома бьём сразу,
                                  # иначе сбор по этой же таблице (2026-10-14)
    tags: int = 0                 # тычек в текущем сборе
    gather_t0: "float | None" = None   # время первой тычки сбора
    return_t0: float = 0.0
    kill_idle_since: "float | None" = None
    cycle_kills: int = 0          # убито у дома в текущем цикле (для "потеряно")
    leash_return: bool = False
    center_return: bool = False   # текущий RETURN — шаг к центру дома перед R
    # Паровоз уже привезли домой (2026-10-14, от игрока: "расчистка дома — только после пула"). До этого
    # (старт, сбор) целых мобов у дома не бьём сразу, а тычем вместе с остальными — 4 рядом = быстрый пул;
    # после — у дома бьём всех: привезённых, напавших и целых у самого дома (home_kill_m).
    pulled: bool = False
    # Добор у дома (2026-10-13: "наагрил 2, пришёл домой и бьёт 2, а рядом с домом ещё двое"): фаза
    # GATHER, но тычки — только рядом с домом, и после добора — сразу добивание, без бега домой.
    topup: bool = False
    need_eagle: bool = True           # перед выбором целей нажать R (начало сбора/добора)
    eagle_until: float = 0.0          # до этого момента таблица ещё обновляется — цели не выбираем
    eagle_last: float = -1e9
    eagle_rows_t: "float | None" = None   # когда после R таблица впервые показала строки
    # Снимок таблицы после R: номера строк, из которых тычем в этом сборе (set — "in" за O(1)).
    # None — снимка ещё нет. Автообновление в игре выключено, поэтому номера до следующего R стабильны.
    plan: "set[int] | None" = None
    eagle_seen: "set[int]" = field(default_factory=set)
    # KILL: "дома пусто" по старой таблице — нажали R и ждём свежую (2026-10-14, лог 00:42:33: без
    # автообновления подошедшие после R мобы в таблице не видны, бот ушёл на сбор, а его били двое).
    verifying: bool = False
    eagle_pending: bool = False       # R заказан, камера едет вниз — таблицу ещё не читаем
    # Прицепившиеся на сборе: номер -> когда впервые оказался рядом (кандидаты) и подтверждённые (set).
    near_since: "dict[int, float]" = field(default_factory=dict)
    followers: "set[int]" = field(default_factory=set)    # текущий RETURN — отход по поводку посреди добивания, а не конец сбора
    # Память тычкнутых в этом сборе: номер строки -> [последние метры, когда видели]. Номера стабильны
    # до следующего R; ошибись память — страхует проверка HP на панели ("уже тычкнута"). dict: O(1) по номеру.
    tagged: "dict[int, list]" = field(default_factory=dict)
    stats: PullStats = field(default_factory=PullStats)

    # --- фильтр целей ---
    def excluded(self, rows, dist_home_m: float) -> "set[int]":
        """
        Номера строк списка, которые в этой фазе брать НЕЛЬЗЯ (добавляются к чёрному списку бота).
        Направления на моба список не знает — считаем худший случай (моб прямо "за спиной" от дома):
          GATHER: моб <= tag_m от дома  И  бот, подбежав на дистанцию удара, <= run_m от дома;
                  тычкнутые (HP < full_hp) и строки без метров — мимо;
          KILL:   тычкнутые (HP < full_hp) и нападающие (мечи) в зоне kill_zone_m от дома; целые — мимо;
          RETURN: никого (бежим домой).
        set comprehension — одна строка вместо цикла с add, и сразу множество для быстрого "in".
        """
        t = self.t
        if self.phase == RETURN:
            return {r.index for r in rows}
        if self.phase == KILL:
            return {r.index for r in rows if not self._kill_ok(r, dist_home_m)}
        return {r.index for r in rows
                if r.distance_m is None
                or (self.plan is not None and not self.topup and r.index not in self.plan)
                or r.index in self.followers
                or r.index in self.tagged
                or (self.topup and dist_home_m + r.distance_m > t.topup_zone_m)
                or (r.hp is not None and r.hp < t.full_hp)
                or getattr(r, "attacking", False)   # мечи = уже агрится на нас: он в паровозе, тычка не нужна
                or r.distance_m <= t.chaser_m
                or (self.tags > 0 and r.distance_m > t.tag_bot_m)   # первую цель берём и дальше (лог 01:06:34)
                or dist_home_m + r.distance_m > t.tag_m
                or dist_home_m + max(0.0, r.distance_m - t.reach_m) > t.run_m}

    def _kill_ok(self, r, dist_home_m: float) -> bool:
        """
        У дома: цель только в зоне kill_zone_m вокруг ДОМА (худший случай: бот от дома + моб от бота) —
        тогда и автобег игры к ней не уведёт бота дальше зоны, и лут упадёт у людей. В зоне:
        нападающие (мечи) — всегда; тычкнутые (HP < full_hp) — да; без HP — только вплотную.
        """
        t = self.t
        attacking = getattr(r, "attacking", False)
        if r.distance_m is None:
            return attacking                      # метры не прочли: верим только мечам (бьёт нас — рядом)
        if attacking:
            # Бьёт нас — добиваем всегда, пока он ближе kill_clear_m: он сам у нас, лут упадёт рядом;
            # отбежим за поводок — вернёмся к дому, а он следом (2026-10-13: "очистить домик").
            return r.distance_m <= t.kill_clear_m
        if self.pulled and dist_home_m + r.distance_m <= t.home_kill_m:
            return True                           # после пула: у самого дома — бьём и целого (расчистка)
        if dist_home_m + r.distance_m > t.kill_zone_m:
            # Худшая оценка вне зоны, но раненый вплотную к нам — всё равно наш, добиваем.
            return r.hp is not None and r.hp < t.full_hp and r.distance_m <= t.reach_m
        if r.hp is not None:
            return r.hp < t.full_hp
        # Без HP вплотную — наш привезённый (HP строки не прочиталось); до пула это просто моб рядом —
        # его тычем на сборе, а не бьём (лог 2026-10-14: на старте бот дрался с одним вместо пула 4).
        return self.pulled and r.distance_m is not None and r.distance_m <= t.kill_near_m

    # --- переходы фаз ---
    def start_gather(self, now: float, fresh: bool = False) -> None:
        """Новый сбор. fresh=True — R только что нажат проверкой "дома пусто" и таблица дочиталась:
        снимок — она же, второй R не нужен."""
        self.phase, self.tags, self.gather_t0, self.kill_idle_since = GATHER, 0, None, None
        self.topup = self.verifying = self.pulled = False
        self.tagged.clear()
        self.near_since.clear()
        self.followers.clear()
        self.need_eagle = not fresh
        self.plan = set(self.eagle_seen) if fresh else None

    def on_eagle(self, now: float) -> None:
        """Нажали R: таблица перенумеруется через мгновение — номера в памяти тычкнутых больше не те,
        снимок — заново."""
        self.need_eagle, self.plan, self.eagle_rows_t = False, None, None
        self.eagle_until = now + self.t.eagle_wait_s
        self.eagle_last = now
        self.eagle_seen.clear()
        self.tagged.clear()
        self.near_since.clear()
        self.followers.clear()

    def eagle_settled(self, now: float, rows) -> bool:
        """
        После R: таблица дочиталась? Пустое чтение сразу после R — это таблица перерисовывается, а не
        "мобов нет": ждём первых строк (до eagle_settle_max_s), потом ещё eagle_stable_s, пока
        дочитаются метры. Номера всех строк, виденных за это время, копим — из них снимок (take_plan).
        """
        if self.eagle_pending or now < self.eagle_until:
            return False
        if rows:
            self.eagle_seen.update(r.index for r in rows)
            if self.eagle_rows_t is None:
                self.eagle_rows_t = now
        if self.eagle_rows_t is not None and now - self.eagle_rows_t >= self.t.eagle_stable_s:
            return True
        return now - self.eagle_last >= self.t.eagle_settle_max_s

    def adopt_rows(self, rows) -> bool:
        """Снимок вышел пустым (таблица не успела прочитаться), а строки появились позже — берём их без
        второго R: R между ними не было, номера те же (лог 00:32:27: второй R стоил 2.3 с)."""
        if self.plan or self.plan is None or self.tags or not rows:
            return False
        self.plan = {r.index for r in rows}
        return True

    def plan_exhausted(self) -> bool:
        """Из снимка тыкать некого: все тычкнуты, брошены (drop_from_plan) или прицепились сами."""
        return self.plan is not None and not (self.plan - self.tagged.keys() - self.followers)

    def take_plan(self) -> "set[int]":
        """Снимок: тычем только мобов, что были в таблице после R (фильтры excluded — как всегда)."""
        self.plan = set(self.eagle_seen)
        return self.plan

    def drop_from_plan(self, index: "int | None") -> None:
        """Цель из снимка не тычется (не добежал / таймаут) — в этом сборе к ней не возвращаемся."""
        if self.plan is not None and index is not None:
            self.plan.discard(index)

    def eagle_empty_retry(self, now: float, has_rows: bool = False) -> bool:
        """Снимок есть, а тыкать некого и тычек 0 — стоим дома; True — пора ещё раз R. Таблица пустая —
        через eagle_empty_retry_s; мобы в ней есть (но далеко) — реже, через eagle_rows_retry_s."""
        wait = self.t.eagle_rows_retry_s if has_rows else self.t.eagle_empty_retry_s
        return self.plan is not None and self.tags == 0 and now - self.eagle_last >= wait

    def home_busy(self, rows, dist_home_m: float, skip_selected: bool = False) -> bool:
        """
        Дом не расчищен: в таблице есть кого бить у дома (бьёт нас / ранен в зоне / у самого дома) —
        то же правило, что выбор цели в добивании (_kill_ok). Сначала расчистить дом, потом пулить
        (2026-10-14, лог 01:03:44: бот тыкал мобов, которые уже били его у дома).
        """
        return any(self._kill_ok(r, dist_home_m) for r in rows
                   if not (skip_selected and getattr(r, "selected", False)))

    def finish_topup(self, now: float) -> None:
        """Добор у дома закончен (набрали max_tags или рядом некого) — добиваем всех тут же."""
        self.phase, self.topup, self.kill_idle_since = KILL, False, None

    def mark_tagged(self, index: "int | None", dist_m: "int | None" = None, now: float = 0.0) -> None:
        """Строка №index (сейчас в dist_m метрах) уже тычкнута — на сборе её больше не берём."""
        if index is not None:
            self.tagged[index] = [dist_m, now]

    def track(self, rows, now: float) -> None:
        """Сверка памяти тычкнутых с чтением таблицы: строки нет дольше TAG_LOST_S — моб умер/ушёл из
        скана, номер свободен. Без экрана, микросекунды (до 8 строк)."""
        by_index = {r.index: r for r in rows}          # dict comprehension: номер -> строка
        for idx, mem in list(self.tagged.items()):     # list(...): словарь меняем по ходу цикла
            r = by_index.get(idx)
            if r is None:
                if now - mem[1] > TAG_LOST_S:
                    del self.tagged[idx]
            else:
                mem[0] = r.distance_m if r.distance_m is not None else mem[0]
                mem[1] = now

    def note_followers(self, rows, now: float) -> None:
        """
        Сбор: кто бежит за нами. Строка (не тычкнутая, не выбранная) не дальше chaser_m от бота
        follow_s подряд — моб прицепился: в паровоз. Отошёл раньше — отсчёт заново (мимо пробежали).
        Номера стабильны до следующего R (автообновление в игре выключено) — номеру можно верить.
        """
        if self.phase != GATHER:
            return
        close = {r.index for r in rows                       # set comprehension: "рядом" за один проход
                 if r.distance_m is not None and r.distance_m <= self.t.chaser_m
                 and r.index not in self.tagged and not getattr(r, "selected", False)}
        for idx in list(self.near_since):
            if idx not in close and idx not in self.followers:
                del self.near_since[idx]
        for idx in close:
            since = self.near_since.setdefault(idx, now)
            if now - since >= self.t.follow_s:
                self.followers.add(idx)

    def train(self, rows) -> int:
        """
        Сколько мобов в паровозе: тычкнутые + напавшие сами (мечи в строке, не из тычкнутых номеров).
        2026-10-13: агрессивные мобы у дома нападают сами — раньше бот бросал сбор и дрался с ними
        скиллами ("бьёт скиллами незаагренных"); теперь они просто едут домой вместе с паровозом.
        """
        present = {r.index for r in rows}
        extra = {r.index for r in rows if getattr(r, "attacking", False) and not getattr(r, "selected", False)}
        extra |= self.followers & present                     # прицепившиеся — тоже паровоз (без двойного счёта)
        return self.tags + len(extra - set(self.tagged))

    def stranger(self, hp_percent: float, sel, dist_home_m: "float | None" = None) -> bool:
        """У дома/на сборе: цель с полным HP, без мечей и не тронутая нами (по строке) — чужой моб,
        не наш паровоз (его выделила игра сама, а не мы из таблицы). Исключение — KILL и моб у самого
        дома (home_kill_m): таких бьём сразу, даже целых."""
        if self.phase not in (KILL, GATHER) or hp_percent < self.t.full_hp * 100.0:
            return False
        if sel is None:
            return True
        if (self.phase == KILL and self.pulled and dist_home_m is not None and sel.distance_m is not None
                and dist_home_m + sel.distance_m <= self.t.home_kill_m):
            return False
        if getattr(sel, "attacking", False) or sel.index in self.tagged or sel.index in self.followers:
            return False
        return sel.hp is None or sel.hp >= self.t.full_hp

    def on_tag(self, now: float, index: "int | None" = None, dist_m: "int | None" = None) -> bool:
        """Тычка прошла. True — сбор окончен (пора домой), False — тыкаем следующего."""
        self.mark_tagged(index, dist_m, now)
        self.tags += 1
        self.stats.tags += 1
        if self.gather_t0 is None:
            self.gather_t0 = now
        return self.tags >= self.t.max_tags or now - self.gather_t0 >= self.t.gather_s

    def gather_should_return(self, now: float, dist_home_m: float, has_candidate: bool) -> bool:
        """На сборе вне боя: убежал дальше run_m, время вышло, или тыкать больше некого (а тычки были)."""
        if dist_home_m > self.t.run_m:
            return True
        if self.tags == 0:
            return False                  # ещё никого не тычкнули — ищем дальше (прогулка у дома)
        return not has_candidate or now - (self.gather_t0 or now) >= self.t.gather_s

    def aoe_now(self, rows) -> int:
        """KILL: сколько мобов вплотную к боту (ближе aoe_near_m, включая нашу цель). 0 — не KILL."""
        if self.phase != KILL:
            return 0
        return sum(1 for r in rows if r.distance_m is not None and r.distance_m <= self.t.aoe_near_m)

    def tag_hopeless(self, sel, now: float, at: TagAttempt) -> "str | None":
        """
        Сбор: тычка по выбранной цели не выходит — причина (бросаем) или None (ждём). Попутно ведёт
        at.best_m/best_t — "когда последний раз сблизились". Бросаем, только если НЕ сближаемся
        tag_stall_s (персонаж стоит), цель убегает дальше tag_drop_m или вышел потолок tag_timeout_s.
        Пока подбегаем — ждём (лог 00:32:40: бросили №2 в 12 м на подбеге по старому таймеру 5 с).
        """
        if self.phase != GATHER:
            return None
        d = sel.distance_m if sel is not None else None
        if d is not None:
            if at.best_m is None:
                at.best_m, at.best_t = d, at.entered_at
            elif d <= at.best_m - self.t.tag_stall_min_m:
                at.best_m, at.best_t = d, now
            if d > self.t.tag_drop_m:
                return "цель убегает (%d м > %.0f м)" % (d, self.t.tag_drop_m)
            if d > self.t.reach_m + 3 and now - at.best_t >= self.t.tag_stall_s:
                return "к цели не сближаемся %.1f с (%d м)" % (now - at.best_t, d)
        if now - at.entered_at > self.t.tag_timeout_s:
            return "тычки нет %.1f с" % (now - at.entered_at)
        return None

    def aoe_keys_now(self) -> "set[str] | None":
        """Сбор и бег домой: скиллы по площади бережём до кучи у дома (2026-10-13)."""
        if self.phase in (GATHER, RETURN):
            return set(self.t.aoe_keys) | set(self.t.gather_save_keys)
        return None

    def target_left_zone(self, sel, dist_home_m: float) -> bool:
        """
        KILL: наша цель ушла (отагрилась и бежит на спавн) — за ней не бежим. Только если она НЕ бьёт
        нас (мечей нет) и уже дальше kill_drop_bot_m от бота, а по худшей оценке — вне зоны. Раньше
        хватало "вне зоны по оценке": дальнобойный в 17 м, бьющий нас, бросался недобитым (лог 07:09:56).
        """
        return (self.phase == KILL and sel is not None and sel.distance_m is not None
                and not getattr(sel, "attacking", False)
                and sel.distance_m > self.t.kill_drop_bot_m
                and dist_home_m + sel.distance_m > self.t.kill_zone_m + self.t.kill_drop_slack_m)

    @staticmethod
    def attacked(rows) -> int:
        """Сколько мобов бьют нас (мечи в строке списка), КРОМЕ выбранной цели: её мечи — это мы её
        тычем/бьём, а не "на нас напали" (иначе первая же тычка сбора выглядела бы нападением)."""
        return sum(1 for r in rows if getattr(r, "attacking", False) and not getattr(r, "selected", False))

    def start_kill(self, now: float) -> None:
        """Нас бьют до первой тычки сбора — сначала добиваем их здесь (цикл не считаем)."""
        self.phase, self.kill_idle_since = KILL, None

    def start_return(self, now: float, leash: bool = False, center: bool = False) -> None:
        """Домой. leash=True — это не конец сбора, а возврат посреди добивания (ушёл дальше kill_leash_m).
        center=True — шаг к центру дома перед R (не конец сбора и не поводок: счётчики не трогаем)."""
        self.phase, self.return_t0, self.leash_return, self.center_return = RETURN, now, leash, center

    def kill_too_far(self, dist_home_m: float) -> bool:
        """KILL: бой увёл бота дальше kill_leash_m от дома — пора назад."""
        return self.phase == KILL and dist_home_m > self.t.kill_leash_m

    def on_home(self, now: float, train: "int | None" = None) -> None:
        """Дошли (или время вышло) — добиваем у дома. Тычки цикла запомнены для учёта потерь.
        Возврат по поводку — тот же цикл: счётчики цикла не трогаем."""
        if self.center_return:
            # Дошли к центру дома перед R — дальше та же проверка "дома пусто?" (уже отсюда).
            self.phase, self.kill_idle_since, self.center_return = KILL, None, False
            return
        if self.leash_return:
            self.phase, self.kill_idle_since, self.leash_return = KILL, None, False
            self.stats.leash_returns += 1
            return
        self.phase, self.kill_idle_since, self.cycle_kills = KILL, None, 0
        self.pulled = True                        # паровоз дома — теперь расчистка дома
        self.stats.cycles += 1
        if self.t.topup_enabled and (self.tags if train is None else train) < self.t.max_tags:
            # Паровоз недобран (train — тычкнутые + напавшие сами, см. train()) — сначала добор рядом с домом (память тычкнутых сохраняем).
            self.phase, self.topup = GATHER, True
            self.need_eagle = True                            # у дома — свежая таблица (R)
            self.stats.topups += 1

    def kill_idle(self, now: float, has_target: bool) -> bool:
        """KILL: True — у дома пусто kill_idle_s, пора на сбор (и подвести итог цикла)."""
        if has_target:
            self.kill_idle_since = None
            return False
        if self.kill_idle_since is None:
            self.kill_idle_since = now
        return now - self.kill_idle_since >= self.t.kill_idle_s

    def close_cycle(self) -> int:
        """Итог цикла перед новым сбором: сколько тычкнутых так и не умерло у дома."""
        lost = max(0, self.tags - self.cycle_kills)
        self.stats.lost += lost
        self.tags = 0
        return lost

    def on_kill(self, kill_dist_home_m: float) -> bool:
        """Учёт убийства: kill_dist_home_m — оценка сверху, где умер моб. True — внутри зоны лута."""
        s = self.stats
        s.kills += 1
        if self.phase == KILL:
            self.cycle_kills += 1
        s.worst_kill_m = max(s.worst_kill_m, kill_dist_home_m)
        inside = kill_dist_home_m <= self.t.loot_m
        s.kills_in_zone += inside
        return inside

    def summary_pairs(self) -> "list[tuple[str, str]]":
        s = self.stats
        pct = "%.0f%%" % (100.0 * s.kills_in_zone / s.kills) if s.kills else "-"
        return [("ПУЛ: циклов сбора", "%d" % s.cycles),
                ("ПУЛ: тычек", "%d" % s.tags),
                ("ПУЛ: убито в %.0f м от дома" % self.t.loot_m, "%d из %d (%s)" % (s.kills_in_zone, s.kills, pct)),
                ("ПУЛ: тычкнули, но не добили", "%d" % s.lost),
                ("ПУЛ: самое дальнее убийство", "%.0f м" % s.worst_kill_m),
                ("ПУЛ: отходов к дому в драке", "%d" % s.leash_returns),
                ("ПУЛ: доборов у дома", "%d" % s.topups)]
