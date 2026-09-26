"""
capture_skill_icons.py

Калибровочный инструмент — НЕ часть бота, запускается вручную ОДИН РАЗ,
чтобы подготовить картинки для визуального редактора порядка ротации
(будущий configure_order.py). Наводишь мышь на иконку каждого скилла в
игре по очереди, нажимаешь Enter в консоли — вырезка экрана вокруг
курсора сохраняется в icons/<слот>.png.

Как пользоваться:
  1. Разверни игру так, чтобы скилл-бар был виден.
  2. Запусти: python capture_skill_icons.py
  3. Для каждого из 12 слотов (в том же порядке, что в skills_config.json:
     1-9, 0, -, =): наведи мышь ТОЧНО на центр иконки скилла в игре,
     вернись в консоль и нажми Enter. Курсор в момент нажатия Enter уже
     стоит на месте — двигать мышь во время самого нажатия не нужно.
  4. Файлы появятся в icons/<слот>.png. Если размер вырезки не совпадает
     с реальным размером иконки в игре (обрезало край или взяло лишнее
     вокруг) — поменяй ICON_WIDTH/ICON_HEIGHT ниже и перезапусти для
     конкретного слота (можно просто Enter -> Enter, файл перезапишется).

Тот же приём чтения курсора, что уже применяется в main.py (_is_key_down)
и был в старом find_skill_coords.py — это ЧТЕНИЕ состояния ОС, не
инъекция ввода, поэтому не попадает под запрет на pyautogui/keyboard/mouse.
"""

import os
import ctypes

import mss
import numpy as np
import cv2

# Слоты в порядке твоего бара — те же ключи, что в skills_config.json.
# Порядок здесь не важен для результата (каждый слот сохраняется в свой
# файл независимо), но идти по порядку бара проще, чтобы не запутаться,
# на какую иконку сейчас наводишь мышь.
SLOT_LABELS = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "0", "-", "="]

# Размер вырезки вокруг курсора, px. Не магическое число под конкретное
# разрешение — подбирается один раз "на глаз" под то, как иконки выглядят
# у ТЕБЯ на экране (та же логика, что была у width/height в build_roi для
# HP-бара). 50x50 — разумная стартовая точка для большинства разрешений.
ICON_WIDTH = 50
ICON_HEIGHT = 50

_OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "icons")


class _CursorPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def _get_cursor_pos() -> tuple[int, int]:
    point = _CursorPoint()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(point))
    return point.x, point.y


def _capture_icon(sct: "mss.mss", center_x: int, center_y: int) -> "np.ndarray":
    roi = {
        "left": center_x - ICON_WIDTH // 2,
        "top": center_y - ICON_HEIGHT // 2,
        "width": ICON_WIDTH,
        "height": ICON_HEIGHT,
    }
    raw = sct.grab(roi)
    # BGRA -> BGR срезом последнего канала — тот же приём, что и в
    # VisionManager._capture: cv2.imwrite не умеет писать альфа-канал
    # для обычного PNG-скриншота, он тут не нужен.
    return np.array(raw)[..., :3]


def main() -> None:
    os.makedirs(_OUTPUT_DIR, exist_ok=True)
    print(f"Иконки будут сохранены в: {_OUTPUT_DIR}")
    print("Разверни игру так, чтобы скилл-бар был виден, и начнём.\n")

    # with mss.mss() — закрываем соединение с драйвером захвата сами по
    # завершении цикла, а не полагаемся на сборщик мусора: та же гигиена,
    # что в VisionManager.close(), просто здесь короткоживущий скрипт, а
    # не долгоживущий объект, поэтому context manager проще, чем ручной
    # close().
    with mss.mss() as sct:
        for slot in SLOT_LABELS:
            input(f"Наведи мышь на иконку скилла '{slot}' и нажми Enter...")
            x, y = _get_cursor_pos()
            frame = _capture_icon(sct, x, y)

            # "-" и "=" — валидные символы в именах файлов Windows (не
            # входят в запрещённый набор \/:*?"<>|), спецобработка для
            # этих двух слотов не нужна.
            save_path = os.path.join(_OUTPUT_DIR, f"{slot}.png")
            cv2.imwrite(save_path, frame)
            print(f"  Сохранено: {save_path} (курсор был в ({x}, {y}))\n")

    print("Готово! Все 12 иконок сохранены в icons/ — дальше запускаем GUI-редактор.")


if __name__ == "__main__":
    main()
