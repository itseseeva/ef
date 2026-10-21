with open('src/core/input_manager.py', 'r', encoding='utf-8') as f:
    c = f.read()

import re
c = re.sub(
    r'(def execute_combo_now\(self.*?-> None:\n(?:.*?\n)*?)\s*item = \("execute_combo"',
    r'\1        if getattr(self, "_chat_locked", False): return\n        item = ("execute_combo"',
    c
)

with open('src/core/input_manager.py', 'w', encoding='utf-8') as f:
    f.write(c)
