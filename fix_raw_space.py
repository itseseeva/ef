with open('tools/chat_bot_test.py', 'r', encoding='utf-8') as f:
    c = f.read()

import re

new_func = '''def press_space_key():
    VK_SPACE = 0x20
    ki_down = KEYBDINPUT(VK_SPACE, 0, 0, 0, None)
    ctypes.windll.user32.SendInput(1, ctypes.byref(INPUT(type=1, ki=ki_down)), ctypes.sizeof(INPUT))
    time.sleep(0.05)
    ki_up = KEYBDINPUT(VK_SPACE, 0, 0x0002, 0, None)
    ctypes.windll.user32.SendInput(1, ctypes.byref(INPUT(type=1, ki=ki_up)), ctypes.sizeof(INPUT))
    time.sleep(0.1)

def press_numpad_divide(inp):'''

c = c.replace('def press_numpad_divide(inp):', new_func)

pattern = r'inp\._down\("space"\)\n\s*time\.sleep\([^)]+\)\n\s*inp\._up\("space"\)'
c = re.sub(pattern, 'press_space_key()', c)

with open('tools/chat_bot_test.py', 'w', encoding='utf-8') as f:
    f.write(c)
