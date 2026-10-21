"""
Песочница режима "пул" (src/core/pull.py, 2026-10-13) — без игры, экрана и ввода.
Запуск из корня проекта: python tools/pull_logic_test.py
Строки списка — искусственные (номер, метры, HP), проверяем фильтр целей и переходы фаз.
"""
import os
import sys
from types import SimpleNamespace as Row

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from src.core.pull import GATHER, KILL, RETURN, PullPlanner, PullTuning, TagAttempt  # noqa: E402

fails = 0



def legacy(**kw):
    """Планировщик для проверок фильтров/фаз, написанных до 2026-10-14: старт в фазе сбора и без зоны
    "бьём сразу у дома" (home_kill_m=0) — иначе их цели в 10-20 м от дома ушли бы в эту зону."""
    pp = PullPlanner(PullTuning(**{"home_kill_m": 0.0, **kw}))
    pp.phase = GATHER
    return pp

def check(name, ok, info=""):
    global fails
    print("%s %s %s" % ("PASS" if ok else "FAIL", name, "" if ok else info))
    fails += 0 if ok else 1


def home(p, now):
    """Пришли домой и (если паровоз недобран) сразу закончили добор — для проверок добивания."""
    p.on_home(now)
    if p.topup:
        p.finish_topup(now)


def row(i, d, hp=None):
    return Row(index=i, distance_m=d, hp=hp)


ROWS = [row(1, 3, 0.80),      # тычкнутый, бежит за нами
        row(2, 20),           # целый, близко
        row(3, 50),           # целый, далеко
        row(4, None),         # метры не прочитаны
        row(5, 30, 0.99)]     # целый (HP ~полное)

# --- фильтр целей ---
p = legacy()
check("F0: (проверки F) фаза сбора", p.phase == GATHER)
home(p, 0.0)
check("F1: KILL у дома — бьём только тычкнутого №1, целых (№2, №3, №5) и без HP (№4) не трогаем",
      p.excluded(ROWS, 0.0) == {2, 3, 4, 5}, p.excluded(ROWS, 0.0))
p.start_gather(0.0)
p.tags = 1                    # правило "не дальше 40 м от бота" — после первой тычки
ex = p.excluded(ROWS, 0.0)
check("F2: GATHER у дома — тычкнутый №1, без метров №4 и дальний №3 (50 м > 30 м от бота) мимо",
      ex == {1, 3, 4}, ex)
ex = p.excluded([row(7, 4), row(8, 12)], 0.0)
check("F2b: GATHER — строка в 4 м без HP (бежит за нами) мимо, 12 м — можно", ex == {7}, ex)
home(p, 0.0)
ex = p.excluded([row(7, 4), row(8, 12), row(9, 20, 0.5)], 0.0)
check("F2c: KILL — без HP в 4 м добиваем, без HP в 12 м нет, раненый в 20 м да", ex == {8}, ex)
p.start_gather(0.0)
p.tags = 1
ex = p.excluded(ROWS, 20.0)
check("F3: GATHER в 20 м от дома — №3 (50 м) мимо: 20+35=55 > 40; №5 (30 м): 20+15=35 — можно",
      ex == {1, 3, 4}, ex)
ex = p.excluded(ROWS, 58.0)
check("F4: GATHER на краю (58 м из 60) — №2 (20 м): 58+5=63 > 60 мимо, №5 тоже", ex == {1, 2, 3, 4, 5}, ex)
p.start_return(1.0)
check("F5: RETURN — никого не берём", p.excluded(ROWS, 10.0) == {1, 2, 3, 4, 5})

# --- сбор ---
p = legacy()
p.start_gather(0.0)
check("G1: тычек нет, кандидатов нет — домой не идём (ищем дальше)", not p.gather_should_return(1.0, 10.0, False))
check("G2: убежал на 65 м (> 60) — домой даже без тычек", p.gather_should_return(1.0, 65.0, True))
r = [p.on_tag(2.0 + 3 * i) for i in range(4)]
check("G3: 4-я тычка — сбор окончен, раньше — нет", r == [False] * 3 + [True], r)
p = legacy()
p.start_gather(0.0)
p.on_tag(1.0)
check("G4: 1 тычка, тыкать больше некого — домой", p.gather_should_return(2.0, 10.0, False))
check("G5: 1 тычка, кандидаты есть, время не вышло — собираем дальше", not p.gather_should_return(2.0, 10.0, True))
check("G6: 1 тычка, прошло 30 с — домой", p.gather_should_return(31.5, 10.0, True))
check("G7: 2-я тычка позже 30 с от первой — сбор окончен", p.on_tag(32.0))

# --- дома и учёт ---
p = legacy()
p.start_gather(0.0)
p.on_tag(1.0)
p.on_tag(2.0)
p.start_return(3.0)
home(p, 10.0)
check("H1: дома — фаза KILL, цикл посчитан", p.phase == KILL and p.stats.cycles == 1)
check("H2: убит у дома (5 м) — в зоне лута", p.on_kill(5.0) is True)
check("H3: убит в 50 м — вне зоны", p.on_kill(50.0) is False)
check("H4: у дома некого добивать 0.3 с — ещё ждём", not p.kill_idle(11.0, False) and not p.kill_idle(11.3, False))
check("H5: 0.5+ с — пора (проверка R, потом сбор)", p.kill_idle(11.55, False))
check("H6: тычкнули 2, у дома убили 2 — потерь 0", p.close_cycle() == 0 and p.stats.lost == 0)
p.start_gather(17.0)
check("H7: новый сбор — фаза GATHER, тычки с нуля", p.phase == GATHER and p.tags == 0)
p.on_tag(18.0)
p.on_tag(19.0)
p.start_return(20.0)
home(p, 25.0)
p.on_kill(3.0)
check("H8: тычкнули 2, добили 1 — потеряно 1 (не добежал/сбросился)", p.close_cycle() == 1 and p.stats.lost == 1)
pairs = dict(p.summary_pairs())
check("H9: итог — 3 убийства, 2 в зоне", pairs["ПУЛ: убито в 35 м от дома"].startswith("2 из 3"), pairs)
# --- P: поводок в добивании (20 м от дома) ---
p = legacy()
p.start_return(0.0)
home(p, 1.0)
check("P1: добивание в 15 м — поводок не тянет", not p.kill_too_far(15.0))
check("P2: добивание в 16 м — назад к дому (поводок 15 м)", p.kill_too_far(16.0) and not p.kill_too_far(14.0))
ex = p.excluded([row(1, 20, 0.5), row(2, 20, 0.5), Row(index=3, distance_m=25, hp=None, attacking=True)], 5.0)
check("P2b: бот в 5 м от дома: раненый в 20 м (=25 от дома) бьём, нападающий в 25 м (=30) бьём", ex == set(), ex)
ex = p.excluded([row(1, 30, 0.5), Row(index=3, distance_m=28, hp=None, attacking=True)], 5.0)
check("P2c: раненый в 30 м (=35 от дома) — не бьём; нападающий в 28 м — бьём (дом зачищаем от всех, кто бьёт)",
      ex == {1}, ex)
ex = p.excluded([row(1, 12, 0.6), Row(index=3, distance_m=45, hp=None, attacking=True)], 22.0)
check("P2d: бот в 22 м от дома: раненый вплотную (12 м) — бьём; 'бьющий' в 45 м (дальше 40) — нет", ex == {3}, ex)
check("P2e: цель бьёт нас (мечи) — не бросаем, даже если по оценке вне зоны",
      not p.target_left_zone(Row(index=1, distance_m=30, hp=0.5, attacking=True), 10.0))
check("P2f: цель в 17 м от бота (бот в 16 м от дома) — не 'ушла', это не бегство", not p.target_left_zone(row(1, 17), 16.0))
p.cycle_kills = 2
p.start_return(2.0, leash=True)
home(p, 4.0)
check("P3: возврат по поводку — снова KILL, цикл тот же (циклов 1, убийства цикла сохранены)",
      p.phase == KILL and p.stats.cycles == 1 and p.cycle_kills == 2 and p.stats.leash_returns == 1,
      (p.phase, p.stats.cycles, p.cycle_kills, p.stats.leash_returns))
p.start_gather(5.0)
check("P4: на сборе поводок добивания не действует (там свой — run_m)", not p.kill_too_far(35.0))

# --- S: скиллы по площади и ушедшая цель ---
p = legacy()
p.start_return(0.0)
home(p, 1.0)
check("S1: у дома 3 моба вплотную (0-6 м) + один в 15 м — рядом 3", p.aoe_now([row(1, 0), row(2, 3), row(3, 6), row(4, 15)]) == 3)
check("S2: цель в 10 м от бота, бот в 5 м от дома — в зоне", not p.target_left_zone(row(1, 10), 5.0))
check("S3: цель ушла на 30 м от бота (бот в 5 м, итого 35 > 30+3) — бросаем", p.target_left_zone(row(1, 30), 5.0))
p.start_gather(2.0)
check("S4: на сборе — ни площади, ни 'ушла за зону'", p.aoe_now([row(1, 0), row(2, 1)]) == 0
      and not p.target_left_zone(row(1, 60), 5.0))
from src.core.rotation import PriorityRotation  # noqa: E402
chain = {"name": "Ротация", "independent": False, "gamepad_steps": [["press_1"], ["press_3"], ["holdfor_1_="]],
         "cast_times": [None, None, None], "step_cds": [5, 12, 10]}
rot = PriorityRotation.from_chains([chain])
check("S5: без приоритета — по порядку цепочки: '1'", rot.choose(0.0)[0].label == "1")
check("S6: куча у дома (prefer 3, 4, =) — первым '3' по площади",
      rot.choose(0.0, prefer={"3", "4", "="})[0].label == "3")
rot.fired(rot.actions[1], 0.0)
check("S7: '3' на откате — следующий по площади '='",
      rot.choose(1.0, prefer={"3", "4", "="})[0].label.startswith("="))

# --- U: сбор — безнадёжная цель и бережём скиллы по площади ---
p = legacy()
p.start_gather(0.0)
def hopeless(seq):
    """seq — [(время, метры)]: прогон одной попытки тычки, ответ на последнем шаге."""
    at, why = TagAttempt(entered_at=0.0), None
    for t, dm in seq:
        why = p.tag_hopeless(row(1, dm), t, at)
    return why


check("U1: цель в 20 м, 2 с после выбора — ждём тычку", hopeless([(0.0, 20), (2.0, 20)]) is None)
check("U2: цель убежала на 55 м (> 50) — бросаем", hopeless([(0.0, 40), (1.0, 55)]) is not None)
check("U3: 7.1 с без тычки (а сближались) — потолок, бросаем",
      hopeless([(0.0, 40), (2.0, 37), (4.0, 34), (6.0, 31), (7.1, 29)]) is not None)
check("U3b: 24 м и через 2.7 с всё ещё 24 м — персонаж не бежит, бросаем", hopeless([(0.0, 24), (2.7, 24)]) is not None)
check("U3b2: 24 м -> 24 м за 1.6 с — ещё ждём (первый E часто не ведёт, повтор ~0.9 с)",
      hopeless([(0.0, 24), (1.6, 24)]) is None)
check("U3c: 21 -> 15 -> 18 -> 17 м за 5 с (лог 00:32:40, №2) — сближались 2.5 с назад, ждём, не бросаем",
      hopeless([(0.0, 21), (1.5, 15), (3.0, 18), (5.0, 17)]) is None)
check("U3d: цель в 12 м (в дистанции удара), не сближаемся — это нормально, ждём",
      hopeless([(0.0, 12), (2.7, 12)]) is None)
check("U4: на сборе скиллы по площади и долгое '8' бережём", p.aoe_keys_now() == {"3", "4", "=", "8"})
p.start_return(1.0)
check("U5: по дороге домой — тоже бережём", p.aoe_keys_now() == {"3", "4", "=", "8"})
home(p, 2.0)
check("U6: у дома — можно", p.aoe_keys_now() is None)
# --- Q: важные скиллы 8 и 9 — первыми ---
qc = [{"name": "6", "independent": True, "gamepad_steps": [["press_6"]], "cast_times": [None], "step_cds": [5]},
      {"name": "Ротация", "independent": False,
       "gamepad_steps": [["press_1"], ["press_3"], ["holdfor_6_8"], ["press_9"]],
       "cast_times": [None, None, None, None], "step_cds": [5, 5, 47, 36]}]
qr = PriorityRotation.from_chains(qc)
check("Q1: всё готово — первым '6' (Постоянно)", qr.choose(0.0)[0].label == "6")
qr.fired(qr.actions[0], 0.0)
pick = qr.choose(0.9)
check("Q2: сразу после '6' — окно: '8' (зажатие 6 с) раньше '1' по порядку", pick[0].label.startswith("8"), pick)
qr.fired(pick[0], 0.9)
pick = qr.choose(7.0)
check("Q3: дальше '6' (откатилась), потом '9' раньше '1'", pick[0].label == "6", pick)
qr.fired(pick[0], 7.0)
pick = qr.choose(7.9)
check("Q3b: '9' раньше '1' и '3'", pick[0].label == "9", pick)
check("Q4: на сборе '8' бережём (avoid) — важный не обходит запрет",
      not PriorityRotation.from_chains(qc).choose(0.0, avoid={"8"})[0].label.startswith("8"))
qd = PriorityRotation.from_chains(qc)
moved = qd.delay_keys(qd.priority, 29.0)
check("Q5: после Старта важные (8, 9) придержаны до 29 с, '6' и прочие — нет",
      sorted(moved) == ["8(зажать)", "9"] and qd.actions[0].next_by_timer < 29.0, moved)
seen = []
tq, busy = 0.0, 0.0
while tq < 40.0:
    if tq >= busy:
        pk = qd.choose(tq)
        if isinstance(pk, tuple):
            qd.fired(pk[0], tq)
            busy = tq + qd.busy_now(pk[0])
            seen.append((round(tq, 1), pk[0].label))
    tq += 0.05
first89 = min(t for t, lab in seen if lab[0] in "89")
check("Q6: в бою с первой секунды — '8'/'9' не раньше 29 с, потом сразу", 29.0 <= first89 < 31.0, first89)
rot2 = PriorityRotation.from_chains([chain])
check("U7: ротация с avoid {3,4,=} не жмёт '3', '=' — только '1'",
      rot2.choose(0.0, avoid={"3", "4", "="})[0].label == "1")
rot2.fired(rot2.actions[0], 0.0)
check("U8: '1' на откате, остальные под запретом — жать нечего (не '3')",
      rot2.choose(1.0, avoid={"3", "4", "="}) in (None,) or rot2.choose(1.0, avoid={"3", "4", "="}).__class__.__name__ != "tuple")

# --- D: добор у дома (по умолчанию выключен — 2026-10-13: "< 4 — пулит тех, кто есть") ---
p0 = legacy()
p0.start_gather(0.0)
p0.on_tag(1.0, 1, 10)
p0.start_return(2.0)
p0.on_home(4.0)
check("D0: по умолчанию добора нет — 1 тычка из 4, дома сразу добивание", p0.phase == KILL and not p0.topup)
p = legacy(topup_enabled=True)
p.start_gather(0.0)
p.on_tag(1.0, 1, 10)
p.on_tag(2.0, 2, 12)
p.start_return(3.0)
p.on_home(6.0)
check("D1: пришёл с 2 тычками из 4 — добор у дома (фаза сбора, тычки и память сохранены)",
      p.phase == GATHER and p.topup and p.tags == 2 and 1 in p.tagged and p.stats.topups == 1)
ex = p.excluded([row(5, 20), row(6, 28), row(1, 10)], 5.0)
check("D2: добор — моб в 20 м (=25 от дома) тычем, в 28 м (=33) нет, тычкнутый №1 нет", ex == {6, 1}, ex)
check("D3: 4-я тычка в доборе — сбор окончен", not p.on_tag(7.0, 5, 20) and p.on_tag(8.0, 7, 15))
p.finish_topup(8.0)
check("D4: добор закончен — добивание, без бега домой", p.phase == KILL and not p.topup)
p2 = legacy(topup_enabled=True)
p2.start_gather(0.0)
for i in range(4):
    p2.on_tag(float(i), i + 1, 10)
p2.start_return(5.0)
p2.on_home(6.0)
check("D5: пришёл с полным паровозом (4) — сразу добивание", p2.phase == KILL and not p2.topup)

# --- B: напавшие сами — часть паровоза ---
p = legacy()
p.start_gather(0.0)
atk = [Row(index=i, distance_m=5, hp=None, attacking=True, selected=False) for i in (6, 7)]
check("B1: тычек 0, напали сами 2 — в паровозе 2", p.train(atk) == 2)
check("B2: на сборе напавших не тычем (они и так едут домой)", p.excluded(atk, 0.0) == {6, 7})
p.on_tag(1.0, 3, 10)
p.on_tag(2.0, 6, 5)
check("B3: тычкнули №3 и №6 (№6 и так с мечами) + напал №7 — паровоз 3, без двойного счёта", p.train(atk) == 3)
p.start_return(3.0)
p.t.topup_enabled = True
p.on_home(5.0, train=4)
check("B4: дома с паровозом 4 (2 тычки + 2 напавших) — сразу добивание, без добора", p.phase == KILL and not p.topup)

# --- E: орлиное зрение (R) ---
p = legacy()
check("E1: старт — нужно R перед выбором целей", p.need_eagle)
p.on_tag(0.0, 3, 10)
p.on_eagle(1.0)
check("E2: нажали R — номера в памяти сброшены, снимка нет", not p.tagged and not p.need_eagle and p.plan is None)
check("E3: 0.1 с после R — таблицу не читаем", not p.eagle_settled(1.1, [row(1, 20)]))
check("E3b: 0.25 с — таблица пустая (перерисовка) — это НЕ 'мобов нет', ждём", not p.eagle_settled(1.25, []))
check("E3c: строки появились — ещё чтение, пока дочитаются метры", not p.eagle_settled(1.3, [row(1, 20), row(3, None)]))
check("E3d: через 0.12 с — готово", p.eagle_settled(1.43, [row(1, 20), row(2, 25), row(3, 30)]))
check("E4: снимок — все строки, что видели после R", p.take_plan() == {1, 2, 3})
ex = p.excluded([row(1, 20), row(2, 25), row(3, 30), row(6, 15)], 0.0)
check("E4b: моб №6 появился после снимка — в этом сборе не тычем", ex == {6}, ex)
p.on_tag(2.0, 1, 18)
p.drop_from_plan(2)
ex = p.excluded([row(1, 15), row(2, 25), row(3, 30)], 0.0)
check("E4c: №1 тычкнут, №2 брошен — остался №3", ex == {1, 2}, ex)
check("E5: тычки есть — повторного R нет (домой с тем, что есть)", not p.eagle_empty_retry(9.0))
q = legacy()
q.start_gather(0.0)
q.on_eagle(0.0)
check("E5b: после R таблица так и пустая 1 с — готово (пусто)", not q.eagle_settled(0.85, []) and q.eagle_settled(1.01, []))
q.take_plan()
check("E5c: пусто, тычек 0 — R снова не раньше чем через 2 с", not q.eagle_empty_retry(1.5) and q.eagle_empty_retry(2.0))
p.start_gather(5.0)
check("E6: новый сбор — снова R первым делом, снимок заново", p.need_eagle and p.plan is None)
q = legacy()
q.start_gather(0.0)
q.on_eagle(0.0)
q.eagle_settled(0.61, [])
q.take_plan()
check("E7: снимок пустой, через 0.2 с строки появились — берём их без второго R",
      q.adopt_rows([row(1, 14), row(2, 13)]) and q.plan == {1, 2})
check("E7b: непустой снимок не перезаписываем", not q.adopt_rows([row(5, 10)]) and q.plan == {1, 2})
q.on_tag(1.0, 1, 12)
check("E8: в снимке остался №2 — не исчерпан", not q.plan_exhausted())
q.drop_from_plan(2)
check("E9: №1 тычкнут, №2 брошен — снимок исчерпан, домой сразу", q.plan_exhausted())

# --- F: прицепившиеся на сборе — в паровозе ---
p = legacy()
p.start_gather(0.0)
p.on_tag(0.5, 1, 10)
near = [row(1, 3), row(3, 4), row(4, 20)]
p.note_followers(near, 1.0)
check("F1: №3 рядом 0 с — ещё не прицепился", p.train(near) == 1 and 3 not in p.followers)
p.note_followers([row(1, 3), row(3, 9), row(4, 20)], 1.5)
p.note_followers(near, 2.0)
check("F2: отошёл и вернулся — отсчёт заново", 3 not in p.followers and p.near_since.get(3) == 2.0)
p.note_followers(near, 3.6)
check("F3: рядом 1.6 с — прицепился: паровоз 2 (тычка №1 + №3), №3 не тычем",
      3 in p.followers and p.train(near) == 2 and 3 in p.excluded([row(3, 12)], 0.0), p.train(near))
p.on_tag(4.0, 4, 18)
p.on_tag(5.0, 5, 15)
check("F4: 3 тычки + прицепившийся = 4 — паровоз полон", p.train(near + [row(5, 5)]) == 4)
p.start_gather(9.0)
check("F5: новый сбор — прицепившиеся забыты", not p.followers and not p.near_since)
# --- S: чужая цель (HP 100%, без мечей) ---
p = legacy()
p.phase = KILL
sel = Row(index=4, distance_m=5, hp=None, attacking=False, selected=True)
check("S1: у дома HP 100%, строки нет — чужой", p.stranger(100.0, None))
check("S2: HP 100%, строка без мечей — чужой", p.stranger(100.0, sel))
check("S3: HP 98% — наш (тычкнут)", not p.stranger(98.0, sel))
check("S4: HP 100%, но мечи — бьёт нас, наш", not p.stranger(100.0, Row(index=4, distance_m=5, hp=None, attacking=True, selected=True)))
p.phase = RETURN
check("S5: по дороге домой правило не действует", not p.stranger(100.0, None))

# --- Z: мобы у самого дома — не водим, бьём сразу ---
p = PullPlanner()
check("Z0: старт — с проверки 'дома пусто?' (KILL), а не сразу сбор", p.phase == KILL)
p.phase = KILL
check("Z1a: ДО пула целый моб у дома (15 м) не бьём — его тычем на сборе", not p._kill_ok(row(4, 15), 3.0))
check("Z1b: до пула и без HP в 6 м — тоже не бьём (тычем)", not p._kill_ok(row(4, 6), 3.0))
check("Z1c: до пула — бьёт нас (мечи) — бьём", p._kill_ok(Row(index=4, distance_m=6, hp=None, attacking=True, selected=False), 3.0))
p.pulled = True
check("Z1: ПОСЛЕ пула у дома целый моб в 15 м (3+15=18 <= 20) — бьём (расчистка)", p._kill_ok(row(4, 15), 3.0))
check("Z2: целый в 25 м (худшая 28 > 20) — не трогаем, его тычем на сборе", not p._kill_ok(row(4, 25), 3.0))
check("Z3: у дома целый в 15 м выделен — не 'чужой'",
      not p.stranger(100.0, Row(index=4, distance_m=15, hp=None, attacking=False, selected=True), 3.0))
p.start_gather(1.0)
ex = p.excluded([row(4, 15), row(5, 25)], 3.0)
check("Z4: на сборе моба у дома тычем вместе с остальными (расчистка — после пула)", ex == set(), ex)
check("Z4b: новый сбор — 'после пула' сброшено", not p.pulled)
check("Z5: на сборе выделенный целый у дома — чужой (не наш сбор)",
      p.stranger(100.0, Row(index=4, distance_m=15, hp=None, attacking=False, selected=True), 3.0))

# --- W: сначала расчистить дом ---
p = PullPlanner()
check("W1: у дома бьёт нас (мечи, 8 м) — дом не расчищен",
      p.home_busy([Row(index=2, distance_m=8, hp=None, attacking=True, selected=False)], 10.0))
check("W2a: до пула целый у дома — не повод расчищать (его тычем)", not p.home_busy([row(1, 9)], 10.0))
p.pulled = True
check("W2: после пула у самого дома целый (10+9=19 <= 20) — дом не расчищен", p.home_busy([row(1, 9)], 10.0))
p.pulled = False
check("W3: все далеко (55-88 м) — дом чист", not p.home_busy([row(1, 59), row(2, 74), row(5, 55)], 0.0))
check("W4: выбранную цель можно не считать",
      not p.home_busy([Row(index=2, distance_m=8, hp=None, attacking=True, selected=True)], 10.0, skip_selected=True))
p.start_gather(0.0)
ex = p.excluded([row(1, 59), row(5, 55), row(2, 74)], 0.0)
check("W5: первая цель на сборе — и в 55-59 м (правило 40 м — только после первой тычки)", ex == set(), ex)
p.on_tag(1.0, 5, 50)
check("W6: после первой тычки дальше 40 м от бота — мимо", 1 in p.excluded([row(1, 59)], 0.0))
p2 = PullPlanner()
p2.start_gather(0.0); p2.on_eagle(0.0); p2.eagle_settled(1.05, []); p2.take_plan()
check("W7: R пустой — мобы в таблице есть (далеко): R через 5 с, пустая — через 2 с",
      not p2.eagle_empty_retry(3.0, True) and p2.eagle_empty_retry(5.1, True) and p2.eagle_empty_retry(2.1, False))

# --- C: поза камеры для R (камера вниз на персонажа) ---
from src.core.camera_pitch import PitchGuard  # noqa: E402
import random as _rnd  # noqa: E402

g = PitchGuard(rng=_rnd.Random(1))
g.p = 45 * g.k                                # откалибрована, смотрим на 45° от нижнего упора
g.start_scan()
moved, t = 0.0, 0.0
while not g.at_floor and t < 2.0:
    moved += g.calib_step(0.005)
    t += 0.005
check("C1: поза для R — камера вниз до упора (запас 45°) и стоит; < 0.8 с",
      g.at_floor and g.p == 0.0 and moved >= 45 * g.k and t < 0.8, (round(t, 2), moved))
g.release_scan(0.15)
up, t2 = 0.0, 0.0
while g.calibrating and t2 < 3.0:
    up -= g.calib_step(0.005)
    t2 += 0.005
check("C2: после R камера назад в стандартные 45° (сначала 0.15 с стоит)", not g.calibrating and abs(up - 45 * g.k) < 2, up)
g2 = PitchGuard(rng=_rnd.Random(2))
g2.p = 45 * g2.k
g2.start_scan()
for _ in range(900):
    g2.calib_step(0.005)
check("C3: R так и не нажали — в упоре не дольше 3 с, потом назад", not g2.at_floor)
g3 = PitchGuard(rng=_rnd.Random(3))
g3.p = 45 * g3.k
g3.start_scan()
g3.release_scan(0.0)                          # отменили, пока ехали вниз
for _ in range(200):
    g3.calib_step(0.005)
check("C4: отмена на пути вниз — в упоре не стоим, сразу назад", not g3.at_floor)
pc = PullPlanner()
pc.eagle_pending = True
check("C5: пока камера едет вниз — таблицу после R не читаем", not pc.eagle_settled(10.0, [row(1, 10)]))

# --- R: шаг к центру дома перед R ---
p = PullPlanner()
p.phase = KILL
p.start_return(1.0, center=True)
p.on_home(3.0)
check("R1b: шаг к центру — не 'паровоз привезли'", not p.pulled)
check("R1: дошли к центру — снова проверка у дома (KILL), без счёта циклов/поводка",
      p.phase == KILL and not p.center_return and p.stats.cycles == 0 and p.stats.leash_returns == 0)

# --- V: проверка "дома пусто" свежей таблицей ---
p = legacy()
p.phase = KILL
p.on_eagle(0.0)
p.verifying = True
check("V1: и во время проверки чужую цель (HP 100%, без мечей) не берём", p.stranger(100.0, None))
p.eagle_settled(0.25, [row(3, 50)])
p.eagle_settled(0.4, [row(3, 50), row(5, 30)])
p.start_gather(0.4, fresh=True)
check("V2: дома пусто — сбор по той же таблице, второй R не нужен",
      p.phase == GATHER and not p.need_eagle and p.plan == {3, 5} and not p.verifying)
p.start_gather(9.0)
check("V3: обычный новый сбор — сначала R", p.need_eagle and p.plan is None)

# --- N: память номеров тычкнутых строк ---
p = legacy()
p.start_gather(0.0)
p.on_tag(1.0, 2)
check("N1: строка №2 тычкнута — на сборе её больше не берём, даже без HP в строке", 2 in p.excluded([row(2, 15)], 0.0))
p.mark_tagged(5)
check("N2: 'уже тычкнута' по панели — №5 тоже в памяти", 5 in p.excluded([row(5, 15)], 0.0))
p.start_gather(5.0)
check("N3: новый сбор — память чистая", p.excluded([row(2, 15), row(5, 15)], 0.0) == set())
p.on_tag(10.0, 2, 15)
p.track([row(2, 10)], 10.3)
check("N4: №2 был в 15 м, через 0.3 с в 10 м (бежит к нам) — тот же моб, помним", 2 in p.tagged)
p.track([row(2, 40)], 10.5)
check("N5: ложное чтение 'под №2 40 м' — номер НЕ забываем (без R номера стабильны, лог 00:32:37)", 2 in p.tagged)
p.on_tag(11.0, 3, 20)
p.track([], 12.0)
check("N6: строки №3 нет 1 с — ещё помним (мигнула)", 3 in p.tagged)
p.track([], 13.5)
check("N7: нет 2.5 с — забыли (умер/ушёл из скана)", 3 not in p.tagged)

# --- A: значок "бьёт нас" в строке списка и правила пула для таких мобов ---
try:
    import cv2
    import numpy as np
    from src.core.vision import target_list as T
except ImportError as e:                      # без numpy/opencv блок A пропускаем, остальное уже проверено
    print("SKIP A: нет numpy/opencv (%s)" % e)
else:
    def cell_with(badge):
        """Ячейка строки 33x198 (тёмно-синий фон) с нарисованным значком в BADGE_RECT."""
        c = np.full((T.CELL_H, T.CELL_W, 3), (45, 30, 30), np.uint8)
        x, y, w, h = T.BADGE_RECT
        badge(c[y:y + h, x:x + w])
        return c

    def icon(b):                              # тёмно-красный круг + два светлых клинка крестом
        cv2.circle(b, (11, 11), 10, (40, 30, 110), -1)
        cv2.line(b, (3, 3), (18, 18), (150, 190, 215), 2)
        cv2.line(b, (18, 3), (3, 18), (150, 190, 215), 2)

    def digit(color):
        return lambda b: cv2.putText(b, "4", (5, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)

    check("A1: мечи на красном круге — моб бьёт нас", T._attacking(cell_with(icon)) is True)
    check("A2: жёлтый номер — нет", T._attacking(cell_with(digit((50, 190, 230)))) is False)
    check("A3: красный номер агрессивного моба — нет", T._attacking(cell_with(digit((40, 90, 230)))) is False)

    A = Row(index=6, distance_m=28, hp=None, attacking=True, selected=False)
    p = legacy()
    check("A4: сбор не начат, нас бьёт №6 — attacked() = 1", p.attacked([A, row(2, 10)]) == 1)
    S = Row(index=7, distance_m=12, hp=None, attacking=True, selected=True)
    check("A5: мечи у ВЫБРАННОЙ цели (это мы её бьём) — не нападение", p.attacked([S]) == 0)
    p.start_kill(0.0)
    check("A6: KILL — бьющего нас дальнобойного в 28 м добиваем (в зоне лута)", 6 not in p.excluded([A], 0.0))
    p.start_gather(0.0)
    p.on_tag(1.0)
    check("A7: сбор, тычка была — мечи = наш паровоз, второй раз не тычем", 6 in p.excluded([A], 0.0))

print("FAILS", fails)
sys.exit(1 if fails else 0)
