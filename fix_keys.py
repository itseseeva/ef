with open('src/core/input_manager.py', 'r', encoding='utf-8') as f:
    c = f.read()

c = c.replace('and name != "enter": return', 'and name not in ("enter", "esc", "space"): return')

with open('src/core/input_manager.py', 'w', encoding='utf-8') as f:
    f.write(c)
