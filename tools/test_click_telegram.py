import time
import ctypes
import ctypes.wintypes
import sys
import os
import cv2
import mss
import numpy as np
import pyautogui

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from src.core.input_manager import InputManager
from src.core.chat.chat_reader import region_rect

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
    
    print("Ищу иконку телеграма на экране...")
    with mss.mss() as sct:
        mon = sct.monitors[1]
        roi = region_rect(mon["left"], mon["top"], mon["width"], mon["height"])
        
        frame = np.array(sct.grab(roi))[..., :3]
        
        # Ищем пиксели цвета иконки (коричневато-розовый)
        b, g, r = cv2.split(frame)
        mask = (r > 120) & (r < 170) & (g > 90) & (g < 135) & (b > 80) & (b < 125)
        
        coords = np.argwhere(mask)
        if len(coords) > 0:
            # Нам нужна самая нижняя иконка (последнее сообщение)
            y_max = coords[:, 0].max()
            bottom_coords = coords[coords[:, 0] > y_max - 20]
            
            y_min, x_min = bottom_coords.min(axis=0)
            y_max, x_max = bottom_coords.max(axis=0)
            
            match_x = (x_min + x_max) // 2
            match_y = (y_min + y_max) // 2
            
            abs_x = roi["left"] + match_x
            abs_y = roi["top"] + match_y
            
            print(f"Найдена иконка по цвету! Кликаю по координатам: {abs_x}, {abs_y}")
            # Перемещаем мышь и кликаем
            pyautogui.moveTo(abs_x, abs_y, duration=0.2)
            pyautogui.click()
            
            time.sleep(0.5)
            print("Печатаю ответ...")
            type_unicode("привет я работаю", delay=0.08)
            time.sleep(0.3)
            # inp.press_button("enter") # пока не жмём, чтобы ты увидел
            print("Успех! Посмотри в игру.")
        else:
            print(f"Иконка не найдена по цвету :(")

if __name__ == "__main__":
    main()
