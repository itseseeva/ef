"""
Песочница проверки зрения (src/core/vision_check.py, 2026-10-12) — без игры и экрана.
Запуск из корня проекта: python tools\\vision_check_test.py
Подаём искусственное время (тики по 1/60 с) и флаги "видно/не видно", сверяем вердикты.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from src.core.vision_check import LIST, MAP, PANEL, VisionCheck  # noqa: E402

fails = 0


def check(name, ok, info=""):
    global fails
    print("%s %s %s" % ("PASS" if ok else "FAIL", name, "" if ok else info))
    fails += 0 if ok else 1


def run(vc, seconds, list_seen=False, map_ok=False, reads_per_s=7, t0=0.0, step=1 / 60):
    """Тики бота: возвращает все события и время конца. Чтения списка — с t0 (как в боте:
    счётчик за всё время, а не за этот отрезок)."""
    ev, t, reads = [], t0, reads_per_s * t0
    while t < t0 + seconds:
        reads += reads_per_s * step
        ev += vc.tick(t, list_seen, int(reads), map_ok)
        t += step
    return ev, t


# 1. Всё видно сразу — никаких провалов, панель ждёт выбора цели.
vc = VisionCheck()
ev, _ = run(vc, 2.0, list_seen=True, map_ok=True)
check("V1: список и карта видны — событий нет", ev == [] and vc.status[LIST] and vc.status[MAP], ev)
check("V2: панель до первого выбора цели — ждёт (None)", vc.status[PANEL] is None)
vc.on_panel()
check("V3: панель нашлась — всё OK, тик дальше ничего не делает", vc.all_ok)

# 2. Список не виден — провал не раньше 6 с активной игры.
vc = VisionCheck()
ev, t = run(vc, 5.5, map_ok=True)
check("L1: 5.5 с без списка — ещё не провал", ev == [] and vc.status[LIST] is None, ev)
ev, t = run(vc, 1.0, map_ok=True, t0=t)
check("L2: после 6 с — провал списка, подсказка в интерфейс", ev == [("fail", LIST)] and len(vc.warnings()) == 1, ev)
ev, _ = run(vc, 0.5, list_seen=True, map_ok=True, t0=t)
check("L3: игрок поправил — 'fixed', подсказка ушла", ev == [("fixed", LIST)] and vc.warnings() == [], ev)

# 3. Игра свёрнута (большая пауза между тиками) — окно проверки не сгорает.
vc = VisionCheck()
vc.tick(0.0, False, 0, False)
ev = vc.tick(60.0, False, 100, False)
check("P1: пауза 60 с (игра свёрнута) — засчитано 0.25 с, не провал", ev == [] and vc.active_s <= 0.25 + 1e-9, (ev, vc.active_s))

# 4. Бот сразу в бою (список почти не читался) — список не проваливаем.
vc = VisionCheck()
ev, _ = run(vc, 10.0, map_ok=True, reads_per_s=0.5)
check("R1: 10 с, но чтений списка < 10 — не провал", vc.status[LIST] is None, (ev, vc.status))

# 5. Миникарта не читается.
vc = VisionCheck()
ev, _ = run(vc, 7.0, list_seen=True)
check("M1: карта не читается 6+ с — провал карты", ev == [("fail", MAP)], ev)

# 6. Панель: 3 выбора цели без панели — провал; потом панель нашлась — исправлено.
vc = VisionCheck()
r = [vc.on_list_click() for _ in range(3)]
check("N1: 1-2 выбора без панели — ещё ждём", r[0] == [] and r[1] == [], r)
check("N2: 3-й выбор без панели — провал панели", r[2] == [PANEL] and vc.status[PANEL] is False, r)
check("N3: дальше выборы не дублируют провал", vc.on_list_click() == [])
check("N4: панель нашлась — первое подтверждение", vc.on_panel() is True and vc.status[PANEL] is True)
check("N5: повторный вход в бой — не новость", vc.on_panel() is False)

# 7. Отключённая функция (нет миникарты) — в отчёт не идёт.
vc = VisionCheck()
vc.skip(MAP)
ev, _ = run(vc, 7.0, list_seen=True)
check("S1: skip(MAP) — провала карты нет", ev == [] and vc.status[MAP] is True, ev)

print("FAILS", fails)
sys.exit(1 if fails else 0)
