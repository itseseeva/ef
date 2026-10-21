with open('src/core/input_manager.py', 'r', encoding='utf-8') as f:
    c = f.read()

import re

# Add chat_locked to init
c = re.sub(
    r'(def __init__\(self, backend=None\) -> None:)',
    r'\1\n        self._chat_locked = False',
    c
)

# Return early from press_button
c = re.sub(
    r'(def press_button\(self, action: str\) -> None:\n(?:.*?\n)*?)\s*self\._queue\.put',
    r'\1        if self._chat_locked: return\n        self._queue.put',
    c
)

# Return early from execute_combo
c = re.sub(
    r'(def execute_combo\(self.*?\n.*?-> None:\n(?:.*?\n)*?)\s*self\._queue\.put',
    r'\1        if self._chat_locked: return\n        self._queue.put',
    c
)

# Make is_idle return False if locked
c = re.sub(
    r'(def is_idle\(self\) -> bool:\n)',
    r'\1        if self._chat_locked: return False\n',
    c
)

# Add set_chat_lock
if 'def set_chat_lock' not in c:
    c += '''
    def set_chat_lock(self, locked: bool) -> None:
        self._chat_locked = locked
        if locked:
            self.cancel_pending_actions(cut_current_wait=True)
'''

with open('src/core/input_manager.py', 'w', encoding='utf-8') as f:
    f.write(c)
