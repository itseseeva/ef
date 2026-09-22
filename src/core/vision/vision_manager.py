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
        self._load_config()
        self.sct = mss.mss()

        monitor        = self.sct.monitors[1]
        self.center_x  = monitor["left"] + monitor["width"]  // 2
        self.center_y  = monitor["top"]  + monitor["height"] // 2

        logger.debug(
            "VisionManager инициализирован. Центр экрана: (%d, %d)",
            self.center_x, self.center_y
        )

    def _load_config(self) -> None:
        self._red_lower_1 = np.array(vision_config.RED_LOWER_1, dtype=np.uint8)
        self._red_upper_1 = np.array(vision_config.RED_UPPER_1, dtype=np.uint8)
        self._red_lower_2 = np.array(vision_config.RED_LOWER_2, dtype=np.uint8)
        self._red_upper_2 = np.array(vision_config.RED_UPPER_2, dtype=np.uint8)
        
        # Добавляем желтый цвет
        self._yellow_lower = np.array(vision_config.YELLOW_LOWER, dtype=np.uint8)
        self._yellow_upper = np.array(vision_config.YELLOW_UPPER, dtype=np.uint8)
        
        self._max_hp_width = float(vision_config.MAX_TARGET_HP_WIDTH)
        logger.debug("VisionManager: HSV-конфиг загружен.")

    def build_roi(self, offset_x: int, offset_y: int,
                  width: int, height: int) -> dict:
        return {
            "left":   self.center_x + offset_x,
            "top":    self.center_y + offset_y,
            "width":  width,
            "height": height,
        }

    def get_target_state(self, roi_coords: dict) -> tuple[bool, float]:
        """
        Главный метод для FSM. Один захват экрана — два значения.
        Returns:
            tuple[bool, float]: (есть ли таргет, процент HP от 0.0 до 100.0)
        """
        frame = self._capture(roi_coords)
        if frame is None:
            return False, 0.0

        bar = self._find_hp_bar(frame)
        if bar is None:
            return False, 0.0

        _, _, bar_width, _ = bar

        hp = min((bar_width / self._max_hp_width) * 100.0, 100.0)
        hp = round(hp, 2)

        return True, hp

    def has_target(self, roi_coords: dict) -> bool:
        """Тонкая обёртка над get_target_state."""
        has, _ = self.get_target_state(roi_coords)
        return has

    def get_hp_percent(self, roi_coords: dict) -> float:
        """Тонкая обёртка над get_target_state."""
        _, hp = self.get_target_state(roi_coords)
        return hp

    def _find_hp_bar(self, frame: "np.ndarray") -> "tuple[int,int,int,int] | None":
        """
        Ищет HP-бар таргета в кадре через анализ связных компонент.
        Фильтрует по соотношению сторон: HP-бар всегда широкий и плоский.
        """
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)

        # Маска для красных (агрессивных) мобов
        mask_red1 = cv2.inRange(hsv, self._red_lower_1, self._red_upper_1)
        mask_red2 = cv2.inRange(hsv, self._red_lower_2, self._red_upper_2)
        mask_red  = mask_red1 | mask_red2

        # Маска для желтых (нейтральных) мобов
        mask_yellow = cv2.inRange(hsv, self._yellow_lower, self._yellow_upper)

        # Объединяем маски — теперь бот видит и красные, и желтые полоски
        mask = mask_red | mask_yellow

        num_labels, _, stats, _ = cv2.connectedComponentsWithStats(
            mask, connectivity=8
        )

        best = None

        for i in range(1, num_labels):
            x, y, w, h, area = stats[i]

            # Фильтр 1: отсекаем мелкий мусор
            if area < 20 or h == 0:
                continue

            # Фильтр 2: Плотность (Extent) — САМЫЙ ВАЖНЫЙ!
            # Настоящая полоска ХП — это сплошной прямоугольник (плотность близка к 1.0).
            # Декоративная линия с "галочкой" образует высокую рамку (h), но внутри неё 
            # почти нет желтых пикселей (много пустоты). Её плотность будет < 0.3.
            extent = area / (w * h)
            if extent < 0.5:
                continue

            # Фильтр 3: соотношение сторон > 4 — HP-бар широкий и плоский
            if (w / h) < 4:
                continue

            if best is None or w > best[2]:
                best = (x, y, w, h)

        return best

    def _capture(self, roi_coords: dict) -> np.ndarray | None:
        try:
            raw = self.sct.grab(roi_coords)
            return np.array(raw)[..., :3]
        except Exception as e:
            logger.error("VisionManager: ошибка захвата экрана: %s", e)
            return None

    def close(self) -> None:
        self.sct.close()
        logger.debug("VisionManager закрыт.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)

    vision = VisionManager()

    TEST_ROI = vision.build_roi(
        offset_x=vision_config.TARGET_HP_OFFSET_X,
        offset_y=vision_config.TARGET_HP_OFFSET_Y,
        width=vision_config.TARGET_HP_WIDTH,
        height=vision_config.TARGET_HP_HEIGHT,
    )

    print(f"Центр экрана: ({vision.center_x}, {vision.center_y})")
    print(f"Тестовый ROI: {TEST_ROI}")
    print("Нажми 'q' для выхода.")

    try:
        while True:
            has_target, hp = vision.get_target_state(TEST_ROI)

            status = f"HP: {hp:.1f}%" if has_target else "Таргет не найден"
            print(status)

            frame = vision._capture(TEST_ROI)
            if frame is not None:
                debug_frame = cv2.resize(
                    frame,
                    (TEST_ROI["width"] * 4, TEST_ROI["height"] * 4),
                    interpolation=cv2.INTER_NEAREST,
                )

                if has_target:
                    bar = vision._find_hp_bar(frame)
                    if bar is not None:
                        x, y, w, h = bar
                        cv2.rectangle(
                            debug_frame,
                            (x * 4, y * 4),
                            ((x + w) * 4, (y + h) * 4),
                            (0, 255, 0),
                            2,
                        )

                cv2.imshow("VisionManager Debug", debug_frame)

            if cv2.waitKey(1) & 0xFF == ord("q"):
                break

            time.sleep(1 / 60)

    finally:
        cv2.destroyAllWindows()
        vision.close()
        print("Песочница закрыта.")
