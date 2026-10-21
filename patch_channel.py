with open('tools/chat_bot_test.py', 'r', encoding='utf-8') as f:
    c = f.read()

import re

# Update generate_reply signature
c = c.replace('def generate_reply(sender: str, message: str) -> str:', 'def generate_reply(sender: str, message: str, channel="WHISPER") -> str:')

# Update prompt usage inside generate_reply
old_prompt_usage = r'messages = \[\{"role": "system", "content": SYSTEM_PROMPT\}\]'
new_prompt_usage = '''        if channel == "NORMAL":
            prompt = SYSTEM_PROMPT + " Важно: это ОБЩИЙ ЧАТ, мимокрокодилы. Отвечай МАКСИМАЛЬНО КРАТКО (1-3 слова), например: 'занят', 'фармлю', 'уйди', 'качаюсь', 'не мешай'. Ничего не объясняй."
        else:
            prompt = SYSTEM_PROMPT + " Важно: это ЛИЧКА. Отвечай дерзко, но можешь использовать 3-6 слов."
        
        messages = [{"role": "system", "content": prompt}]'''
c = re.sub(old_prompt_usage, new_prompt_usage, c)

# Pass channel="NORMAL" in normal replies
old_normal = '''                    normal_replies = []
                    for sender, texts in normal_by_sender.items():
                        combined_text = " ".join(texts)
                        print(f"\\n[Чат-бот] Новое сообщение рядом от {sender}. Генерирую ответ (фарм продолжается)...")
                        reply_text = generate_reply(sender, combined_text)'''
new_normal = '''                    normal_replies = []
                    for sender, texts in normal_by_sender.items():
                        combined_text = " ".join(texts)
                        print(f"\\n[Чат-бот] Новое сообщение рядом от {sender}. Генерирую ответ (фарм продолжается)...")
                        reply_text = generate_reply(sender, combined_text, channel="NORMAL")'''
c = c.replace(old_normal, new_normal)

with open('tools/chat_bot_test.py', 'w', encoding='utf-8') as f:
    f.write(c)
