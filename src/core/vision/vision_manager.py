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

        Непрерывность цели (self._last_bar_center): после расширения ROI
        под всю зону охоты (см. vision_config — теперь зона покрывает
        большую часть экрана, а не узкую полосу у центра) в кадре обычно
        одновременно несколько похожих по цвету животных. Правило "бери
        самый ШИРОКИЙ подходящий по форме блоб" в такой зоне на каждом
        кадре может независимо выбрать РАЗНОЕ животное — реальный симптом,
        подтверждённый логом: hp_percent скачет 100%->18%->58%->100% туда-
        сюда, чего у одной живой цели физически быть не может. Фикс —
        _find_hp_bar() между кадрами предпочитает кандидата БЛИЖАЙШЕГО к
        прошлой позиции бара (см. _MAX_BAR_JUMP_PX), а не просто самого
        широкого: настоящий HP-бар не телепортируется через весь экран за
        один тик (16мс на 60 FPS), а другое животное рядом — телепортируется
        относительно ПРОШЛОЙ позиции старой цели, потому что это другой
        объект. reset_tracking() явно вызывается из FSM (bot.py) в момент
        ПОДТВЕРЖДЁННОЙ смерти цели (после грейс-периода) — иначе "память"
        держалась бы за координаты мёртвой цели и мешала бы честно
        зацепиться за самого широкого кандидата при поиске новой.
    """

    # Максимальное смещение центра бара МЕЖДУ СОСЕДНИМИ тиками (не между
    # секундами лога — реальный get_target_info() дёргается ~60 раз/сек
    # из FSM), при котором кандидат ещё считается "тем же" баром, что и
    # на прошлом кадре, а не другим животным поблизости. 120px за 16мс —
    # это уже ~7500px/сек кажущейся скорости, с большим запасом больше,
    # чем может дать сглаженное движение камеры (_aim_worker) или бег
    # моба — щедрый порог на ложные срабатывания, но всё ещё на порядок
    # меньше типичного "прыжка" между двумя разными животными в широком
    # ROI (сотни px, см. докстринг класса).
    _MAX_BAR_JUMP_PX = 120.0

    # --- Проверка "таблички с именем" над кандидатом (2026-09-28) ---
    #
    # Почему это появилось: фильтр по форме (плотность + соотношение
    # сторон) не отличает настоящий HP-бар от случайного декоративного
    # объекта в мире (цветок, метка ресурса на земле) — оба могут быть
    # нужного цвета и формы. Но у НАСТОЯЩЕГО бара выбранной цели ВСЕГДА
    # есть текстовая табличка с именем моба прямо над полоской — а у
    # декорации в мире текста рядом никогда не бывает. Это качественно
    # более надёжный признак, чем просто "форма похожа".
    #
    # Полноценное распознавание текста (OCR) на каждый кадр и каждого
    # кандидата убило бы FPS — счёт идёт на микросекунды, а не на
    # десятки миллисекунд. Поэтому здесь НЕ читаем текст, а лишь считаем
    # "плотность граней" (edge density) в зоне над кандидатом через
    # cv2.Canny: у настоящего текста всегда много коротких контрастных
    # граней на маленькой площади (границы букв), а у травы/камня/цветка
    # — почти нет. Canny на маленьком кропе (не на всём кадре) — это
    # доли миллисекунды, укладывается в 60 FPS с большим запасом.

    # Высота зоны проверки НАД баром, в долях от высоты самого бара h.
    # Табличка с именем в T&L заметно выше самой полоски ХП — подбирай
    # через песочницу (__main__ ниже), если промахнёмся мимо реального
    # текста или наоборот захватим слишком много ROI сверху.
    _NAMEPLATE_HEIGHT_RATIO = 2.0

    # Горизонтальный запас по бокам, px — имя часто чуть шире/уже самого
    # бара, поэтому берём зону проверки чуть шире габаритов кандидата.
    _NAMEPLATE_PADDING_PX = 20

    # Порог доли "граневых" пикселей в зоне, начиная с которого считаем,
    # что там есть текстоподобная структура. Стартовое значение для
    # подбора в песочнице — НЕ калибровано на реальных скриншотах T&L,
    # это первая цифра для проверки, а не финальная константа.
    _NAMEPLATE_MIN_EDGE_DENSITY = 0.06

    def __init__(self) -> None:
        self._load_config()
        self.sct = mss.mss()

        monitor        = self.sct.monitors[1]
        self.center_x  = monitor["left"] + monitor["width"]  // 2
        self.center_y  = monitor["top"]  + monitor["height"] // 2

        # "Память" о последней позиции бара В КООРДИНАТАХ КАДРА (не
        # экрана) — см. докстринг класса про непрерывность цели. None,
        # пока ни одного бара ещё не видели, или после reset_tracking().
        self._last_bar_center: "tuple[float, float] | None" = None

        # Антидребезг панели цели (2026-10-02, баг: комбо обрывалось на
        # первом скилле + RB+Y сбивало живую цель). См. docstring
        # get_panel_target_info() — короткая версия: _panel_miss_streak
        # считает подряд идущие кадры БЕЗ находки, _last_panel_info хранит
        # последнее ДОСТОВЕРНОЕ (found=True) показание панели, которое
        # отдаётся наружу вместо свежего "пусто", пока streak не набрал
        # _panel_miss_debounce_frames. None здесь означает "панель вообще
        # ни разу не найдена с последнего reset_tracking()" — тогда
        # отдавать нечего, честно возвращаем found=False с первого кадра.
        self._panel_miss_streak = 0
        self._last_panel_info: "TargetInfo | None" = None

        logger.debug(
            "VisionManager инициализирован. Центр экрана: (%d, %d)",
            self.center_x, self.center_y
        )

    def reset_tracking(self) -> None:
        """
        Сбрасывает "память" о последней позиции бара — вызывай из FSM в
        момент ПОДТВЕРЖДЁННОЙ смерти цели (тот же момент, что и
        input_manager.clear_aim_target(), см. bot.py._handle_combat).
        Без явного сброса здесь _find_hp_bar() продолжал бы сравнивать
        новых кандидатов с координатами уже мёртвой цели и там, где давно
        никого нет — а нужно честно взять самого широкого кандидата
        заново, как при самом первом обнаружении.

        Заодно сбрасывает антидребезг панели (_panel_miss_streak/
        _last_panel_info, см. __init__ и get_panel_target_info) — иначе
        после подтверждённой смерти бот мог бы ещё несколько кадров
        отдавать наружу found=True с HP МЁРТВОЙ цели (последнее
        достоверное показание), пока новый streak не набежит заново. Тот
        же момент вызова, что и у _last_bar_center — оба "не верь старым
        данным" должны сбрасываться синхронно.
        """
        self._last_bar_center = None
        self._panel_miss_streak = 0
        self._last_panel_info = None

    def _load_config(self) -> None:
        self._red_lower_1 = np.array(vision_config.RED_LOWER_1, dtype=np.uint8)
        self._red_upper_1 = np.array(vision_config.RED_UPPER_1, dtype=np.uint8)
        self._red_lower_2 = np.array(vision_config.RED_LOWER_2, dtype=np.uint8)
        self._red_upper_2 = np.array(vision_config.RED_UPPER_2, dtype=np.uint8)

        # Добавляем желтый цвет
        self._yellow_lower = np.array(vision_config.YELLOW_LOWER, dtype=np.uint8)
        self._yellow_upper = np.array(vision_config.YELLOW_UPPER, dtype=np.uint8)

        self._max_hp_width = float(vision_config.MAX_TARGET_HP_WIDTH)
        self._max_panel_bar_width = float(vision_config.MAX_TARGET_PANEL_BAR_WIDTH)
        self._panel_miss_debounce_frames = int(vision_config.PANEL_MISS_DEBOUNCE_FRAMES)

        # Офсеты панели — нужны ЗДЕСЬ тоже (не только в get_panel_target_info),
        # см. _mask_out_panel(): мировая зона TARGET_HP_* геометрически
        # накрывает панель целиком, и без маскировки _find_hp_bar() мог
        # поймать HP-бар самой панели как будто это летающая табличка над
        # мобом (у панели есть настоящий текст сверху — она честно проходит
        # _has_nameplate_above(), фильтр её не отсеивает).
        self._panel_offset_x = int(vision_config.TARGET_PANEL_OFFSET_X)
        self._panel_offset_y = int(vision_config.TARGET_PANEL_OFFSET_Y)
        self._panel_width = int(vision_config.TARGET_PANEL_WIDTH)
        self._panel_height = int(vision_config.TARGET_PANEL_HEIGHT)

        logger.debug("VisionManager: HSV-конфиг загружен.")

    def _mask_out_panel(self, frame: "np.ndarray", roi_coords: dict) -> None:
        """
        Зануляет область панели цели ВНУТРИ кадра МИРОВОЙ зоны (см.
        комментарий в _load_config про TARGET_PANEL_* внутри TARGET_HP_*).
        Вызывается ТОЛЬКО из get_target_info() — у get_panel_target_info()
        свой, отдельный захват именно зоны панели, маскировать там нечего
        (панель там и ДОЛЖНА быть видна).

        Считает прямоугольник панели В ЛОКАЛЬНЫХ координатах переданного
        кадра (не в координатах экрана) заново на каждый вызов, а не
        кеширует готовый прямоугольник в __init__ — потому что он зависит
        от roi_coords МИРОВОЙ зоны, а не только от самой панели, и дешевле
        посчитать 4 вычитания, чем держать синхронизацию кеша с каждым
        местом, откуда может прийти другой roi_coords.

        Мутирует frame IN PLACE (без возврата копии) — лишняя аллокация
        полного кадра 60 раз в секунду того не стоит.
        """
        panel_left = (self.center_x + self._panel_offset_x) - roi_coords["left"]
        panel_top = (self.center_y + self._panel_offset_y) - roi_coords["top"]

        # Клип к границам кадра — если при будущей перекалибровке панель
        # окажется ЧАСТИЧНО или ПОЛНОСТЬЮ вне мировой зоны, маска должна
        # тихо ничего не делать на той части, а не упасть по IndexError на
        # отрицательном/слишком большом срезе.
        x0 = max(0, panel_left)
        y0 = max(0, panel_top)
        x1 = min(frame.shape[1], panel_left + self._panel_width)
        y1 = min(frame.shape[0], panel_top + self._panel_height)

        if x1 > x0 and y1 > y0:
            frame[y0:y1, x0:x1] = 0

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
        Летающий бар НАД ГОЛОВОЙ моба (мировая зона, TARGET_HP_* в
        конфиге). ВАЖНО (2026-10-02): с появлением get_panel_target_info()
        ниже этот метод больше НЕ источник правды для "жив ли таргет" —
        этим занимается панель цели (см. докстринг класса и
        get_panel_target_info). Этот метод остаётся ТОЛЬКО ради
        offset_x/offset_y — больше ничего не вернёт панель цели (она не
        привязана к позиции моба на экране), а camera aim без координат
        работать не может. found/hp_percent отсюда для FSM больше не
        используются напрямую (хотя технически считаются и возвращаются —
        убирать их из dataclass не стал, чтобы не ломать отладочные логи
        и __main__-песочницу, которые их тоже печатают).
        """
        empty = TargetInfo(found=False, hp_percent=0.0, offset_x=0.0, offset_y=0.0)

        frame = self._capture(roi_coords)
        if frame is None:
            return empty

        # См. _mask_out_panel(): панель цели физически лежит ВНУТРИ этой
        # огромной зоны — без маски её собственный HP-бар иногда ловился
        # здесь как будто это летающая табличка над мобом (баг: камера
        # бесконечно доворачивала к неподвижной панели, см. комментарий
        # в _load_config). Вызываем ДО _find_hp_bar(), чтобы маскированные
        # пиксели вообще не участвовали в HSV-поиске кандидатов.
        self._mask_out_panel(frame, roi_coords)

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

    def get_panel_target_info(self, roi_coords: dict) -> TargetInfo:
        """
        Источник правды для found/hp_percent (2026-10-02) — панель цели:
        отдельный от летающего бара элемент интерфейса (имя+уровень+
        замочек+бар+дистанция), который висит на ФИКСИРОВАННОМ месте
        экрана и появляется ТОЛЬКО когда реально есть выбранная цель (см.
        vision_config.TARGET_PANEL_* и докстринг класса).

        ОБНОВЛЕНО (2026-10-02, баг от пользователя: цепочка скиллов
        обрывалась на первом скилле, а SEARCH спамил RB+Y по живой цели).
        Причина была в том, что этот метод отдавал found=False буквально
        на первом же кадре, где _collect_bar_candidates() ничего не
        нашёл — а такой кадр у маленькой панели абсолютно нормален: эффект
        каста, частицы, анимация попадания могут на 1-2 кадра перекрыть
        её ровно в момент боя, когда цена ложного "цель умерла" выше
        всего. У мирового бара была своя защита от шума (непрерывность,
        self._last_bar_center) — у панели её не было вообще, хотя
        докстринг _handle_combat() в bot.py ошибочно считал её "более
        стабильной по природе". Теперь у панели СВОЯ защита того же духа:
        см. _panel_miss_streak/_last_panel_info в __init__ и
        _scan_panel_once() ниже. Грейс-период в bot.py (_TARGET_LOST_GRACE_S)
        никуда не делся — остаётся второй, более грубой страховкой уже на
        уровне FSM, а не единственной защитой, как было раньше.

        offset_x/offset_y в результате НЕ значат "куда доворачивать
        камеру" — панель не привязана к положению моба на экране, для
        прицеливания по-прежнему используй get_target_info() (мировая
        зона). Поля заполнены только для единообразия TargetInfo и
        отладочных логов.
        """
        raw = self._scan_panel_once(roi_coords)

        if raw.found:
            # Свежая, подтверждённая находка — сбрасываем счётчик пропусков
            # и запоминаем ЭТО показание как "последнее достоверное" на
            # случай, если следующий кадр(ы) окажутся шумом.
            self._panel_miss_streak = 0
            self._last_panel_info = raw
            return raw

        # Кадр без находки. Не доверяем ему немедленно — считаем подряд
        # идущие пропуски и сравниваем с порогом из конфига.
        self._panel_miss_streak += 1

        still_within_debounce = (
            self._panel_miss_streak < self._panel_miss_debounce_frames
        )
        if still_within_debounce and self._last_panel_info is not None:
            # Пропуск внутри допустимого окна шума И до этого реально была
            # цель — отдаём наружу СТАРОЕ достоверное показание, а не этот
            # пустой кадр. Для FSM это выглядит как "цель всё ещё здесь",
            # что и правильно: один моргнувший кадр — не смерть.
            return self._last_panel_info

        # Либо порог пропусков исчерпан (моргание слишком долгое, похоже
        # на настоящую пропажу), либо панели не было вообще ни разу с
        # последнего reset_tracking() — в обоих случаях честно отдаём
        # "пусто" и больше не держимся за устаревшее достоверное значение.
        self._last_panel_info = None
        return raw

    def _scan_panel_once(self, roi_coords: dict) -> TargetInfo:
        """
        Один СЫРОЙ, без антидребезга, скан зоны панели цели. Вынесен из
        get_panel_target_info() отдельным методом, чтобы антидребезг (там)
        не путался с самой детекцией (здесь) в одном теле функции — другая
        причина изменения (калибровка порога кадров vs логика самого
        зрения) правит свой метод, не трогая второй.
        """
        empty = TargetInfo(found=False, hp_percent=0.0, offset_x=0.0, offset_y=0.0)

        frame = self._capture(roi_coords)
        if frame is None:
            return empty

        candidates = self._collect_bar_candidates(frame)
        if not candidates:
            return empty

        bar_x, bar_y, bar_w, bar_h = max(candidates, key=lambda c: c[2])

        hp = min((bar_w / self._max_panel_bar_width) * 100.0, 100.0)
        hp = round(hp, 2)

        bar_center_x = bar_x + bar_w / 2
        bar_center_y = bar_y + bar_h / 2
        roi_center_x = roi_coords["width"] / 2
        roi_center_y = roi_coords["height"] / 2

        return TargetInfo(
            found=True,
            hp_percent=hp,
            offset_x=bar_center_x - roi_center_x,
            offset_y=bar_center_y - roi_center_y,
        )

    def _collect_bar_candidates(self, frame: "np.ndarray") -> "list[tuple[int,int,int,int]]":
        """
        Считает HSV-маску и отдаёт ВСЕХ кандидатов, прошедших фильтры формы
        (плотность + соотношение сторон) — БЕЗ выбора финального бара, это
        отдельный шаг в _find_hp_bar().

        Вынесено в отдельный метод не просто для красоты: отладочная
        песочница (__main__ ниже) зовёт именно этот метод, чтобы нарисовать
        ВСЕХ кандидатов в окне отладки, а не только финальный выбор. Так
        сразу видно на реальном скриншоте: фильтр в принципе видит
        настоящую полоску ХП (она просто не была выбрана финальной) — или
        не видит её вовсе, и рамка ловит только шум (тело моба, ландшафт).
        Копировать сюда логику маски отдельно от боевого пути было бы
        опасно — тогда песочница тестировала бы не тот код, что реально
        работает в бою, а его устаревшую копию.
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

        # Собираем ВСЕХ кандидатов, прошедших фильтры формы, а не сразу
        # сворачиваем в "самого широкого". Выбор финального бара — уже в
        # _find_hp_bar(), с учётом непрерывности.
        candidates: list[tuple[int, int, int, int]] = []

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

            candidates.append((x, y, w, h))

        return candidates

    def _has_nameplate_above(self, frame: "np.ndarray", bar: "tuple[int,int,int,int]") -> bool:
        """
        Проверяет, есть ли НАД кандидатом что-то похожее на текстовую
        табличку с именем цели (см. докстринг констант _NAMEPLATE_* выше
        про то, почему это надёжнее чистой проверки формы, и почему это
        НЕ полноценный OCR — только плотность граней через cv2.Canny).
        """
        x, y, w, h = bar

        plate_h = int(h * self._NAMEPLATE_HEIGHT_RATIO)
        plate_y0 = max(0, y - plate_h)
        plate_y1 = y
        plate_x0 = max(0, x - self._NAMEPLATE_PADDING_PX)
        plate_x1 = min(frame.shape[1], x + w + self._NAMEPLATE_PADDING_PX)

        if plate_y1 <= plate_y0 or plate_x1 <= plate_x0:
            # Кандидат прижат к самому верху ROI — некуда смотреть выше
            # него. Лучше честно отказать, чем гадать по пустой зоне.
            return False

        region = frame[plate_y0:plate_y1, plate_x0:plate_x1]
        gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray, 80, 160)

        edge_density = np.count_nonzero(edges) / edges.size
        return edge_density >= self._NAMEPLATE_MIN_EDGE_DENSITY

    def _find_hp_bar(self, frame: "np.ndarray") -> "tuple[int,int,int,int] | None":
        """
        Ищет HP-бар таргета в кадре: берёт кандидатов из
        _collect_bar_candidates(), затем ОТСЕИВАЕТ тех, над кем нет
        текстоподобной таблички с именем (_has_nameplate_above) — это и
        есть основной фильтр от декоративных объектов мира (цветы, метки
        ресурсов), у которых нужный цвет/форма могут случайно совпасть с
        баром, но текста рядом никогда не будет.

        Среди прошедших ОБЕ проверки кандидатов выбор идёт в два шага
        (см. докстринг класса про self._last_bar_center):
          1. Если есть "память" о прошлой позиции — берём БЛИЖАЙШЕГО к ней
             кандидата, а не самого широкого, при условии что он не дальше
             _MAX_BAR_JUMP_PX (иначе это не продолжение прежней цели, а
             какой-то другой объект оказался ближе всех остальных, но всё
             равно слишком далеко от места, где реально была цель).
          2. Если памяти нет (первый кадр вообще, или после
             reset_tracking() при подтверждённой смерти) ИЛИ ни один
             кандидат не прошёл проверку по расстоянию — берём самого
             широкого, как раньше: это "первый контакт" с новой целью,
             сравнивать пока не с чем.
        """
        candidates = self._collect_bar_candidates(frame)
        candidates = [c for c in candidates if self._has_nameplate_above(frame, c)]

        if not candidates:
            # НЕ трогаем self._last_bar_center здесь — одиночный кадр без
            # кандидатов (моб на миг за деревом/спиной) не должен обнулять
            # память, иначе непрерывность ломалась бы от того же шума,
            # который мы и пытаемся пережить.
            return None

        best = None
        if self._last_bar_center is not None:
            last_x, last_y = self._last_bar_center

            def _dist_to_last(c: "tuple[int, int, int, int]") -> float:
                cx, cy, cw, ch = c
                bar_cx, bar_cy = cx + cw / 2, cy + ch / 2
                return ((bar_cx - last_x) ** 2 + (bar_cy - last_y) ** 2) ** 0.5

            nearest = min(candidates, key=_dist_to_last)
            if _dist_to_last(nearest) <= self._MAX_BAR_JUMP_PX:
                best = nearest

        if best is None:
            # Памяти нет, или ни один кандидат не похож на продолжение
            # прежней цели — "первый контакт", берём самого широкого,
            # как и раньше.
            best = max(candidates, key=lambda c: c[2])

        bx, by, bw, bh = best
        self._last_bar_center = (bx + bw / 2, by + bh / 2)
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


_DEBUG_WINDOW_NAME = "VisionManager Debug"

# Верхний предел зума окна отладки — старое поведение было "всегда x4",
# что при широком ROI (сейчас 1407x411 под всю зону охоты) даёт окно
# 5628x1644px — больше, чем разрешение почти любого монитора целиком.
# Поэтому это теперь именно ВЕРХНИЙ предел, а не фиксированный множитель:
# реальный масштаб ниже всегда пересчитывается под фактический размер
# второго монитора (см. _fit_debug_window).
_DEBUG_MAX_ZOOM = 4.0

# Оставляем небольшой отступ от края монитора (панель задач, рамка окна
# от Windows) — 100% ширины/высоты монитора визуально даёт окно, которое
# либо не помещается, либо упирается в панель задач.
_DEBUG_SCREEN_MARGIN = 0.9


def _fit_debug_window(monitors: "list[dict]", roi_width: int, roi_height: int) -> "tuple[dict, float]":
    """
    Выбирает монитор для окна отладки и масштаб, при котором окно
    гарантированно влезает в его реальное разрешение.

    monitors — это vision.sct.monitors (тот же mss, что и для захвата
    экрана, не отдельный источник правды). monitors[0] — виртуальный
    "весь рабочий стол", monitors[1] — первый физический монитор (на нём
    же и считается center_x/center_y в __init__), monitors[2] — второй
    физический монитор, если он подключён.

    Никаких захардкоженных координат/пикселей монитора — только то, что
    реально вернула Windows через mss на момент запуска. Если второго
    монитора нет физически, тихо переключаться на первый было бы плохой
    идеей (можно долго не понять, почему "второй монитор не подключается"),
    поэтому явно предупреждаем в консоль.
    """
    if len(monitors) > 2:
        target = monitors[2]
    else:
        target = monitors[1]
        print("[vision_manager] Второй монитор не найден (mss видит только "
              f"{len(monitors) - 1} физических монитор(а)) — показываю окно "
              "отладки на первом.")

    # Масштаб ограничен СВЕРХУ значением _DEBUG_MAX_ZOOM (не нужно, чтобы
    # маленький ROI открывался микроскопическим окном 1:1), но никогда не
    # больше того, что реально влезает в выбранный монитор с отступом.
    scale = min(
        _DEBUG_MAX_ZOOM,
        (target["width"] * _DEBUG_SCREEN_MARGIN) / roi_width,
        (target["height"] * _DEBUG_SCREEN_MARGIN) / roi_height,
    )
    # Защита от вырожденного случая (экзотическое разрешение/ROI больше
    # монитора в разы) — не даём масштабу уйти к нулю или в минус.
    scale = max(scale, 0.1)

    return target, scale


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)

    vision = VisionManager()

    TEST_ROI = vision.build_roi(
        offset_x=vision_config.TARGET_HP_OFFSET_X,
        offset_y=vision_config.TARGET_HP_OFFSET_Y,
        width=vision_config.TARGET_HP_WIDTH,
        height=vision_config.TARGET_HP_HEIGHT,
    )

    target_monitor, debug_scale = _fit_debug_window(
        vision.sct.monitors, TEST_ROI["width"], TEST_ROI["height"]
    )
    debug_w = int(TEST_ROI["width"] * debug_scale)
    debug_h = int(TEST_ROI["height"] * debug_scale)

    # WINDOW_NORMAL (а не дефолтный WINDOW_AUTOSIZE от простого imshow) —
    # только с ним moveWindow/resizeWindow реально работают и окно потом
    # можно ещё и руками подвинуть/растянуть мышью, если нужно.
    cv2.namedWindow(_DEBUG_WINDOW_NAME, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(_DEBUG_WINDOW_NAME, debug_w, debug_h)
    cv2.moveWindow(_DEBUG_WINDOW_NAME, target_monitor["left"], target_monitor["top"])
    # Позиционируем ОДИН раз до входа в цикл 60 FPS, а не на каждый кадр —
    # moveWindow/resizeWindow дёргают оконный менеджер Windows, это не
    # бесплатно, а окну не нужно "плыть" каждый тик.

    # --- Второе окно отладки — панель цели (2026-10-02, см. докстринг
    # get_panel_target_info) --- Зона маленькая, поэтому зум побольше
    # (_DEBUG_MAX_ZOOM тут не подходит — тот рассчитан под огромный
    # мировой ROI, панель на порядок меньше, нужен свой масштаб).
    PANEL_ROI = vision.build_roi(
        offset_x=vision_config.TARGET_PANEL_OFFSET_X,
        offset_y=vision_config.TARGET_PANEL_OFFSET_Y,
        width=vision_config.TARGET_PANEL_WIDTH,
        height=vision_config.TARGET_PANEL_HEIGHT,
    )
    _PANEL_DEBUG_MAX_ZOOM = 6.0
    panel_scale = min(
        _PANEL_DEBUG_MAX_ZOOM,
        (target_monitor["width"] * _DEBUG_SCREEN_MARGIN) / PANEL_ROI["width"],
        (target_monitor["height"] * _DEBUG_SCREEN_MARGIN) / PANEL_ROI["height"],
    )
    panel_scale = max(panel_scale, 0.1)
    panel_debug_w = int(PANEL_ROI["width"] * panel_scale)
    panel_debug_h = int(PANEL_ROI["height"] * panel_scale)

    _PANEL_DEBUG_WINDOW_NAME = "VisionManager Panel Debug"
    cv2.namedWindow(_PANEL_DEBUG_WINDOW_NAME, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(_PANEL_DEBUG_WINDOW_NAME, panel_debug_w, panel_debug_h)
    # Ставим правее основного окна отладки, на том же мониторе — если не
    # влезает (слишком узкий монитор), ляжет поверх, просто подвинь руками.
    cv2.moveWindow(
        _PANEL_DEBUG_WINDOW_NAME,
        target_monitor["left"] + debug_w + 20,
        target_monitor["top"],
    )

    print(f"Центр экрана: ({vision.center_x}, {vision.center_y})")
    print(f"Тестовый ROI (мировой бар): {TEST_ROI}")
    print(f"Тестовый ROI (панель цели): {PANEL_ROI}")
    print(f"Окно отладки: монитор ({target_monitor['left']}, {target_monitor['top']}), "
          f"масштаб x{debug_scale:.2f}, размер {debug_w}x{debug_h}px")
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
                    (debug_w, debug_h),
                    interpolation=cv2.INTER_NEAREST,
                )

                # Центр ROI — опорная точка, от которой считается offset.
                # Рисуем крестом, чтобы визуально видеть, куда "должен"
                # попадать бар, и насколько он от этой точки уехал.
                roi_cx = debug_w // 2
                roi_cy = debug_h // 2
                cv2.drawMarker(debug_frame, (roi_cx, roi_cy), (255, 0, 0),
                                markerType=cv2.MARKER_CROSS, markerSize=20, thickness=2)

                # ДИАГНОСТИКА (2026-09-28, дополнено проверкой таблички с
                # именем): три уровня кандидатов, три цвета рамок.
                #   КРАСНЫЙ тонкий  — прошёл фильтр формы, но НЕТ таблички
                #                     с именем над ним (значит это НЕ бар —
                #                     декорация мира, цветок, метка и т.п.)
                #   ГОЛУБОЙ тонкий  — прошёл И форму, И табличку с именем,
                #                     но не стал финальным выбором (значит
                #                     непрерывность выбрала другого — можно
                #                     разбираться дальше в этой логике)
                #   ЗЕЛЁНЫЙ толстый — финальный выбор (см. ниже)
                shape_candidates = vision._collect_bar_candidates(frame)
                verified_candidates = [
                    c for c in shape_candidates if vision._has_nameplate_above(frame, c)
                ]
                bar = vision._find_hp_bar(frame)  # финальный выбор — та же логика, что и в бою

                for cx, cy, cw, ch in shape_candidates:
                    if (cx, cy, cw, ch) in verified_candidates:
                        continue  # эти рисуем ниже, голубым
                    cv2.rectangle(
                        debug_frame,
                        (int(cx * debug_scale), int(cy * debug_scale)),
                        (int((cx + cw) * debug_scale), int((cy + ch) * debug_scale)),
                        (0, 0, 255),
                        1,
                    )

                for cx, cy, cw, ch in verified_candidates:
                    if (cx, cy, cw, ch) == bar:
                        continue  # его отрисуем отдельно ниже — толще и зелёным
                    cv2.rectangle(
                        debug_frame,
                        (int(cx * debug_scale), int(cy * debug_scale)),
                        (int((cx + cw) * debug_scale), int((cy + ch) * debug_scale)),
                        (255, 255, 0),
                        1,
                    )

                if bar is not None:
                    x, y, w, h = bar
                    cv2.rectangle(
                        debug_frame,
                        (int(x * debug_scale), int(y * debug_scale)),
                        (int((x + w) * debug_scale), int((y + h) * debug_scale)),
                        (0, 255, 0),
                        2,
                    )
                    # Линия от центра ROI к центру бара — наглядно
                    # показывает direction/magnitude offset_x/offset_y.
                    bar_cx = int((x + w / 2) * debug_scale)
                    bar_cy = int((y + h / 2) * debug_scale)
                    cv2.line(debug_frame, (roi_cx, roi_cy), (bar_cx, bar_cy), (0, 255, 255), 2)

                cv2.imshow(_DEBUG_WINDOW_NAME, debug_frame)

            # --- Второе окно: панель цели (found/hp_percent — новый
            # источник правды, см. get_panel_target_info) ---
            panel_info = vision.get_panel_target_info(PANEL_ROI)
            panel_status = (
                f"ПАНЕЛЬ: HP {panel_info.hp_percent:.1f}%"
                if panel_info.found else "ПАНЕЛЬ: цели нет"
            )
            print(panel_status)

            panel_frame = vision._capture(PANEL_ROI)
            if panel_frame is not None:
                panel_debug_frame = cv2.resize(
                    panel_frame,
                    (panel_debug_w, panel_debug_h),
                    interpolation=cv2.INTER_NEAREST,
                )
                if panel_info.found:
                    panel_candidates = vision._collect_bar_candidates(panel_frame)
                    if panel_candidates:
                        px, py, pw, ph = max(panel_candidates, key=lambda c: c[2])
                        cv2.rectangle(
                            panel_debug_frame,
                            (int(px * panel_scale), int(py * panel_scale)),
                            (int((px + pw) * panel_scale), int((py + ph) * panel_scale)),
                            (0, 255, 0),
                            2,
                        )
                cv2.imshow(_PANEL_DEBUG_WINDOW_NAME, panel_debug_frame)

            if cv2.waitKey(1) & 0xFF == ord("q"):
                break

            time.sleep(1 / 60)

    finally:
        cv2.destroyAllWindows()
        vision.close()
        print("Песочница закрыта.")