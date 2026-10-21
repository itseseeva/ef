with open('src/core/input_manager.py', 'r', encoding='utf-8') as f:
    c = f.read()

import re

c = re.sub(
    r'(def _down\(self, name: str\) -> bool:\n(?:.*?\n)*?\s*target = self\.INPUT_MAP\.get\(name\))',
    r'\1\n        if getattr(self, "_chat_locked", False) and name != "enter": return False',
    c
)

c = re.sub(
    r'(def _up\(self, name: str\) -> None:\n(?:.*?\n)*?\s*target = self\.INPUT_MAP\.get\(name\))',
    r'\1\n        if getattr(self, "_chat_locked", False) and name != "enter": return',
    c
)

c = re.sub(
    r'(def _tap\(self, name: str\) -> bool:\n(?:.*?\n)*?\s*target = self\.INPUT_MAP\.get\(name\))',
    r'\1\n        if getattr(self, "_chat_locked", False) and name != "enter": return False',
    c
)

with open('src/core/input_manager.py', 'w', encoding='utf-8') as f:
    f.write(c)
