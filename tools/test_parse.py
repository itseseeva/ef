import sys
sys.path.insert(0, 'e:/Users/username/Desktop/EZF')
from src.core.chat.chat_reader import parse, WHISPER, NORMAL

rows = [
    (NORMAL, '', 49, 56, False),
    (NORMAL, '', 101, 116, False),
    (NORMAL, '', 117, 126, False),
    (WHISPER, 'ay аy: "', 133, 151, False),
    (NORMAL, '', 152, 161, False),
    (WHISPER, "'Ты тут ?", 162, 179, False),
    (NORMAL, '', 180, 187, False),
    (NORMAL, '9 SensciGG', 197, 213, True),
    (NORMAL, 'Ищем активных игроков в наше PvE /PvPа €', 217, 237, False),
    (NORMAL, 'сообщество: Неважно, тольКО ли ты созДал]', 243, 264, False),
    (NORMAL, '7u персонажа или уже топчешь эндгейм_контент', 265, 286, False),
    (NORMAL, 'ногами_у нас найдется место КаждомУ @вязь[OL', 292, 314, False),
    (NORMAL, '', 315, 325, False),
    (WHISPER, 'IceQueex; .', 326, 344, True),
    (WHISPER, 'хжж*ец сколько можкноыаль тебя', 350, 369, False),
    (WHISPER, 'ну типо еслилы вклiтил\'м@pоs;_ То Лаки скажи', 382, 403, False),
    (NORMAL, '', 406, 426, False),
]

msgs = parse(rows)
print('Total messages:', len(msgs))
for m in msgs:
    print(m.channel, m.sender, m.text.encode('utf-8'))
