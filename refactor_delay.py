with open('tools/chat_bot_test.py', 'r', encoding='utf-8') as f:
    c = f.read()

import re

# We will completely replace the `if has_messages:` block.

old_block = r'''                if has_messages:
                    # ПАУЗИМ БОТА
                    print\("\\n\[Чат-бот\] Новое сообщение! Ставлю бота на паузу\.\.\."\)
                    bot_instance\._paused_by_chat = True
                
                    # Ждем полсекунды.*?
                    time\.sleep\(0\.5\)
                
                    # Блокируем ЛЮБЫЕ.*?
                    if hasattr\(inp, 'set_chat_lock'\):
                        inp\.set_chat_lock\(True\)
                    else:
                        inp\.cancel_pending_actions\(\)
                    time\.sleep\(0\.1\)
                
                    try:
                            for sender, texts in whispers_by_sender\.items\(\):
                                combined_text = " "\.join\(texts\)
                                print\(f"\[ЛС\] \{sender\}: \{combined_text\}"\)
                                reply_text = generate_reply\(sender, combined_text\)
                                reply_to_chat\(sender, reply_text, inp\)
                                sent_messages\.append\(reply_text\)
                                if len\(sent_messages\) > 10:
                                    sent_messages\.pop\(0\)
                                time\.sleep\(2\.0\)
                            
                            for sender, texts in normal_by_sender\.items\(\):
                                combined_text = " "\.join\(texts\)
                                print\(f"\[Чат\] \{sender\}: \{combined_text\}"\)
                                ignored_normal_senders\.add\(sender\)
                                reply_text = generate_reply\(sender, combined_text\)
                                reply_to_normal_chat\(reply_text, inp\)
                                sent_messages\.append\(reply_text\)
                                if len\(sent_messages\) > 10:
                                    sent_messages\.pop\(0\)
                                time\.sleep\(2\.0\)
                    finally:
                        # СНИМАЕМ ПАУЗУ В ЛЮБОМ СЛУЧАЕ
                        print\("\[Чат-бот\] Ответ отправлен\. Снимаю паузу, продолжаем фарм!\\\\n"\)
                        if hasattr\(inp, 'set_chat_lock'\):
                            inp\.set_chat_lock\(False\)
                        bot_instance\._paused_by_chat = False'''

new_block = '''                if has_messages:
                    # Сначала генерируем ответы (бот продолжает фармить)
                    whisper_replies = []
                    for sender, texts in whispers_by_sender.items():
                        combined_text = " ".join(texts)
                        print(f"\\n[Чат-бот] Новое ЛС от {sender}. Генерирую ответ (фарм продолжается)...")
                        reply_text = generate_reply(sender, combined_text)
                        whisper_replies.append((sender, reply_text))
                        
                    normal_replies = []
                    for sender, texts in normal_by_sender.items():
                        combined_text = " ".join(texts)
                        print(f"\\n[Чат-бот] Новое сообщение рядом от {sender}. Генерирую ответ (фарм продолжается)...")
                        reply_text = generate_reply(sender, combined_text)
                        normal_replies.append((sender, reply_text))
                        ignored_normal_senders.add(sender)

                    # Ответы сгенерированы! Теперь ставим паузу, чтобы напечатать их
                    print("[Чат-бот] Ответы готовы. Ставлю бота на паузу для печати в чат...")
                    bot_instance._paused_by_chat = True
                    time.sleep(0.5)
                    
                    if hasattr(inp, 'set_chat_lock'):
                        inp.set_chat_lock(True)
                    else:
                        inp.cancel_pending_actions()
                    time.sleep(0.1)
                    
                    try:
                        for sender, reply_text in whisper_replies:
                            reply_to_chat(sender, reply_text, inp)
                            sent_messages.append(reply_text)
                            if len(sent_messages) > 10:
                                sent_messages.pop(0)
                            time.sleep(1.0)
                            
                        for sender, reply_text in normal_replies:
                            reply_to_normal_chat(reply_text, inp)
                            sent_messages.append(reply_text)
                            if len(sent_messages) > 10:
                                sent_messages.pop(0)
                            time.sleep(1.0)
                    finally:
                        print("[Чат-бот] Все ответы отправлены. Снимаю паузу, продолжаем фарм!\\n")
                        if hasattr(inp, 'set_chat_lock'):
                            inp.set_chat_lock(False)
                        bot_instance._paused_by_chat = False'''

c = re.sub(old_block, new_block, c, flags=re.DOTALL)

with open('tools/chat_bot_test.py', 'w', encoding='utf-8') as f:
    f.write(c)
