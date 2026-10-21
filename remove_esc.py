with open('tools/chat_bot_test.py', 'r', encoding='utf-8') as f:
    c = f.read()

import re

c = re.sub(
    r'\s*inp\._down\("esc"\)\n\s*time\.sleep\(0\.15\)\n\s*inp\._up\("esc"\)\n\s*time\.sleep\(0\.2\)',
    '',
    c
)

with open('tools/chat_bot_test.py', 'w', encoding='utf-8') as f:
    f.write(c)
