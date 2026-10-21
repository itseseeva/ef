"""
Песочница (2026-10-12): решение "домой или следующий моб" после убийства — без игры.
Методы FarmBot берём из bot.py как есть (ast), навигатор — заглушка. Запуск из корня проекта: python tools\\home_due_test.py
"""
import ast, logging, os, random, sys
src = open(sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", "src", "core", "bot.py"), encoding="utf-8-sig").read()
cls = next(n for n in ast.parse(src).body if isinstance(n, ast.ClassDef) and n.name == "FarmBot")
want = {"_home_due", "_home_trip_active", "_schedule_home", "_start_home_trip"}
body = [n for n in cls.body if (isinstance(n, ast.FunctionDef) and n.name in want)
        or (isinstance(n, ast.Assign) and any(getattr(t, "id", "").startswith("_HOME_") for t in n.targets))]
ns = {"gamepad_debug_logger": logging.getLogger("t"), "random": random}
exec(compile(ast.fix_missing_locations(ast.Module([ast.ClassDef("Bot", [], [], body, [])], [])), "m", "exec"), ns)
Bot = ns["Bot"]
assert src.count("if self._home_due(now):\n                    self._start_home_trip(now)") == 1, "guard после убийства"

class Nav:
    def __init__(s, d): s.d = d
    def available(s, now): return True
    def dist_home_m(s): return s.d

fails = 0
def check(n, ok):
    global fails; print("PASS" if ok else "FAIL", n); fails += not ok

def make(d, next_at):
    b = Bot(); b.movement = object(); b.navigator = Nav(d); b._next_home_at = next_at
    b._home_trip_deadline = None; b.calls = []
    b._enter_roam = lambda now, why, home=False: b.calls.append(home)
    return b

b = make(82.0, 1e9)
check("H1: 82 м > поводка 40 м — домой, хотя таймер не подошёл", b._home_due(0.0))
b._start_home_trip(0.0)
check("H2: поход начат — ROAM домой", b.calls == [True] and b._home_trip_active())
check("H3: после боя по дороге поход продолжается", b._home_due(5.0))
b = make(10.0, 1e9)
check("H4: 10 м, таймер не подошёл — следующий моб", not b._home_due(0.0))
b = make(10.0, 0.0)
check("H5: 10 м, таймер подошёл — домой", b._home_due(1.0))
b = make(5.0, 0.0)
check("H6: уже дома (5 м <= 8) — не идём, таймер перенесён на 60 с", not b._home_due(1.0) and b._next_home_at > 60.0)
print("FAILS", fails)
