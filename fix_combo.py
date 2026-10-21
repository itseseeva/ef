with open('src/core/input_manager.py', 'r', encoding='utf-8') as f:
    c = f.read()

parts = c.split('def execute_combo_now(self, actions:')
if len(parts) > 1:
    subparts = parts[1].split('        item = ("execute_combo",', 1)
    if len(subparts) > 1:
        c = parts[0] + 'def execute_combo_now(self, actions:' + subparts[0] + '        if getattr(self, "_chat_locked", False): return\n        item = ("execute_combo",' + subparts[1]
        with open('src/core/input_manager.py', 'w', encoding='utf-8') as f:
            f.write(c)
        print("Fixed execute_combo_now")
