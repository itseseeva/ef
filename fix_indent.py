with open('tools/chat_bot_test.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

new_lines = []
for i, line in enumerate(lines):
    if line.strip() == "if hasattr(inp, 'set_chat_lock'):" and "finally:" in lines[i-2]:
        new_lines.append("                        if hasattr(inp, 'set_chat_lock'):\n")
    elif line.strip() == "inp.set_chat_lock(False)" and "hasattr" in lines[i-1]:
        new_lines.append("                            inp.set_chat_lock(False)\n")
    elif line.strip() == "bot_instance._paused_by_chat = False" and "hasattr" in lines[i-2]:
        new_lines.append("                        bot_instance._paused_by_chat = False\n")
    else:
        new_lines.append(line)

with open('tools/chat_bot_test.py', 'w', encoding='utf-8') as f:
    f.writelines(new_lines)
