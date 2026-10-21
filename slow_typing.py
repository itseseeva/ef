with open('tools/chat_bot_test.py', 'r', encoding='utf-8') as f:
    c = f.read()

import re

c = re.sub(
    r'(def type_unicode\(text: str, delay: float = 0.01\):\n(?:.*?\n)*?\s*time\.sleep\(delay\))',
    r'import random\n\ndef type_unicode(text: str, delay: float = -1.0):\n    """Печатает текст посимвольно (через WinAPI)."""\n    for char in text:\n        ki_down = KEYBDINPUT(0, ord(char), 0x0004, 0, None)\n        inp_down = INPUT(type=1, ki=ki_down)\n        ctypes.windll.user32.SendInput(1, ctypes.byref(inp_down), ctypes.sizeof(INPUT))\n        time.sleep(0.01)\n        \n        ki_up = KEYBDINPUT(0, ord(char), 0x0004 | 0x0002, 0, None)\n        inp_up = INPUT(type=1, ki=ki_up)\n        ctypes.windll.user32.SendInput(1, ctypes.byref(inp_up), ctypes.sizeof(INPUT))\n        \n        if delay < 0:\n            time.sleep(random.uniform(0.03, 0.09))\n        else:\n            time.sleep(delay)',
    c
)

with open('tools/chat_bot_test.py', 'w', encoding='utf-8') as f:
    f.write(c)
