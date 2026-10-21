with open('tools/chat_bot_test.py', 'r', encoding='utf-8') as f:
    c = f.read()

import re

# Update prompt to emphasize creativity
old_prompt_regex = r'SYSTEM_PROMPT = ".*?"'
new_prompt = 'SYSTEM_PROMPT = "Ты геймер-задрот, который сейчас жестко гриндит мобов на макросе. Отвечай очень коротко, сленгом, по-русски, без знаков препинания, иногда с опечатками. ВАЖНОЕ ПРАВИЛО: ты вообще не хочешь ни с кем общаться. На ЛЮБОЕ первое сообщение сразу отвечай отмазкой. ПРИДУМЫВАЙ КАЖДЫЙ РАЗ НОВУЮ, УНИКАЛЬНУЮ ОТМАЗКУ (просто как пример: \'я квест делаю не мешай\', \'фулл инвентарь ща\', \'сори смотрю ютуб\', \'соло фарм\', \'нет времени\', \'отвали я под бафом\'). Фантазируй и придумывай свои собственные причины, чтобы это каждый раз звучало по-разному и естественно!"'
c = re.sub(old_prompt_regex, new_prompt, c)

# Add temperature=1.2 to requests.post
old_data = r'"model": "openai/gpt-4o-mini",\s*"messages": messages\s*\}'
new_data = '"model": "openai/gpt-4o-mini",\n                "messages": messages,\n                "temperature": 1.2\n            }'
c = re.sub(old_data, new_data, c)

with open('tools/chat_bot_test.py', 'w', encoding='utf-8') as f:
    f.write(c)
