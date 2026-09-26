"""
capture_skill_reference.py

Калибровочный инструмент — НЕ часть бота, запускается вручную ОДИН РАЗ
на каждый скилл. Сохраняет эталонный снимок иконки скилла на диск, пока
скилл в игре ТОЧНО готов к касту (не на откате). Этот эталон потом
использует VisionManager.get_skill_ready() для сравнения "похоже на
готовое состояние / не похоже", вместо гадания порога яркости числом.

Как пользоваться:
  1. Через find_skill_coords.py найди offset_x/offset_y иконки скилла
     и впиши их ниже вместе с именем скилла (совпадающим с тем, что
     будет в self._skills в bot.py).
  2. Дождись в игре момента, когда скилл ТОЧНО готов (не на откате).
  3. Запусти: python capture_skill_reference.py
  4. Файл сохранится в references/<SKILL_NAME>.png — этот путь и впиши
     в bot.py в поле 'reference' для соответствующего скилла.

Повтори для каждого скилла в твоей ротации — у каждого свой эталон.
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '.')))

from src.core.vision.vision_manager import VisionManager

# ============= ПРАВЬ ПОД СЕБЯ ПЕРЕД КАЖДЫМ ЗАПУСКОМ =============
SKILL_NAME = "skill_1"           # должно совпадать с "name" в bot.py
OFFSET_X, OFFSET_Y = 200, 320    # из find_skill_coords.py
WIDTH, HEIGHT = 40, 40           # размер ROI вокруг иконки
# ==================================================================


def main() -> None:
    vision = VisionManager()
    roi = vision.build_roi(OFFSET_X, OFFSET_Y, WIDTH, HEIGHT)

    out_dir = os.path.join(os.path.dirname(__file__), "references")
    os.makedirs(out_dir, exist_ok=True)
    save_path = os.path.join(out_dir, f"{SKILL_NAME}.png")

    print(f"Захватываю ROI {roi} для скилла '{SKILL_NAME}'...")
    print("УБЕДИСЬ, что скилл в игре ПРЯМО СЕЙЧАС готов к касту!")

    ok = vision.save_skill_reference(roi, save_path)
    vision.close()

    if ok:
        print(f"Готово: {save_path}")
        print(f"Впиши в bot.py: \"reference\": \"references/{SKILL_NAME}.png\"")
    else:
        print("Не удалось сохранить эталон — смотри лог ошибки выше.")


if __name__ == "__main__":
    main()
