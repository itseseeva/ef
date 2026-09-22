import os
import sys
import time
import logging

import cv2
import mss
import numpy as np

# Хак для песочницы: добавляем корень проекта в пути поиска модулей,
# чтобы абсолютный импорт 'src.config' работал при прямом запуске файла.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../..')))
from src.config import vision_config

logger = logging.getLogger(__name__)


class VisionManager:
    """
    Модуль машинного зрения. Поставляет данные о состоянии игры для FSM.

    Архитектурный принцип:
        self.sct = mss.mss() открывается ОДИН РАЗ в __init__.
        Повторная инициализация mss на каждом кадре создаёт накладные расходы
        на подключение к драйверу захвата — недопустимо при 60 FPS.

        Конкретные координаты ROI (Region of Interest — зона захвата) передаются
        в каждый метод отдельно. Это позволяет одним объектом VisionManager
        читать HP таргета, HP персонажа и MP без пересоздания захвата.
    """

    def __init__(self) -> None:
        # _load_config первым — HSV-массивы должны быть готовы до любых операций.
        self._load_config()

        # mss.mss() открывается один раз на весь жизненный цикл объекта.
        self.sct = mss.mss()

        # Центр главного монитора вычисляется один раз при старте.
        # sct.monitors[1] — главный монитор (0 — виртуальный, объединяющий все).
        # Храним center_x / center_y как атрибуты: разрешение не меняется в рантайме,
        # пересчитывать каждый кадр — бессмысленная трата CPU.
        monitor        = self.sct.monitors[1]
        self.center_x  = monitor["left"] + monitor["width"]  // 2
        self.center_y  = monitor["top"]  + monitor["height"] // 2

        logger.debug(
            "VisionManager инициализирован. Центр экрана: (%d, %d)",
            self.center_x, self.center_y
        )

    def _load_config(self) -> None:
        """
        Загружает HSV-диапазоны из src/config/vision_config.py и конвертирует
        их в numpy-массивы с dtype=uint8.
        """
        self._red_lower_1 = np.array(vision_config.RED_LOWER_1, dtype=np.uint8)
        self._red_upper_1 = np.array(vision_config.RED_UPPER_1, dtype=np.uint8)
        self._red_lower_2 = np.array(vision_config.RED_LOWER_2, dtype=np.uint8)
        self._red_upper_2 = np.array(vision_config.RED_UPPER_2, dtype=np.uint8)
        logger.debug("VisionManager: HSV-конфиг загружен.")

    def build_roi(self, offset_x: int, offset_y: int,
                  width: int, height: int) -> dict:
        """
        Строит словарь ROI динамически от центра экрана.

        Почему метод, а не просто dict в коде состояний:
            Логика вычисления координат живёт в одном месте.
            FSM-состояния передают только семантические оффсеты из конфига —
            не думают про пиксели и разрешение.

        Args:
            offset_x: сдвиг по горизонтали от центра (отрицательный = влево).
            offset_y: сдвиг по вертикали от центра (отрицательный = вверх).
            width:    ширина зоны захвата в пикселях.
            height:   высота зоны захвата в пикселях.

        Returns:
            dict: {'top': int, 'left': int, 'width': int, 'height': int}
                  готов к передаче в sct.grab() или get_hp_percent().
        """
        return {
            "left":   self.center_x + offset_x,
            "top":    self.center_y + offset_y,
            "width":  width,
            "height": height,
        }

    def get_hp_percent(self, roi_coords: dict) -> float:
        """Возвращает процент заполненности красной полоски HP (0.0 — 100.0)."""
        frame = self._capture(roi_coords)
        if frame is None:
            return 0.0

        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        mask1 = cv2.inRange(hsv, self._red_lower_1, self._red_upper_1)
        mask2 = cv2.inRange(hsv, self._red_lower_2, self._red_upper_2)
        mask  = mask1 | mask2

        red_pixels   = np.count_nonzero(mask)
        total_pixels = mask.size

        if total_pixels == 0:
            return 0.0

        return round((red_pixels / total_pixels) * 100.0, 2)

    def _capture(self, roi_coords: dict) -> np.ndarray | None:
        """Захватывает зону экрана через уже открытый self.sct."""
        try:
            raw = self.sct.grab(roi_coords)
            return np.array(raw)[..., :3]
        except Exception as e:
            logger.error("VisionManager: ошибка захвата экрана: %s", e)
            return None

    def close(self) -> None:
        """Явно закрывает mss при завершении работы бота."""
        self.sct.close()
        logger.debug("VisionManager закрыт.")


# ----------------------------------------------------------------------
# ПЕСОЧНИЦА ДЛЯ ЛОКАЛЬНОГО ТЕСТИРОВАНИЯ
# ----------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)

    vision = VisionManager()

    # ROI строится от центра через конфиг — никаких абсолютных координат.
    # Подгоняй оффсеты в vision_config.py и перезапускай песочницу.
    TEST_ROI = vision.build_roi(
        offset_x = vision_config.TARGET_HP_OFFSET_X,
        offset_y = vision_config.TARGET_HP_OFFSET_Y,
        width    = vision_config.TARGET_HP_WIDTH,
        height   = vision_config.TARGET_HP_HEIGHT,
    )

    print(f"Центр экрана: ({vision.center_x}, {vision.center_y})")
    print(f"Тестовый ROI: {TEST_ROI}")
    print("Нажми 'q' для выхода.")

    try:
        while True:
            hp = vision.get_hp_percent(TEST_ROI)
            print(f"HP: {hp:.1f}%")

            frame = vision._capture(TEST_ROI)
            if frame is not None:
                debug_frame = cv2.resize(
                    frame,
                    (TEST_ROI["width"] * 4, TEST_ROI["height"] * 4),
                    interpolation=cv2.INTER_NEAREST,
                )
                cv2.imshow("VisionManager Debug", debug_frame)

            if cv2.waitKey(1) & 0xFF == ord("q"):
                break

            time.sleep(1 / 60)

    finally:
        cv2.destroyAllWindows()
        vision.close()
        print("Песочница закрыта.")
