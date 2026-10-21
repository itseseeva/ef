with open('tools/chat_bot_test.py', 'r', encoding='utf-8') as f:
    c = f.read()

import re

# 1. Remove delay=0.01 from type_unicode calls
c = c.replace('type_unicode("reply", delay=0.01)', 'type_unicode("reply")')
c = c.replace('type_unicode(message, delay=0.01)', 'type_unicode(message)')
c = c.replace('type_unicode("s", delay=0.01)', 'type_unicode("s")')

# 2. Add ensure_chat_closed at the start of reply functions
# and increase enter hold time to 0.15s
def patch_enters(text):
    text = text.replace('inp._down("enter")\n    time.sleep(0.1)\n    inp._up("enter")', 'inp._down("enter")\n    time.sleep(0.15)\n    inp._up("enter")')
    return text

c = patch_enters(c)

# 3. Add Esc to clear chat state before typing
c = re.sub(
    r'(def reply_to_normal_chat\(message: str, inp: InputManager\):\n(?:.*?\n)*?\s*print\(f"Печатаю ответ в обычный чат: \{message\}"\))',
    r'\1\n    inp._down("esc")\n    time.sleep(0.15)\n    inp._up("esc")\n    time.sleep(0.2)',
    c
)

c = re.sub(
    r'(def reply_to_chat\(sender: str, message: str, inp: InputManager\):\n(?:.*?\n)*?\s*print\(f"Печатаю ответ: \{message\}"\))',
    r'\1\n    inp._down("esc")\n    time.sleep(0.15)\n    inp._up("esc")\n    time.sleep(0.2)',
    c
)

with open('tools/chat_bot_test.py', 'w', encoding='utf-8') as f:
    f.write(c)
