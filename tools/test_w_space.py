import time
import ctypes
import ctypes.wintypes
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from src.core.input_manager import InputManager

class KEYBDINPUT(ctypes.Structure):
    _fields_ = [('wVk', ctypes.wintypes.WORD), 
                ('wScan', ctypes.wintypes.WORD), 
                ('dwFlags', ctypes.wintypes.DWORD), 
                ('time', ctypes.wintypes.DWORD), 
                ('dwExtraInfo', ctypes.POINTER(ctypes.c_ulong))]
class _I(ctypes.Union):
    _fields_ = [('ki', KEYBDINPUT), ('pad', ctypes.c_ulong*8)]
class INPUT(ctypes.Structure):
    _anonymous_ = ('i',)
    _fields_ = [('type', ctypes.wintypes.DWORD), ('i', _I)]

def type_unicode(text: str, delay: float = 0.08):
    for char in text:
        ki_down = KEYBDINPUT(0, ord(char), 0x0004, 0, None)
        inp_down = INPUT(type=1, ki=ki_down)
        ctypes.windll.user32.SendInput(1, ctypes.byref(inp_down), ctypes.sizeof(INPUT))
        time.sleep(0.01)
        ki_up = KEYBDINPUT(0, ord(char), 0x0004 | 0x0002, 0, None)
        inp_up = INPUT(type=1, ki=ki_up)
        ctypes.windll.user32.SendInput(1, ctypes.byref(inp_up), ctypes.sizeof(INPUT))
        time.sleep(delay)

def press_numpad_divide():
    VK_DIVIDE = 0x6F
    # Key down
    ki_down = KEYBDINPUT(VK_DIVIDE, 0, 0, 0, None)
    ctypes.windll.user32.SendInput(1, ctypes.byref(INPUT(type=1, ki=ki_down)), ctypes.sizeof(INPUT))
    time.sleep(0.01)
    # Key up
    ki_up = KEYBDINPUT(VK_DIVIDE, 0, 0x0002, 0, None)
    ctypes.windll.user32.SendInput(1, ctypes.byref(INPUT(type=1, ki=ki_up)), ctypes.sizeof(INPUT))

def main():
    print("У тебя есть 3 секунды, чтобы переключиться в игру...")
    time.sleep(3)
    
    inp = InputManager()
    
    print("Открываю чат (нажимаю Enter)...")
    inp.press_button("enter")
    time.sleep(0.5)
    
    print("Печатаю: / (через Numpad Divide)")
    press_numpad_divide()
    time.sleep(0.1)
    
    print("Печатаю: reply (через Юникод)")
    type_unicode("reply", delay=0.1)
    time.sleep(0.1)
    
    print("Нажимаю пробел (аппаратно)")
    inp.press_button("space")
    time.sleep(0.5)
    
    print("Печатаю текст сообщения...")
    type_unicode("привет я работаю", delay=0.1)
    time.sleep(0.3)
    
    print("Нажимаю Enter для отправки!")
    # Одинарное нажатие Enter отправляет сообщение и ЗАКРЫВАЕТ чат
    inp._down("enter")
    time.sleep(0.1)
    inp._up("enter")
    
    print("Возвращаю чат в обычный режим (/s)...")
    time.sleep(0.5)
    
    # Открываем чат ЗАНОВО
    inp.press_button("enter")
    time.sleep(0.3)
    
    # Печатаем /s
    press_numpad_divide()
    time.sleep(0.05)
    type_unicode("s", delay=0.08)
    
    # Пробел для переключения режима (чат остаётся открытым)
    inp.press_button("space")
    time.sleep(0.3)
    
    # Enter для закрытия пустой строки чата и возврата в игру
    inp._down("enter")
    time.sleep(0.1)
    inp._up("enter")
    
    print("Готово! Проверяй игру.")

if __name__ == "__main__":
    main()
