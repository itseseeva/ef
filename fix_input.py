with open('src/core/input_manager.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

lines[2220] = '            if getattr(self, "_chat_locked", False):\n'
lines[2221] = '                break\n'
lines[2222] = '            verb, _, target = action.partition("_")\n'

with open('src/core/input_manager.py', 'w', encoding='utf-8') as f:
    f.writelines(lines)
