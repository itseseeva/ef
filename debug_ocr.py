with open('tools/chat_bot_test.py', 'r', encoding='utf-8') as f:
    c = f.read()

import re

c = re.sub(
    r'(for m in messages:\n)',
    r'\1                    print(f"DEBUG OCR NEW MSG: sender={m.sender}, text={m.text}, channel={m.channel}")\n',
    c
)

with open('tools/chat_bot_test.py', 'w', encoding='utf-8') as f:
    f.write(c)
