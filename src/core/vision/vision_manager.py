"""
src/core/vision/vision_manager.py
"""

import os
import sys
import time
import logging
from dataclasses import dataclass

import cv2
import mss
import numpy as np

# Хак для песочницы: добавляем корень проекта в пути поиска модулей,
# чтобы абсолютный импорт 'src.config' работал при прямом запуске файла.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../..')))
from src.config import vision_config

logger = logging.getLogger(__name__)


@dataclass
class TargetInfo:
    """
    Результат ОДНОГО скана экрана за тик. dataclass, а не кортеж —
    именованные поля вместо result[0]/result[1]/..., меньше риска
    перепутать порядок при чтении в FSM, и repr сразу показывает
    имена полей при отладке в логах — не нужно помнить, что где лежит.
    """
    found: bool
    hp_percent: float
    offset_x: float  # смещение центра HP-бара от центра ROI, px. + = бар правее центра ROI.
    offset_y: float  # + = бар ниже центра ROI.


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

        get_target_info() — ЕДИНСТВЕННАЯ точка входа для FSM за один тик:
        один захват кадра (mss.grab), один проход детекции, из которого
        разом получаются found/hp_percent/offset_x/offset_y. Раньше
        has_target()/get_hp_percent() были раздельными обёртками, каждая
        со своим захватом кадра — вызвать оба за тик значило бы захватывать
        экран дважды, лишняя нагрузка на 60 FPS цикл. Поэтому оба метода
        удалены, а не оставлены рядом с новым — одна дорога вместо двух
        означает, что случайно наступить на грабли двойного захвата
        попросту негде.
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

    def get_target_info(self, roi_coords: dict) -> TargetInfo:
        """
        Главный метод для FSM. Один захват экрана — сразу все данные,
        нужные и для боевой логики (found, hp_percent), и для доворота
        камеры (offset_x, offset_y).
        """
        empty = TargetInfo(found=False, hp_percent=0.0, offset_x=0.0, offset_y=0.0)

        frame = self._capture(roi_coords)
        if frame is None:
            return empty

        bar = self._find_hp_bar(frame)
        if bar is None:
            return empty

        bar_x, bar_y, bar_w, bar_h = bar

        hp = min((bar_w / self._max_hp_width) * 100.0, 100.0)
        hp = round(hp, 2)

        # Центр найденного бара В КАДРЕ (координаты внутри самого ROI,
        # а не всего экрана — frame это уже вырезанный кусок по roi_coords).
        bar_center_x = bar_x + bar_w / 2
        bar_center_y = bar_y + bar_h / 2

        # Центр САМОГО ROI (не центр экрана!) — насколько бар отклонился
        # от точки, где мы ОЖИДАЛИ его увидеть. ROI сам смещён от центра
        # экрана офсетами из конфига (HP-бар обычно не строго в центре
        # экрана), но для доворота камеры важна разница именно от центра
        # ROI — это и есть "насколько цель уехала от привычного места".
        roi_center_x = roi_coords["width"] / 2
        roi_center_y = roi_coords["height"] / 2

        offset_x = bar_center_x - roi_center_x
        offset_y = bar_center_y - roi_center_y

        return TargetInfo(found=True, hp_percent=hp, offset_x=offset_x, offset_y=offset_y)

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
            info = vision.get_target_info(TEST_ROI)

            status = (
                f"HP: {info.hp_percent:.1f}% | offset=({info.offset_x:+.0f}, {info.offset_y:+.0f})px"
                if info.found else "Таргет не найден"
            )
            print(status)

            frame = vision._capture(TEST_ROI)
            if frame is not None:
                debug_frame = cv2.resize(
                    frame,
                    (TEST_ROI["width"] * 4, TEST_ROI["height"] * 4),
                    interpolation=cv2.INTER_NEAREST,
                )

                # Центр ROI — опорная точка, от которой считается offset.
                # Рисуем крестом, чтобы визуально видеть, куда "должен"
                # попадать бар, и насколько он от этой точки уехал.
                roi_cx = TEST_ROI["width"] * 4 // 2
                roi_cy = TEST_ROI["height"] * 4 // 2
                cv2.drawMarker(debug_frame, (roi_cx, roi_cy), (255, 0, 0),
                                markerType=cv2.MARKER_CROSS, markerSize=20, thickness=2)

                if info.found:
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
                        # Линия от центра ROI к центру бара — наглядно
                        # показывает direction/magnitude offset_x/offset_y.
                        bar_cx = (x + w // 2) * 4
                        bar_cy = (y + h // 2) * 4
                        cv2.line(debug_frame, (roi_cx, roi_cy), (bar_cx, bar_cy), (0, 255, 255), 2)

                cv2.imshow("VisionManager Debug", debug_frame)

            if cv2.waitKey(1) & 0xFF == ord("q"):
                break

            time.sleep(1 / 60)

    finally:
        cv2.destroyAllWindows()
        vision.close()
        print("Песочница закрыта.")
