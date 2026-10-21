with open('src/core/input_manager.py', 'r', encoding='utf-8') as f:
    c = f.read()

import re

# 1. Update set_chat_lock to release all keys
c = re.sub(
    r'(def set_chat_lock\(self, locked: bool\) -> None:\n\s*self\._chat_locked = locked\n\s*if locked:\n\s*self\.cancel_pending_actions\(cut_current_wait=True\))',
    r'\1\n            self._release_all()',
    c
)

# 2. Add chat_locked check in _do_execute_combo loop
c = re.sub(
    r'(for action in actions:\n\s*verb, _, target = action\.partition\("_"\))',
    r'for action in actions:\n              if getattr(self, "_chat_locked", False):\n                  break\n              verb, _, target = action.partition("_")',
    c
)

# 3. Add chat_locked check in movement_key
c = re.sub(
    r'(def movement_key\(self, name: str, down: bool\) -> bool:\n(?:.*?\n)*?\s*target = self\.INPUT_MAP\.get\(name\))',
    r'\1\n        if getattr(self, "_chat_locked", False): return False',
    c
)

# 4. Add chat_locked check in move
c = re.sub(
    r'(def move\(self, direction: str, duration_s: float\) -> None:\n(?:.*?\n)*?\s*key = self\._MOVE_KEYS\.get\(direction\))',
    r'\1\n        if getattr(self, "_chat_locked", False): return',
    c
)

# 5. Add chat_locked check in movement_blocked
c = re.sub(
    r'(def movement_blocked\(self\) -> "str \| None":\n(?:.*?\n)*?\s*if self\._abort_event\.is_set\(\) or self\._stop_event\.is_set\(\):\n\s*return ".*?\"\n)',
    r'\1        if getattr(self, "_chat_locked", False):\n            return "чат"\n',
    c
)

with open('src/core/input_manager.py', 'w', encoding='utf-8') as f:
    f.write(c)
