with open('tools/chat_bot_test.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

new_block = [
    '    try:\n',
    '        if channel == "NORMAL":\n',
    '            prompt = SYSTEM_PROMPT + " Важно: это ОБЩИЙ ЧАТ, где просто пробегают мимо. Отвечай МАКСИМАЛЬНО КРАТКО (1-3 слова), например: \'занят\', \'фармлю\', \'уйди\', \'качаюсь\', \'не мешай\'. Ничего не объясняй."\n',
    '        else:\n',
    '            prompt = SYSTEM_PROMPT + " Важно: это ЛИЧКА. Отвечай дерзко, но можешь использовать 3-6 слов."\n',
    '        \n',
    '        messages = [{"role": "system", "content": prompt}]\n',
]

lines = lines[:26] + new_block + lines[33:]

with open('tools/chat_bot_test.py', 'w', encoding='utf-8') as f:
    f.writelines(lines)
