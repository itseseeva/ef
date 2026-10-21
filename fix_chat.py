with open('tools/chat_bot_test.py', 'r', encoding='utf-8') as f:
    c = f.read()

# Add timeout
c = c.replace('})', '}), timeout=10.0', 1)

# Replace space button
c = c.replace('inp.press_button("space")', 'inp._down("space")\n    time.sleep(0.1)\n    inp._up("space")')

# Replace locking logic
import re
c = re.sub(
    r'(bot_instance\._paused_by_chat = True\s+# Ждем полсекунды.*?time\.sleep\(0\.5\)\s+# Если бот двигался.*?inp\.cancel_pending_actions\(\)\s*time\.sleep\(0\.1\)\s*try:)',
    '''bot_instance._paused_by_chat = True
                
                # Ждем полсекунды, чтобы FarmBot успел остановиться
                time.sleep(0.5)
                
                # Блокируем ЛЮБЫЕ нажатия от фоновых потоков (скиллов)
                if hasattr(inp, 'set_chat_lock'):
                    inp.set_chat_lock(True)
                else:
                    inp.cancel_pending_actions()
                time.sleep(0.1)
                
                try:''',
    c, flags=re.DOTALL
)

c = re.sub(
    r'(print\("\[Чат-бот\] Ответ отправлен.*?bot_instance\._paused_by_chat = False)',
    '''print("[Чат-бот] Ответ отправлен. Снимаю паузу, продолжаем фарм!\\n")
                    if hasattr(inp, 'set_chat_lock'):
                        inp.set_chat_lock(False)
                    bot_instance._paused_by_chat = False''',
    c, flags=re.DOTALL
)

with open('tools/chat_bot_test.py', 'w', encoding='utf-8') as f:
    f.write(c)
