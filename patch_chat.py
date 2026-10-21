with open('tools/chat_bot_test.py', 'r', encoding='utf-8') as f:
    c = f.read()

import re
c = c.replace('inp.press_button("enter")', 'inp._down("enter")\n    time.sleep(0.1)\n    inp._up("enter")')

old_try = '''                bot_instance._paused_by_chat = True
                
                # Ждем полсекунды, чтобы FarmBot успел остановиться
                time.sleep(0.5)
                
                # Если бот двигался, отменяем все зажатые клавиши
                inp.cancel_pending_actions()
                time.sleep(0.1)
                
                try:'''
new_try = '''                bot_instance._paused_by_chat = True
                
                # Ждем полсекунды, чтобы FarmBot успел остановиться
                time.sleep(0.5)
                
                # Блокируем ЛЮБЫЕ нажатия от фоновых потоков (скиллов)
                if hasattr(inp, 'set_chat_lock'):
                    inp.set_chat_lock(True)
                else:
                    inp.cancel_pending_actions()
                time.sleep(0.1)
                
                try:'''
c = c.replace(old_try, new_try)

old_finally = '''                finally:
                    # СНИМАЕМ ПАУЗУ В ЛЮБОМ СЛУЧАЕ
                    print("[Чат-бот] Ответ отправлен. Снимаю паузу, продолжаем фарм!\\n")
                    bot_instance._paused_by_chat = False'''
new_finally = '''                finally:
                    # СНИМАЕМ ПАУЗУ В ЛЮБОМ СЛУЧАЕ
                    print("[Чат-бот] Ответ отправлен. Снимаю паузу, продолжаем фарм!\\n")
                    if hasattr(inp, 'set_chat_lock'):
                        inp.set_chat_lock(False)
                    bot_instance._paused_by_chat = False'''
c = c.replace(old_finally, new_finally)

with open('tools/chat_bot_test.py', 'w', encoding='utf-8') as f:
    f.write(c)
