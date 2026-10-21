with open('tools/chat_bot_test.py', 'r', encoding='utf-8') as f:
    c = f.read()

import re
c = re.sub(r'def main\(\):.*', '''def chat_loop(bot_instance):
    print("Инициализация OCR для чат-бота...")
    ocr = make_ocr()
    if ocr is None:
        print("Ошибка: OCR не инициализирован. Чат-бот не запущен.")
        return
        
    from src.core.chat.chat_reader import NORMAL
    reader = ChatReader(ocr)
    
    # Используем тот же InputManager, что и основной бот, если он есть
    inp = getattr(bot_instance, "input_manager", InputManager())
    
    print("Чат-бот запущен в фоновом потоке. Жду сообщений...")
    
    sent_messages = []
    ignored_normal_senders = set()
    
    with mss.mss() as sct:
        mon = sct.monitors[1]
        roi = region_rect(mon["left"], mon["top"], mon["width"], mon["height"])
        
        while getattr(bot_instance, "_running", True):
            frame = np.array(sct.grab(roi))[..., :3]
            t0 = time.perf_counter()
            
            messages = reader.poll(frame)
            
            whispers_by_sender = {}
            normal_by_sender = {}
            
            for m in messages:
                if not m.sender:
                    continue
                    
                # Игнорируем эхо
                if is_own_message(m.text, sent_messages):
                    continue
                
                if m.channel == WHISPER:
                    if m.sender not in whispers_by_sender:
                        whispers_by_sender[m.sender] = []
                    whispers_by_sender[m.sender].append(m.text)
                    
                elif m.channel == NORMAL:
                    if m.sender in ignored_normal_senders:
                        continue
                    if m.sender not in normal_by_sender:
                        normal_by_sender[m.sender] = []
                    normal_by_sender[m.sender].append(m.text)
            
            has_messages = bool(whispers_by_sender) or bool(normal_by_sender)
            
            if has_messages:
                # ПАУЗИМ БОТА
                print("\\n[Чат-бот] Новое сообщение! Ставлю бота на паузу...")
                bot_instance._paused_by_chat = True
                
                # Ждем полсекунды, чтобы FarmBot успел остановиться
                time.sleep(0.5)
                
                # Если бот двигался, отменяем все зажатые клавиши
                inp.cancel_pending_actions()
                time.sleep(0.1)
                
                try:
                    for sender, texts in whispers_by_sender.items():
                        combined_text = " ".join(texts)
                        print(f"[ЛС] {sender}: {combined_text}")
                        reply_text = generate_reply(sender, combined_text)
                        reply_to_chat(sender, reply_text, inp)
                        sent_messages.append(reply_text)
                        if len(sent_messages) > 10:
                            sent_messages.pop(0)
                        time.sleep(2.0)
                        
                    for sender, texts in normal_by_sender.items():
                        combined_text = " ".join(texts)
                        print(f"[Чат] {sender}: {combined_text}")
                        ignored_normal_senders.add(sender)
                        reply_text = generate_reply(sender, combined_text)
                        reply_to_normal_chat(reply_text, inp)
                        sent_messages.append(reply_text)
                        if len(sent_messages) > 10:
                            sent_messages.pop(0)
                        time.sleep(2.0)
                finally:
                    # СНИМАЕМ ПАУЗУ В ЛЮБОМ СЛУЧАЕ
                    print("[Чат-бот] Ответ отправлен. Снимаю паузу, продолжаем фарм!\\n")
                    bot_instance._paused_by_chat = False
            
            dt = time.perf_counter() - t0
            time.sleep(max(0.05, 0.4 - dt))


def start_chat_thread(bot_instance):
    import threading
    t = threading.Thread(target=chat_loop, args=(bot_instance,), daemon=True)
    t.start()
    return t

if __name__ == "__main__":
    class DummyBot:
        _running = True
        _paused_by_chat = False
        input_manager = InputManager()
    
    dbot = DummyBot()
    start_chat_thread(dbot)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        dbot._running = False''', c, flags=re.DOTALL)

with open('tools/chat_bot_test.py', 'w', encoding='utf-8') as f:
    f.write(c)
