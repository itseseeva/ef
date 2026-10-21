with open('tools/chat_bot_test.py', 'r', encoding='utf-8') as f:
    c = f.read()

import re

new_func = '''def press_enter_key():
    VK_RETURN = 0x0D
    ki_down = KEYBDINPUT(VK_RETURN, 0, 0, 0, None)
    ctypes.windll.user32.SendInput(1, ctypes.byref(INPUT(type=1, ki=ki_down)), ctypes.sizeof(INPUT))
    time.sleep(0.05)
    ki_up = KEYBDINPUT(VK_RETURN, 0, 0x0002, 0, None)
    ctypes.windll.user32.SendInput(1, ctypes.byref(INPUT(type=1, ki=ki_up)), ctypes.sizeof(INPUT))
    time.sleep(0.1)

def press_numpad_divide(inp):'''

c = c.replace('def press_numpad_divide(inp):', new_func)

# Replace the input manager enter presses with the direct SendInput function
pattern = r'inp\._down\("enter"\)\n\s*time\.sleep\([^)]+\)\n\s*inp\._up\("enter"\)'
c = re.sub(pattern, 'press_enter_key()', c)

# Also remove the rogue padding sleeps around the old enter blocks if any exist
# (press_enter_key handles its own 0.1s post-sleep)

with open('tools/chat_bot_test.py', 'w', encoding='utf-8') as f:
    f.write(c)
