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
from src.config import aim_config, panel_template, vision_config
from src.core.vision.panel_detector import PanelResult, read_panel
from src.core.vision.target_list import TargetList, read_target_list
from src.core.vision import world_numbers

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
    # Для МИРОВОГО бара (get_target_info): куда сместилось ТЕЛО моба
    # относительно ТОЧКИ ПРИЦЕЛА игры (белая точка, см. aim_config.py), px.
    # + по X = моб правее прицела, + по Y = ниже. Нули = прицел в мобе.
    # (Раньше отсчёт шёл от центра зоны поиска — он не совпадал с прицелом.)
    offset_x: float
    offset_y: float
    # Отпечаток ТЕКСТА HP на панели (hp_text.HpText) или None — нет данных (только
    # панель; мировой бар и тестовые заглушки его не заполняют). По умолчанию None:
    # старый код и фейки, строившие TargetInfo из четырёх полей, продолжают работать.
    hp_text: "object | None" = None


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

    # --- Липкий захват (2026-10-02) ---
    # Баг по логу: панель цели стабильно показывала ТУ ЖЕ цель, а мировой бар
    # за один кадр "прыгал" на 450 px — камера разворачивалась к ДРУГОМУ
    # мобу. Причина: если нужный бар пропадал на кадр (моб за деревом,
    # мигание таблички), рядом с прошлой позицией (<= _MAX_BAR_JUMP_PX)
    # кандидатов не было, и код падал на правило "возьми самый широкий бар
    # во всей зоне" — то есть чужой, — и ПЕРЕЗАПИСЫВАЛ память о цели.
    #
    # Теперь, пока захват есть, чужие бары не рассматриваются вообще: либо
    # нашли бар РЯДОМ с прежним (радиус растёт, пока бар потерян — моб мог
    # пробежать), либо возвращаем "не найдено", и камера просто стоит.
    # Захват снимается, если бара рядом нет дольше _LOCK_RELEASE_S, или
    # явно: reset_tracking() (смерть цели) / release_bar_lock().
    _LOCK_RELEASE_S = 1.5
    _LOCK_RADIUS_GROWTH_PX_S = 250.0
    _LOCK_RADIUS_MAX_PX = 300.0

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

    # --- Минимальный размер бара при ПЕРВОМ контакте (2026-10-02) ---
    # Вторая линия защиты от мусора после проверки цвета (см. фильтр 4 в
    # _collect_bar_candidates): мелочь вроде цветов и пятен сухой земли —
    # это 12-30 px в ширину, а полоска настоящего моба на Full HD — около
    # 85-90 px при полном HP. Пока захвата нет, кандидат должен быть не
    # меньше этих порогов (запас — раненый моб с ~45% HP ещё проходит). С
    # захватом (бар уже ведём) пороги НЕ применяются: у почти мёртвого моба
    # заливка короткая, и терять его нельзя. Не угадал — песочница рисует
    # отброшенных серым: смотри их размеры и поправь числа ниже.
    #
    # 2026-10-03 (выбор ближнего моба зрением): порог ширины поднят 40 -> 60, и
    # добавлен потолок высоты. Причины по кадрам 16:37:
    #   - 43-px "обрубок" (803,312) у самого бара выбранной цели — чужой бар,
    #     наполовину закрытый табличкой выбранной; клик по нему снова брал её же;
    #   - бары у края зоны (42-63 px) обрезаны рамкой: центр неверный, камера
    #     доворачивала мимо;
    #   - моб с заливкой < 70% почти всегда уже чей-то (его бьёт другой игрок).
    # Потолок высоты: после ослабленных фильтров формы (extent 0.1, w/h 1.5) в
    # кандидаты попадают слипшиеся пятна "стрелка метки + соседний бар" (78x20).
    # Полоска ХП — 3-7 px в высоту, 9 — с запасом.
    _WORLD_FIRST_CONTACT_MIN_W = 60
    _WORLD_FIRST_CONTACT_MIN_H = 3
    _WORLD_FIRST_CONTACT_MAX_H = 9
    # Бар "прилип" к выбранной цели: по X ближе полусуммы ширин + этот запас,
    # по Y — ближе _STUCK_TO_MARKED_DY_PX. Прицел такие тела не различает (клик
    # возьмёт выбранную), поэтому для переключения они не годятся.
    _STUCK_TO_MARKED_GAP_PX = 10
    _STUCK_TO_MARKED_DY_PX = 30

    def __init__(self) -> None:
        self._load_config()
        self.sct = mss.mss()

        monitor        = self.sct.monitors[1]
        self.center_x  = monitor["left"] + monitor["width"]  // 2
        self.center_y  = monitor["top"]  + monitor["height"] // 2
        # Размер экрана нужен для перевода долей из aim_config в пиксели.
        self.screen_w  = monitor["width"]
        self.screen_h  = monitor["height"]

        # Ручная подстройка прицела из песочницы (клавиши i/j/k/l), px.
        # В бою всегда (0, 0): боевой код её не трогает, только песочница.
        self.aim_nudge_px = [0.0, 0.0]

        # "Память" о последней позиции бара В КООРДИНАТАХ КАДРА (не
        # экрана) — см. докстринг класса про непрерывность цели. None,
        # пока ни одного бара ещё не видели, или после reset_tracking().
        self._last_bar_center: "tuple[float, float] | None" = None

        # С какого момента (time.monotonic()) подходящего бара рядом с
        # захватом нет; None — бар на месте (или захвата нет). См. _LOCK_*.
        self._lock_lost_since: "float | None" = None

        # Диагностика для лога бота: сколько кандидатов прошло ВСЕ проверки
        # в последнем кадре и есть ли захват. Читает bot._log_vision_status.
        self.last_candidate_count = 0
        # Сколько баров в последнем кадре признаны ВЫБРАННОЙ целью (красные
        # стрелки, см. _has_selection_marker). Заполняется только при
        # require_marker=True; читает лог бота.
        self.last_marked_count = 0
        # Кто под прицелом (2026-10-03): все бары последнего кадра БОЯ (прошли проверку
        # таблички) и те из них, что с меткой выбранной цели, плюс точка, где центр бара
        # стоит, когда тело моба на прицеле. Заполняет _find_hp_bar(require_marker=True),
        # читает crosshair_target(). Отдельно от захвата бара — ничего не меняет в ведении.
        self.last_bar_candidates: "list[tuple[int, int, int, int]]" = []
        self.last_marked_bars: "list[tuple[int, int, int, int]]" = []
        self._last_ref: "tuple[float, float] | None" = None

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

        # Результат ПОСЛЕДНЕГО сырого скана панели (panel_detector.read_panel):
        # reason объясняет, почему панель не признана ("нет бежевой
        # окантовки", "нет малиновой плашки кнопки X"...). Читают отладочное
        # окно песочницы и лог бота — без этого "цели нет" было бы чёрным
        # ящиком, а калибровку пришлось бы делать вслепую.
        self.last_panel_result: "PanelResult | None" = None
        self._plate_warning_logged = False
        # Ссылки на ПОСЛЕДНИЕ сырые кадры (панель и мир) — для "чёрного ящика"
        # (src/core/blackbox.py): при инциденте он сохраняет, что бот видел в этот
        # момент. Ссылка, а не копия: каждый захват — новый массив, ничего не
        # перезаписывается, лишних затрат на кадр нет.
        self.last_panel_frame: "np.ndarray | None" = None
        self.last_world_frame: "np.ndarray | None" = None

        # Список целей "Астрального зрения" (2026-10-04, src/core/vision/target_list.py):
        # своя маленькая зона слева; последний разбор хранится для лога и FSM.
        self.list_roi = self.build_roi(
            vision_config.TARGET_LIST_OFFSET_X, vision_config.TARGET_LIST_OFFSET_Y,
            vision_config.TARGET_LIST_WIDTH, vision_config.TARGET_LIST_HEIGHT,
        )
        # Миникарта — для одометра ROAM (src/core/vision/minimap.py). getattr: старый
        # vision_config без MINIMAP_* не должен ронять зрение.
        self.minimap_roi = self.build_roi(
            getattr(vision_config, "MINIMAP_OFFSET_X", 652), getattr(vision_config, "MINIMAP_OFFSET_Y", -492),
            getattr(vision_config, "MINIMAP_WIDTH", 228), getattr(vision_config, "MINIMAP_HEIGHT", 162),
        )
        self.last_target_list: TargetList = TargetList(False, reason="ещё не читали")

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
        self._lock_lost_since = None
        self._panel_miss_streak = 0
        self._last_panel_info = None

    def release_bar_lock(self) -> None:
        """
        Снимает ТОЛЬКО захват мирового бара (в отличие от reset_tracking()
        не трогает антидребезг панели). Зови, когда нужно выбрать бар
        заново "с нуля" — например, в момент входа в COMBAT, когда цель
        выбрана Tab-ом и старый захват (на любом баре, пойманном в SEARCH)
        к ней отношения не имеет.
        """
        self._last_bar_center = None
        self._lock_lost_since = None

    @property
    def bar_locked(self) -> bool:
        """Есть ли сейчас захват мирового бара (см. _LOCK_*)."""
        return self._last_bar_center is not None

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

        # Список целей тоже лежит внутри мировой зоны: его жёлтые имена и полоски
        # не должны попадать в кандидаты мировых баров. getattr — моки без поля.
        lr = getattr(self, "list_roi", None)
        if lr is not None and getattr(self, "last_target_list", None) is not None and self.last_target_list.visible:
            lx0 = max(0, lr["left"] - roi_coords["left"])
            ly0 = max(0, lr["top"] - roi_coords["top"])
            lx1 = min(frame.shape[1], lr["left"] + lr["width"] - roi_coords["left"])
            ly1 = min(frame.shape[0], lr["top"] + lr["height"] - roi_coords["top"])
            if lx1 > lx0 and ly1 > ly0:
                frame[ly0:ly1, lx0:lx1] = 0

    # --- Список целей (Астральное зрение) ---

    def get_target_list(self) -> TargetList:
        """Захват зоны списка (~420x194, доли мс) + разбор строк (1-3 мс)."""
        tl = read_target_list(self._capture(self.list_roi))
        self.last_target_list = tl
        return tl

    def get_minimap(self) -> "np.ndarray | None":
        """Кадр миникарты (BGR, ~228x162) для одометра. Захват ~1 мс; бот зовёт ~5 раз/с."""
        return self._capture(self.minimap_roi)

    def find_numbered_bar(self, number: int) -> TargetInfo:
        """
        Моб с номером `number` над табличкой (тот же номер, что у строки в списке
        целей) — в ПОСЛЕДНЕМ кадре мира (его снимает get_target_info в этом же тике;
        нового захвата экрана нет). Offset — куда довернуть, чтобы тело моба встало в
        прицел (та же точка ref, что в get_target_info). Цифры без образца -> не найдено.
        Как проверить без игры: на скриншоте с номерами — get_target_info, потом этот метод.
        """
        empty = TargetInfo(found=False, hp_percent=0.0, offset_x=0.0, offset_y=0.0)
        frame, ref = self.last_world_frame, self._last_ref
        if frame is None or ref is None or not world_numbers.supported(number):
            return empty
        for c in self.last_bar_candidates:
            n, _score = world_numbers.read_number(frame, c)
            if n == number:
                # Центр ПОЛНОЙ полоски (левый край заливки + половина), а не заливки:
                # у раненого моба заливка короче, и её центр уехал бы влево.
                cx = c[0] + world_numbers.NUM_DX_FROM_LEFT
                cy = c[1] + c[3] / 2.0
                return TargetInfo(found=True, hp_percent=0.0, offset_x=cx - ref[0], offset_y=cy - ref[1])
        return empty

    # --- Слежение за целью из списка: "найти по номеру, вести по месту" (bot._list_track) ---

    def world_bars(self) -> "list[tuple[int, int, int, int]]":
        """Настоящие полоски HP последнего кадра мира (без букв имён: узкие отброшены)."""
        return [c for c in self.last_bar_candidates if c[2] >= world_numbers.MIN_BAR_W]

    @staticmethod
    def bar_anchor(bar: "tuple[int, int, int, int]") -> "tuple[float, float]":
        """Точка полоски, по которой следим: центр ПОЛНОЙ полоски (у раненого моба заливка короче)."""
        return bar[0] + world_numbers.NUM_DX_FROM_LEFT, bar[1] + bar[3] / 2.0

    def bar_number(self, bar: "tuple[int, int, int, int]") -> "int | None":
        """Номер над этой полоской в последнем кадре мира (None — не прочитали)."""
        if self.last_world_frame is None:
            return None
        return world_numbers.read_number(self.last_world_frame, bar)[0]

    def bar_info(self, bar: "tuple[int, int, int, int]") -> TargetInfo:
        """Куда довернуть камеру, чтобы моб этой полоски встал в прицел."""
        if self._last_ref is None:
            return TargetInfo(found=False, hp_percent=0.0, offset_x=0.0, offset_y=0.0)
        ax, ay = self.bar_anchor(bar)
        return TargetInfo(found=True, hp_percent=0.0, offset_x=ax - self._last_ref[0], offset_y=ay - self._last_ref[1])

    def number_window_of_marked(self) -> "np.ndarray | None":
        """Окно номера над баром ВЫБРАННОЙ цели (со стрелками) — сырьё для новых образцов цифр."""
        if self.last_world_frame is None or not self.last_marked_bars:
            return None
        bar = self.last_marked_bars[0]
        if bar[2] < world_numbers.MIN_BAR_W:
            return None
        return world_numbers.number_window(self.last_world_frame, bar)

    def list_point_to_screen(self, pt: "tuple[float, float]") -> "tuple[float, float]":
        """Точка в координатах зоны списка -> пиксели экрана (для клика)."""
        return self.list_roi["left"] + pt[0], self.list_roi["top"] + pt[1]

    # --- Точка прицела (см. src/config/aim_config.py) ---

    def aim_offset_px(self) -> "tuple[float, float]":
        """Прицел игры относительно ЦЕНТРА ЭКРАНА в пикселях (доли из aim_config * размер экрана)."""
        return (
            aim_config.AIM_POINT_X_FRAC * self.screen_w + self.aim_nudge_px[0],
            aim_config.AIM_POINT_Y_FRAC * self.screen_h + self.aim_nudge_px[1],
        )

    def body_offset_px(self) -> float:
        """На сколько px тело моба ниже центра его бара (доля высоты экрана)."""
        return aim_config.AIM_BODY_OFFSET_Y_FRAC * self.screen_h

    def aim_point_in_roi(self, roi_coords: dict) -> "tuple[float, float]":
        """
        Точка прицела в координатах КАДРА этой зоны (тех же, в каких считаются
        координаты баров). Считается из center_x/center_y и положения зоны,
        поэтому остаётся верной при любом ROI и любом разрешении.
        """
        dx, dy = self.aim_offset_px()
        return (
            self.center_x + dx - roi_coords["left"],
            self.center_y + dy - roi_coords["top"],
        )

    def build_roi(self, offset_x: int, offset_y: int,
                  width: int, height: int) -> dict:
        return {
            "left":   self.center_x + offset_x,
            "top":    self.center_y + offset_y,
            "width":  width,
            "height": height,
        }

    def get_target_info(self, roi_coords: dict, require_marker: bool = False,
                        avoid_marked: bool = False) -> TargetInfo:
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

        require_marker=True (бой): учитываются ТОЛЬКО бары с меткой выбранной
        цели (красные стрелки по бокам имени, см. _has_selection_marker). Без
        метки бар "чужой" — камера на него не идёт. Баг по логу (2026-10-02):
        при нескольких мобах в кадре зрение теряло бар цели, брало ближайший
        чужой, камера уезжала на 446 px, персонаж отворачивался и перестал
        попадать по раненому мобу.
        """
        empty = TargetInfo(found=False, hp_percent=0.0, offset_x=0.0, offset_y=0.0)

        frame = self._capture(roi_coords)
        if frame is None:
            return empty
        self.last_world_frame = frame

        # См. _mask_out_panel(): панель цели физически лежит ВНУТРИ этой
        # огромной зоны — без маски её собственный HP-бар иногда ловился
        # здесь как будто это летающая табличка над мобом (баг: камера
        # бесконечно доворачивала к неподвижной панели, см. комментарий
        # в _load_config). Вызываем ДО _find_hp_bar(), чтобы маскированные
        # пиксели вообще не участвовали в HSV-поиске кандидатов.
        self._mask_out_panel(frame, roi_coords)

        # Точка прицела в кадре и тело моба ниже бара. При первом контакте
        # выбираем бар, чьё ТЕЛО ближе всего к прицелу (ref — в координатах
        # центра бара, поэтому вычитаем поправку на тело).
        aim_x, aim_y = self.aim_point_in_roi(roi_coords)
        body_off = self.body_offset_px()

        bar = self._find_hp_bar(frame, ref=(aim_x, aim_y - body_off), require_marker=require_marker,
                                avoid_marked=avoid_marked)
        if bar is None:
            return empty

        bar_x, bar_y, bar_w, bar_h = bar

        hp = min((bar_w / self._max_hp_width) * 100.0, 100.0)
        hp = round(hp, 2)

        # Центр найденного бара В КАДРЕ (координаты внутри самого ROI,
        # а не всего экрана — frame это уже вырезанный кусок по roi_coords).
        bar_center_x = bar_x + bar_w / 2
        bar_center_y = bar_y + bar_h / 2

        # Куда надо довернуть камеру: ТЕЛО моба (центр бара + поправка вниз)
        # относительно ПРИЦЕЛА игры. Нули = прицел точно в мобе. Раньше тут
        # считалось от центра зоны поиска — он на ~63 px левее прицела, и
        # камера уверенно наводила моба мимо белой точки.
        offset_x = bar_center_x - aim_x
        offset_y = (bar_center_y + body_off) - aim_y

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

        ИСПРАВЛЕНО (2026-10-02, баг: бот "видел цель" на пустой траве и
        начинал комбо в пустоту). Раньше панелью считался ЛЮБОЙ
        жёлтый/красный прямоугольник нужной формы — на траве таких
        сотни (на 6700 кусках травы со скриншотов игрока старая проверка
        срабатывала в 24% случаев). Теперь found=True только если в кадре
        есть сама ПАНЕЛЬ: рамка полоски из двух чёрных линий, бежевая
        окантовка и красная кнопка X (см. panel_detector.read_panel и
        src/config/panel_template.py). HP считается по заливке внутри этой
        рамки, а не по ширине случайного пятна.
        """
        empty = TargetInfo(found=False, hp_percent=0.0, offset_x=0.0, offset_y=0.0)

        frame = self._capture(roi_coords)
        if frame is None:
            self.last_panel_result = None
            return empty
        self.last_panel_frame = frame

        result = read_panel(frame)
        self.last_panel_result = result
        if not result.ok:
            return empty

        if not result.plate_checked and not self._plate_warning_logged:
            # ROI панели обрезает кнопку X: проверка слабее (рамка и
            # окантовка всё равно проверяются). Говорим один раз, не спамим.
            self._plate_warning_logged = True
            logger.warning(
                "ROI панели обрезает кнопку X — проверка плашки ослаблена. "
                "Увеличь vision_config.TARGET_PANEL_WIDTH на 10-15 px."
            )

        bar_x, bar_y, bar_w, bar_h = result.bar
        hp = result.hp_percent

        bar_center_x = bar_x + bar_w / 2
        bar_center_y = bar_y + bar_h / 2
        roi_center_x = roi_coords["width"] / 2
        roi_center_y = roi_coords["height"] / 2

        return TargetInfo(
            found=True,
            hp_percent=hp,
            offset_x=bar_center_x - roi_center_x,
            offset_y=bar_center_y - roi_center_y,
            hp_text=result.text,
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

        num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(
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

            # Фильтр 2: Плотность (Extent).
            # Ослаблено (2026-10-03): полоска ХП выбранной цели (с мечом/"галочкой" по центру)
            # имеет низкую плотность и высоту, из-за чего старый фильтр < 0.5 её убивал,
            # и бот переключался на соседних мобов.
            extent = area / (w * h)
            if extent < 0.1:
                continue

            # Фильтр 3: соотношение сторон (ослаблено для полоски с галочкой)
            if (w / h) < 1.5:
                continue

            # Фильтр 4: ЯРКОСТЬ и НАСЫЩЕННОСТЬ заливки (2026-10-02, баг:
            # жёлто-зелёная трава проходила как "бар"). Заливка настоящего
            # бара — светящийся элемент интерфейса, а трава — тусклая; замер
            # по скриншотам игрока, средние по пикселям компоненты:
            #     настоящий бар: S ~198, V ~197     трава: S 98-121, V 100-143
            # Берём СРЕДНЕЕ по компоненте, а не по каждому пикселю: края
            # бара смешаны с рамкой и темнее, среднее их переживает. Границы
            # (panel_template.WORLD_BAR_MIN_MEAN_*) — с запасом в обе стороны.
            sub_mask = labels[y:y + h, x:x + w] == i
            sub_hsv = hsv[y:y + h, x:x + w]
            mean_s = float(sub_hsv[..., 1][sub_mask].mean())
            mean_v = float(sub_hsv[..., 2][sub_mask].mean())
            if (mean_s < panel_template.WORLD_BAR_MIN_MEAN_S
                    or mean_v < panel_template.WORLD_BAR_MIN_MEAN_V):
                continue

            candidates.append((x, y, w, h))

        return candidates

    def _big_enough_for_first_contact(self, bar: "tuple[int,int,int,int]") -> bool:
        """Размерный фильтр первого контакта; вынесен, чтобы песочница рисовала то же правило."""
        return (bar[2] >= self._WORLD_FIRST_CONTACT_MIN_W
                and self._WORLD_FIRST_CONTACT_MIN_H <= bar[3] <= self._WORLD_FIRST_CONTACT_MAX_H)

    def _stuck_to_marked(self, c: "tuple[int,int,int,int]",
                         marked: "list[tuple[int,int,int,int]]") -> bool:
        """
        True, если бар c — сама выбранная цель или стоит вплотную к ней (см.
        _STUCK_TO_MARKED_*). Сравнение по центрам: работает и для бара выбранной,
        слипшегося с иконкой меча (высокое пятно, центр по Y почти тот же).
        any() с генератором — ленивая проверка: остановится на первом совпадении.
        """
        cx, cy = c[0] + c[2] / 2.0, c[1] + c[3] / 2.0
        return any(
            abs(cx - (m[0] + m[2] / 2.0)) < (c[2] + m[2]) / 2.0 + self._STUCK_TO_MARKED_GAP_PX
            and abs(cy - (m[1] + m[3] / 2.0)) < self._STUCK_TO_MARKED_DY_PX
            for m in marked
        )

    def _has_selection_marker(self, frame: "np.ndarray", bar: "tuple[int,int,int,int]") -> bool:
        """
        True, если над баром — табличка ВЫБРАННОЙ цели: две красные стрелки
        по бокам имени (см. panel_template.MARK_*). Берём узкую полосу над
        баром, ищем в ней красные пятна нужного размера и ищем среди них
        ПАРУ: на одной высоте, по разные стороны, и так, чтобы середина
        между ними приходилась над серединой полной полоски. Привязка идёт
        к ЛЕВОМУ краю заливки (она растёт слева), поэтому не зависит от того,
        сколько HP осталось у моба.

        Работает на кропе полосы (а не на всём кадре): стоимость — доли
        миллисекунды на кандидата.

        Как тестировать без игры: см. vision_marker_test.py — реальный кадр
        с выбранной целью (стрелки есть) и кадры без неё (стрелок нет).
        """
        x, y, w, h = bar
        H, W = frame.shape[:2]
        y0 = max(0, y - panel_template.MARK_MAX_ABOVE_PX)
        y1 = max(0, y - panel_template.MARK_MIN_ABOVE_PX)
        cx = x + w / 2.0
        x0 = max(0, int(cx - panel_template.MARK_MAX_DX_PX))
        x1 = min(W, int(cx + panel_template.MARK_MAX_DX_PX))
        if y1 - y0 < panel_template.MARK_MIN_SIDE_PX or x1 - x0 < 2 * panel_template.MARK_MIN_SIDE_PX:
            return False

        hsv = cv2.cvtColor(np.ascontiguousarray(frame[y0:y1, x0:x1]), cv2.COLOR_BGR2HSV)
        mask = (
            cv2.inRange(hsv, np.array(panel_template.MARK_RED_LOWER_1, np.uint8),
                        np.array(panel_template.MARK_RED_UPPER_1, np.uint8))
            | cv2.inRange(hsv, np.array(panel_template.MARK_RED_LOWER_2, np.uint8),
                          np.array(panel_template.MARK_RED_UPPER_2, np.uint8))
        )
        if not mask.any():
            return False

        n, _, stats, cent = cv2.connectedComponentsWithStats(mask, connectivity=8)
        blobs: "list[tuple[float, float]]" = []     # центры подходящих по размеру пятен
        for i in range(1, n):
            bx, by, bw, bh, area = stats[i]
            if not (panel_template.MARK_MIN_AREA <= area <= panel_template.MARK_MAX_AREA):
                continue
            if not (panel_template.MARK_MIN_SIDE_PX <= bw <= panel_template.MARK_MAX_SIDE_PX
                    and panel_template.MARK_MIN_SIDE_PX <= bh <= panel_template.MARK_MAX_SIDE_PX):
                continue
            blobs.append((cent[i][0] + x0, cent[i][1] + y0))
        if len(blobs) < 2:
            return False

        gap = panel_template.MARK_MIN_ARROW_GAP_PX
        left = [b for b in blobs if b[0] < cx - gap]
        right = [b for b in blobs if b[0] > cx + gap]
        for lx, ly in left:
            for rx, ry in right:
                if abs(ly - ry) > panel_template.MARK_MAX_PAIR_DY_PX:
                    continue
                mid_x = (lx + rx) / 2.0
                # левый край заливки должен лежать на HALF левее середины пары
                if abs((mid_x - x) - panel_template.MARK_HALF_BAR_PX) <= panel_template.MARK_BAR_X_TOL_PX:
                    return True
        return False

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

    def _find_hp_bar(
        self,
        frame: "np.ndarray",
        ref: "tuple[float, float] | None" = None,
        require_marker: bool = False,
        avoid_marked: bool = False,
    ) -> "tuple[int,int,int,int] | None":
        """
        Ищет HP-бар цели: берёт кандидатов из _collect_bar_candidates(),
        оставляет только тех, над кем есть табличка с именем
        (_has_nameplate_above), и выбирает ОДНОГО по правилам ниже.

        1) Захват есть (мы уже следим за каким-то баром):
           смотрим ТОЛЬКО на кандидатов рядом с прошлой позицией. Радиус =
           _MAX_BAR_JUMP_PX + рост за каждую секунду потери, но не больше
           _LOCK_RADIUS_MAX_PX. Нашли — продолжаем следить. Не нашли —
           возвращаем None (камера стоит), а если так длится дольше
           _LOCK_RELEASE_S — захват снимается. Чужие бары в этом режиме
           не рассматриваются, даже если они единственные в кадре.
        2) Захвата нет ("первый контакт"): берём бар, тело которого БЛИЖАЙШЕ
           К ПРИЦЕЛУ игры (параметр ref; без него — к центру кадра), при
           равенстве — самый широкий. Раньше брали
           самый широкий во всей зоне — это почти случайный выбор между
           мобами. Ширину бара как признак HP/дальности НЕ используем: у
           мировых баров она не калибрована (MAX_TARGET_HP_WIDTH).

        Как тестировать без игры: подставь в _collect_bar_candidates и
        _has_nameplate_above заглушки со списком прямоугольников и гоняй
        кадры по очереди (так и проверена эта логика, см. историю чата).
        """
        now = time.monotonic()
        candidates = self._collect_bar_candidates(frame)
        candidates = [c for c in candidates if self._has_nameplate_above(frame, c)]
        self._last_ref = ref
        # Метка выбранной цели проверяется у ВСЕХ кандидатов (в любом режиме): по
        # ней решается "кто под прицелом" (crosshair_target) и кого вести.
        marked = [c for c in candidates if self._has_selection_marker(frame, c)]
        self.last_bar_candidates = list(candidates)
        self.last_marked_bars = list(marked)

        def _center(c: "tuple[int, int, int, int]") -> "tuple[float, float]":
            return c[0] + c[2] / 2, c[1] + c[3] / 2

        if avoid_marked:
            # Бот сам выбрал ДРУГОГО моба и бежит к нему, а прежняя цель ещё
            # выбрана в игре: её бар (с меткой) не берём вовсе.
            # Вплотную к ней — тоже нет: прицел их не различит (см. _stuck_to_marked).
            candidates = [c for c in candidates if not self._stuck_to_marked(c, marked)]
        elif marked:
            # Выбранная в игре цель (метка-стрелки) — истина: ведём её, даже если
            # липкий захват держал другой бар. Баг со скриншота 2026-10-03: прицел
            # на новом мобе (SEL), а линия камеры тянулась к старому — захват
            # ждал _LOCK_RELEASE_S, прежде чем отпустить старый бар.
            ref_pt = self._last_bar_center or (ref if ref is not None else (frame.shape[1] / 2, frame.shape[0] / 2))
            best = min(marked, key=lambda c: (_center(c)[0] - ref_pt[0]) ** 2 + (_center(c)[1] - ref_pt[1]) ** 2)
            self._last_bar_center = _center(best)
            self._lock_lost_since = None
            self.last_marked_count = len(marked)
            self.last_candidate_count = len(marked) if require_marker else len(candidates)
            return best
        self.last_marked_count = len(marked)
        if require_marker:
            # Бой: только бар выбранной цели. Меток в кадре нет -> вести некого.
            self.last_candidate_count = 0
            return None
        if self._last_bar_center is None:
            # Первый контакт: мелочь вроде цветов и сухой земли отсекаем по
            # размеру (см. _WORLD_FIRST_CONTACT_MIN_*). Ведение уже
            # захваченного бара этим не ограничиваем.
            candidates = [c for c in candidates if self._big_enough_for_first_contact(c)]
        self.last_candidate_count = len(candidates)

        if self._last_bar_center is not None:
            last_x, last_y = self._last_bar_center
            lost_s = 0.0 if self._lock_lost_since is None else now - self._lock_lost_since
            radius = min(
                self._MAX_BAR_JUMP_PX + self._LOCK_RADIUS_GROWTH_PX_S * lost_s,
                self._LOCK_RADIUS_MAX_PX,
            )

            def _dist_to_last(c: "tuple[int, int, int, int]") -> float:
                cx, cy = _center(c)
                return ((cx - last_x) ** 2 + (cy - last_y) ** 2) ** 0.5

            near = [c for c in candidates if _dist_to_last(c) <= radius]
            if near:
                best = min(near, key=_dist_to_last)
                self._last_bar_center = _center(best)
                self._lock_lost_since = None
                return best

            # Бара рядом нет. Не прыгаем на чужие — стоим и ждём.
            if self._lock_lost_since is None:
                self._lock_lost_since = now
            elif now - self._lock_lost_since > self._LOCK_RELEASE_S:
                self._last_bar_center = None
                self._lock_lost_since = None
            return None

        if not candidates:
            return None

        # Первый контакт: БЛИЖАЙШИЙ МОБ (2026-10-03, выбор цели зрением вместо Tab).
        # Полоски над мобами на экране одного размера на любой дистанции (84-87 px
        # во всех кадрах), зато чем моб ближе, тем НИЖЕ его полоска: на кадре
        # 12:55:48 соседний Шипобраз y=467, клювозавр в 499 м y=308. См. nearness_key.
        roi_cx, roi_cy = ref if ref is not None else (frame.shape[1] / 2, frame.shape[0] / 2)

        def _first_contact_key(c: "tuple[int, int, int, int]") -> "tuple[float, int]":
            return (self.nearness_key(c, (roi_cx, roi_cy)), -c[2])

        best = min(candidates, key=_first_contact_key)
        self._last_bar_center = _center(best)
        self._lock_lost_since = None
        return best

    # Насколько горизонтальное смещение весит против высоты: моб чуть в стороне
    # лучше, чем моб выше (дальше), но поворот камеры тоже стоит времени.
    _NEAR_X_WEIGHT = 0.35

    def nearness_key(self, c: "tuple[int, int, int, int]", ref: "tuple[float, float]") -> float:
        """
        Вариант 4: Выбор по чистой высоте на экране (Depth-first).
        Чем ниже моб на экране (больше Y), тем он физически ближе в 3D мире.
        Горизонтальное расстояние дает лишь небольшой штраф, чтобы не хватать мобов совсем с краю, если есть кто-то перед носом.
        """
        cx, cy = c[0] + c[2] / 2, c[1] + c[3] / 2
        player_x = ref[0]
        # Возвращаем отрицательный Y (чем ниже, тем меньше число) + небольшой штраф за дальность от центра.
        return -cy + abs(cx - player_x) * 0.3
    def pick_switch_bar(self) -> "tuple[int, int, int, int] | None":
        """
        Ближайший моб из последнего кадра, КРОМЕ выбранной цели (бар с меткой) —
        для правила "5 с без урона -> к другому мобу". None — других нет.
        Чистая функция от сохранённых списков, без нового захвата экрана.
        """
        ref = self._last_ref
        if ref is None:
            return None
        # Не выбранная, не вплотную к ней (клик взял бы её же — кадр 16:37:53) и
        # похожая на целую полоску (см. _WORLD_FIRST_CONTACT_*).
        others = [c for c in self.last_bar_candidates
                  if self._big_enough_for_first_contact(c)
                  and not self._stuck_to_marked(c, self.last_marked_bars)]
        if not others:
            return None
        return min(others, key=lambda c: self.nearness_key(c, ref))

    def lock_bar(self, c: "tuple[int, int, int, int]") -> None:
        """Взять липкий захват на конкретный бар (его дальше ведёт get_target_info)."""
        self._last_bar_center = (c[0] + c[2] / 2, c[1] + c[3] / 2)
        self._lock_lost_since = None

    def crosshair_target(self, radius_px: float) -> str:
        """
        Кто ПОД ПРИЦЕЛОМ по последнему кадру боя: "marked" — выбранная цель (бар с
        меткой), "other" — ДРУГОЙ моб (бар без метки), "none" — никого (или кадр
        снимался без проверки меток, тогда судить не о чем).

        Зачем: в игре Ctrl+ЛКМ и ПКМ выбирают того, кто под прицелом. Лог 2026-10-03
        12:55:49 — Tab взял ближнего моба, а Ctrl+ЛКМ через 0.08 с вернул дальнего
        (499 м), потому что прицел смотрел на него. Кликать можно, только если под
        прицелом выбранная цель или пусто (тогда клик действует на текущую цель).

        "Под прицелом" = ближайший бар, чей центр не дальше radius_px от точки, где он
        стоит, когда тело моба на прицеле (та же точка ref, что в _find_hp_bar).
        Чистая функция от сохранённых списков — без нового захвата экрана.
        Как тестировать без игры: заполни last_bar_candidates/last_marked_bars/_last_ref
        руками (vision_marker_test.py, блок CROSS).
        """
        ref = self._last_ref
        if ref is None or not self.last_bar_candidates:
            return "none"
        rx, ry = ref
        best, best_d = None, float(radius_px)
        for c in self.last_bar_candidates:
            d = ((c[0] + c[2] / 2 - rx) ** 2 + (c[1] + c[3] / 2 - ry) ** 2) ** 0.5
            if d <= best_d:
                best, best_d = c, d
        if best is None:
            return "none"
        return "marked" if best in self.last_marked_bars else "other"

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

# Панель цели — крошечная зона (десятки пикселей), ей нужен зум побольше,
# чем мировому ROI. Тот же принцип "верхний предел, а не множитель":
# _fit_debug_window всё равно урежет до реального размера монитора.
_PANEL_DEBUG_MAX_ZOOM = 6.0

# Оставляем небольшой отступ от края монитора (панель задач, рамка окна
# от Windows) — 100% ширины/высоты монитора визуально даёт окно, которое
# либо не помещается, либо упирается в панель задач.
_DEBUG_SCREEN_MARGIN = 0.9


def _pick_debug_monitor(monitors: "list[dict]") -> dict:
    """
    Выбирает монитор для окон отладки: второй, если он есть, иначе первый.

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
        return monitors[2]
    print("[vision_manager] Второй монитор не найден (mss видит только "
          f"{len(monitors) - 1} физических монитор(а)) — показываю окна "
          "отладки на первом.")
    return monitors[1]


def _fit_zoom(monitor: dict, roi_width: int, roi_height: int, max_zoom: float) -> float:
    """
    Масштаб окна отладки: не больше max_zoom (мировому ROI хватает x4,
    крошечной панели цели нужен x6 — см. _PANEL_DEBUG_MAX_ZOOM) и никогда
    не больше того, что реально влезает в монитор с отступом.
    """
    # Масштаб ограничен СВЕРХУ (не нужно, чтобы маленький ROI открывался
    # микроскопическим окном 1:1), но никогда не больше того, что реально
    # влезает в выбранный монитор с отступом.
    scale = min(
        max_zoom,
        (monitor["width"] * _DEBUG_SCREEN_MARGIN) / roi_width,
        (monitor["height"] * _DEBUG_SCREEN_MARGIN) / roi_height,
    )
    # Защита от вырожденного случая (экзотическое разрешение/ROI больше
    # монитора в разы) — не даём масштабу уйти к нулю или в минус.
    return max(scale, 0.1)


def _fit_debug_window(
    monitors: "list[dict]",
    roi_width: int,
    roi_height: int,
    max_zoom: float = _DEBUG_MAX_ZOOM,
) -> "tuple[dict, float]":
    """Монитор + масштаб одним вызовом (обёртка над _pick_debug_monitor и _fit_zoom)."""
    target = _pick_debug_monitor(monitors)
    return target, _fit_zoom(target, roi_width, roi_height, max_zoom)


def _render_world_debug(
    vision: "VisionManager",
    frame: np.ndarray,
    debug_w: int,
    debug_h: int,
    debug_scale: float,
    roi_coords: dict,
    require_marker: bool = False,
) -> np.ndarray:
    """
    Рисует отладочный кадр мирового HP-бара (старое "большое" окно).

    require_marker=True — режим боя (клавиша 'm' в песочнице): финальным
    выбором считается только бар с меткой выбранной цели. Бары с меткой в
    любом режиме обведены ОРАНЖЕВЫМ и подписаны SEL: если у живой выбранной
    цели оранжевого бара нет — метка не распознаётся (элитные мобы, боссы),
    и сними кадр клавишей 's'.

    Вынесено из __main__ в отдельную функцию: мировое окно можно
    отключить (флаг --panel-only), и вызывать его целиком только по
    условию проще и безопаснее, чем держать 60 строк внутри if в цикле.
    """
    debug_frame = cv2.resize(frame, (debug_w, debug_h), interpolation=cv2.INTER_NEAREST)

    # ТОЧКА ПРИЦЕЛА бота (aim_config.py) — от неё считается offset. Рисуем
    # розовым кольцом с точкой в центре: белая точка прицела ИГРЫ должна
    # лежать ровно в центре кольца. Если нет — сдвинь кольцо клавишами
    # j/l/i/k (см. __main__) и перенеси напечатанные значения в aim_config.py.
    aim_x, aim_y = vision.aim_point_in_roi(roi_coords)
    roi_cx = int(aim_x * debug_scale)
    roi_cy = int(aim_y * debug_scale)
    cv2.circle(debug_frame, (roi_cx, roi_cy), 12, (255, 0, 255), 2)
    cv2.circle(debug_frame, (roi_cx, roi_cy), 2, (255, 0, 255), -1)
    cv2.putText(debug_frame, "AIM", (roi_cx + 16, roi_cy - 10),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 0, 255), 1, cv2.LINE_AA)

    # ДИАГНОСТИКА (2026-09-28, дополнено проверкой таблички с
    # именем): три уровня кандидатов, три цвета рамок.
    #   КРАСНЫЙ тонкий  — прошёл фильтр формы, но НЕТ таблички
    #                     с именем над ним (значит это НЕ бар —
    #                     декорация мира, цветок, метка и т.п.)
    #   ЗЕЛЁНЫЙ толстый — финальный выбор (см. ниже)
    #   СЕРЫЙ тонкий    — прошёл форму и табличку, но СЛИШКОМ МЕЛКИЙ для
    #                     первого контакта (_WORLD_FIRST_CONTACT_MIN_*:
    #                     цветы, сухая земля). Если настоящий моб рисуется
    #                     серым — пороги велики, поправь их в классе.
    #   ОРАНЖЕВЫЙ SEL   — над баром есть метка выбранной цели (красные
    #                     стрелки по бокам имени).
    shape_candidates = vision._collect_bar_candidates(frame)
    verified_candidates = [
        c for c in shape_candidates if vision._has_nameplate_above(frame, c)
    ]
    # Запоминаем ДО _find_hp_bar: он сам может взять/снять захват, а размерный
    # фильтр действует только пока захвата не было.
    was_locked = vision.bar_locked
    # финальный выбор — та же логика, что и в бою
    bar = vision._find_hp_bar(frame, require_marker=require_marker)
    marked_set = {c for c in verified_candidates if vision._has_selection_marker(frame, c)}

    for cx, cy, cw, ch in shape_candidates:
        if (cx, cy, cw, ch) in verified_candidates:
            continue  # эти рисуем ниже, голубым/серым
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
        too_small = (
            (not require_marker) and (not was_locked)
            and not vision._big_enough_for_first_contact((cx, cy, cw, ch))
        )
        cv2.rectangle(
            debug_frame,
            (int(cx * debug_scale), int(cy * debug_scale)),
            (int((cx + cw) * debug_scale), int((cy + ch) * debug_scale)),
            (128, 128, 128) if too_small else (255, 255, 0),
            1,
        )

    for cx, cy, cw, ch in marked_set:
        cv2.rectangle(
            debug_frame,
            (int(cx * debug_scale) - 3, int(cy * debug_scale) - 3),
            (int((cx + cw) * debug_scale) + 3, int((cy + ch) * debug_scale) + 3),
            (0, 165, 255),
            2,
        )
        cv2.putText(debug_frame, "SEL", (int(cx * debug_scale), int(cy * debug_scale) - 8),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 165, 255), 1, cv2.LINE_AA)

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
        # Линия идёт к ТЕЛУ моба (центр бара + поправка вниз) — именно эту
        # точку бот старается совместить с прицелом.
        bar_cx = int((x + w / 2) * debug_scale)
        bar_cy = int((y + h / 2 + vision.body_offset_px()) * debug_scale)
        cv2.line(debug_frame, (roi_cx, roi_cy), (bar_cx, bar_cy), (0, 255, 255), 2)
        cv2.circle(debug_frame, (bar_cx, bar_cy), 5, (0, 255, 255), -1)

    return debug_frame


if __name__ == "__main__":
    # ------------------------------------------------------------------
    # Песочница VisionManager: ДВА окна зрения (как у бота при
    # _USE_WORLD_VISION = True).
    #
    #   python vision_manager.py               -> оба окна:
    #       "VisionManager Debug"       — мировой HP-бар над мобом (поиск и
    #                                     ведение целей в кадре);
    #       "VisionManager Panel Debug" — маленькая панель выбранной цели
    #                                     (цель есть / жива / сколько HP).
    #   python vision_manager.py --panel-only  -> только окно панели (для
    #       отладки запасного режима бота _USE_WORLD_VISION = False; без
    #       мирового окна песочница ещё и заметно легче — не считается
    #       Canny/контуры по ROI 1407x411).
    #
    # Флаг через sys.argv, а не argparse: один булев переключатель для
    # песочницы — argparse здесь лишний код ради одного слова.
    # ------------------------------------------------------------------
    logging.basicConfig(level=logging.DEBUG)

    SHOW_WORLD = "--panel-only" not in sys.argv[1:]

    vision = VisionManager()

    PANEL_ROI = vision.build_roi(
        offset_x=vision_config.TARGET_PANEL_OFFSET_X,
        offset_y=vision_config.TARGET_PANEL_OFFSET_Y,
        width=vision_config.TARGET_PANEL_WIDTH,
        height=vision_config.TARGET_PANEL_HEIGHT,
    )

    # Мировое окно — переменные в None/0, если окно выключено, чтобы цикл
    # ниже решал одним `if world_roi is not None`, рисовать ли его.
    world_roi = None
    world_w = world_h = 0
    world_scale = 1.0
    if SHOW_WORLD:
        world_roi = vision.build_roi(
            offset_x=vision_config.TARGET_HP_OFFSET_X,
            offset_y=vision_config.TARGET_HP_OFFSET_Y,
            width=vision_config.TARGET_HP_WIDTH,
            height=vision_config.TARGET_HP_HEIGHT,
        )

    # ОДИН монитор для обоих окон (второй, если подключён) и свой масштаб
    # под каждую зону: мировому ROI хватает x4, крошечной панели нужен x6.
    target_monitor = _pick_debug_monitor(vision.sct.monitors)
    panel_scale = _fit_zoom(target_monitor, PANEL_ROI["width"], PANEL_ROI["height"],
                            _PANEL_DEBUG_MAX_ZOOM)
    if world_roi is not None:
        world_scale = _fit_zoom(target_monitor, world_roi["width"], world_roi["height"],
                                _DEBUG_MAX_ZOOM)
        world_w = int(world_roi["width"] * world_scale)
        world_h = int(world_roi["height"] * world_scale)
    panel_debug_w = int(PANEL_ROI["width"] * panel_scale)
    panel_debug_h = int(PANEL_ROI["height"] * panel_scale)

    # WINDOW_NORMAL (а не дефолтный WINDOW_AUTOSIZE от простого imshow) —
    # только с ним moveWindow/resizeWindow реально работают и окно потом
    # можно ещё и руками подвинуть/растянуть мышью, если нужно.
    # Позиционируем ОДИН раз до входа в цикл 60 FPS, а не на каждый кадр —
    # moveWindow/resizeWindow дёргают оконный менеджер Windows, это не
    # бесплатно, а окну не нужно "плыть" каждый тик.
    _PANEL_DEBUG_WINDOW_NAME = "VisionManager Panel Debug"
    panel_x = target_monitor["left"]
    if world_roi is not None:
        cv2.namedWindow(_DEBUG_WINDOW_NAME, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(_DEBUG_WINDOW_NAME, world_w, world_h)
        cv2.moveWindow(_DEBUG_WINDOW_NAME, target_monitor["left"], target_monitor["top"])
        # Панель — правее мирового окна на том же мониторе; если не влезает
        # (узкий монитор), ляжет поверх, просто подвинь руками.
        panel_x = target_monitor["left"] + world_w + 20
    cv2.namedWindow(_PANEL_DEBUG_WINDOW_NAME, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(_PANEL_DEBUG_WINDOW_NAME, panel_debug_w, panel_debug_h)
    cv2.moveWindow(_PANEL_DEBUG_WINDOW_NAME, panel_x, target_monitor["top"])

    print(f"Центр экрана: ({vision.center_x}, {vision.center_y})")
    if world_roi is not None:
        print(f"Тестовый ROI (мировой бар): {world_roi}")
        print(f"Мировое окно: монитор ({target_monitor['left']}, {target_monitor['top']}), "
              f"масштаб x{world_scale:.2f}, размер {world_w}x{world_h}px")
    else:
        print("Мировое окно выключено (--panel-only): работает только окно панели.")
    print(f"Тестовый ROI (панель цели): {PANEL_ROI}")
    print(f"Окно панели: масштаб x{panel_scale:.2f}, размер {panel_debug_w}x{panel_debug_h}px")
    print("Нажми 'q' для выхода, 's' — сохранить сырые кадры в debug_captures/, "
          "'m' — режим боя: брать только бар с меткой выбранной цели (стрелки).")
    ax0, ay0 = vision.aim_offset_px()
    print(f"Точка прицела (розовое кольцо): ({ax0:+.1f}, {ay0:+.1f}) px от центра экрана. "
          "Сдвиг: j/l — влево/вправо, i/k — вверх/вниз (1 px; с Shift — 5 px).")

    last_panel_reason = ""
    require_marker = False   # клавиша 'm': режим боя (только бар с меткой)
    world_frame = None  # объявляем заранее: клавише 's' он нужен и при --panel-only

    try:
        while True:
            # --- Окно панели (всегда): единственный источник правды для
            # found/hp_percent (см. get_panel_target_info) ---
            panel_info = vision.get_panel_target_info(PANEL_ROI)
            print(
                f"ПАНЕЛЬ: HP {panel_info.hp_percent:.1f}%"
                if panel_info.found else "ПАНЕЛЬ: цели нет"
            )

            panel_frame = vision._capture(PANEL_ROI)
            if panel_frame is not None:
                panel_debug_frame = cv2.resize(
                    panel_frame,
                    (panel_debug_w, panel_debug_h),
                    interpolation=cv2.INTER_NEAREST,
                )
                # Рамка и подпись берутся из ТОГО ЖЕ результата, на котором
                # принимает решение бот (vision.last_panel_result) — а не из
                # отдельного пересчёта, чтобы окно не могло показать одно, а
                # бот увидеть другое.
                pres = vision.last_panel_result
                if pres is not None and pres.ok:
                    px, py, pw, ph = pres.bar
                    cv2.rectangle(
                        panel_debug_frame,
                        (int(px * panel_scale), int(py * panel_scale)),
                        (int((px + pw) * panel_scale), int((py + ph) * panel_scale)),
                        (0, 255, 0),
                        2,
                    )
                    label, color = f"PANEL OK  HP {pres.hp_percent:.0f}%", (0, 255, 0)
                else:
                    label = "NO PANEL: " + (pres.reason if pres is not None else "нет кадра")
                    color = (0, 0, 255)
                # Подпись латиницей по умолчанию: шрифты cv2 не рисуют кириллицу
                # ("???"), поэтому причину отказа дублируем в консоль ниже.
                cv2.putText(panel_debug_frame, label.split(":")[0], (4, 16),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1, cv2.LINE_AA)
                if pres is not None and pres.reason != last_panel_reason:
                    last_panel_reason = pres.reason
                    print(f"ПАНЕЛЬ: проверка структуры -> {pres.reason}")
                cv2.imshow(_PANEL_DEBUG_WINDOW_NAME, panel_debug_frame)

            # --- Мировое окно (по умолчанию; не создаётся с --panel-only):
            # HP-бар над мобом. С --panel-only этот блок пропускается
            # целиком — ни get_target_info, ни захвата, ни поиска
            # кандидатов (самая тяжёлая часть кадра: Canny + контуры по
            # ROI 1407x411).
            if world_roi is not None:
                info = vision.get_target_info(world_roi, require_marker=require_marker)
                mode = "бой: только метка" if require_marker else "поиск"
                print(
                    f"МИР: HP {info.hp_percent:.1f}% | "
                    f"offset=({info.offset_x:+.0f}, {info.offset_y:+.0f})px | режим: {mode}"
                    if info.found else f"МИР: таргет не найден | режим: {mode}"
                )
                world_frame = vision._capture(world_roi)
                if world_frame is not None:
                    cv2.imshow(
                        _DEBUG_WINDOW_NAME,
                        _render_world_debug(vision, world_frame, world_w, world_h, world_scale,
                                            world_roi, require_marker),
                    )

            key = cv2.waitKey(1) & 0xFF
            if key == ord("q"):
                break
            # Подстройка точки прицела: i (вверх) / k (вниз) / j (влево) /
            # l (вправо) — 1 px, с Shift (I/K/J/L) — 5 px. Буквы, а не WASD:
            # 's' уже занята сохранением кадров. Двигаем только ручную добавку
            # aim_nudge_px, в aim_config.py ничего не пишем — файл правишь сам
            # по напечатанным значениям.
            nudge = {
                ord("j"): (-1, 0), ord("l"): (1, 0), ord("i"): (0, -1), ord("k"): (0, 1),
                ord("J"): (-5, 0), ord("L"): (5, 0), ord("I"): (0, -5), ord("K"): (0, 5),
            }.get(key)
            if nudge is not None:
                vision.aim_nudge_px[0] += nudge[0]
                vision.aim_nudge_px[1] += nudge[1]
                ax, ay = vision.aim_offset_px()
                print(
                    "Прицел от центра экрана: (%+.1f, %+.1f) px.  Впиши в aim_config.py:\n"
                    "    AIM_POINT_X_FRAC = %.4f\n    AIM_POINT_Y_FRAC = %.4f"
                    % (ax, ay, ax / vision.screen_w, ay / vision.screen_h)
                )
                continue
            if key == ord("m"):
                require_marker = not require_marker
                vision.reset_tracking()   # режимы выбирают бар по-разному — захват начинаем заново
                print("Режим мирового окна: %s" % (
                    "БОЙ — только бар с меткой выбранной цели (оранжевый SEL)" if require_marker
                    else "ПОИСК — любой бар с табличкой"))
                continue
            if key == ord("r"):
                vision.reset_tracking()
                print("Сброс захвата цели (имитация перехода бота в SEARCH)")
                continue
            if key == ord("s"):
                # Сохраняем СЫРЫЕ кадры (без рамок и подписей, PNG без потерь).
                # Скриншот окна отладки сжат и разрисован — по нему нельзя
                # честно подобрать пороги; сырой кадр — можно (и на нём же
                # гоняются тесты без игры). Жми 's' в момент, когда детектор
                # ошибается: цель есть, а окно пишет NO PANEL — или наоборот.
                stamp = time.strftime("%Y%m%d_%H%M%S")
                os.makedirs("debug_captures", exist_ok=True)
                if panel_frame is not None:
                    cv2.imwrite(f"debug_captures/panel_{stamp}.png", panel_frame)
                if world_roi is not None and world_frame is not None:
                    cv2.imwrite(f"debug_captures/world_{stamp}.png", world_frame)
                print(f"Сохранено в debug_captures/ (метка {stamp})")

            time.sleep(1 / 60)

    finally:
        cv2.destroyAllWindows()
        vision.close()
        print("Песочница закрыта.")