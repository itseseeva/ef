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

def main():
    print("У тебя есть 3 секунды, чтобы переключиться в игру...")
    time.sleep(3)
    
    inp = InputManager()
    
    print("Открываю чат (нажимаю Enter)...")
    inp.press_button("enter")
    time.sleep(0.5)
    
    print("Печатаю: /w [пробел] lceQueen [пробел]")
    
    # Печатаем через Юникод (так раскладка не имеет значения!)
    type_unicode("/w", delay=0.1)
    time.sleep(0.1)
    
    # А вот пробел жмём аппаратно, чтобы интерфейс игры среагировал
    inp.press_button("space")
    time.sleep(0.3)
    
    type_unicode("lceQueen", delay=0.1)
    time.sleep(0.1)
    
    inp.press_button("space")
    
    print("Готово! Посмотри в игру: что именно написалось в чате?")
    print("Я НЕ нажимал Enter в конце, чтобы ты увидел результат.")

if __name__ == "__main__":
    main()
