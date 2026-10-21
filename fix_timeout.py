with open('tools/chat_bot_test.py', 'r', encoding='utf-8') as f:
    c = f.read()

c = c.replace('}), timeout=10.0', '})')

import re
c = re.sub(
    r'(\s*data=json\.dumps\(\{.*?\n\s*\}\)\n\s*)(\))',
    r'\1, timeout=10.0\2',
    c, flags=re.DOTALL
)

with open('tools/chat_bot_test.py', 'w', encoding='utf-8') as f:
    f.write(c)
