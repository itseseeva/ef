with open('tools/chat_bot_test.py', 'r', encoding='utf-8') as f:
    c = f.read()

c = c.replace('return "ща занят"', 'return random.choice(FALLBACK_PHRASES)')
c = c.replace('return "Ща занят"', 'return random.choice(FALLBACK_PHRASES)')

with open('tools/chat_bot_test.py', 'w', encoding='utf-8') as f:
    f.write(c)
