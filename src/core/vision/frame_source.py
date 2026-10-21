"""
Источник кадров: ОДИН поток снимает весь монитор, все остальные берут вырезки из последнего кадра.

Зачем (лог 2026-10-06 04:41, строка PERF): главный поток бота тратил 19-20 мс из ~25 мс тика на
захват экрана — mss (GDI BitBlt) на каждую зону (панель, список, мир) делает отдельный снимок
по 5-7 мс. Бот успевал 37 решений в секунду вместо 60 — отсюда часть микропауз.

Как: DXGI Desktop Duplication (пакет dxcam) — Windows отдаёт кадр рабочего стола прямо из
видеопамяти, ~2-4 мс на весь экран. Поток захвата крутится сам по себе и кладёт последний кадр
в атрибут (присваивание ссылки атомарно под GIL — лок не нужен). Потребитель (crop) вырезает
кусок numpy-срезом за доли миллисекунды. Это классическая схема "производитель — последний кадр":
читателю не нужна очередь всех кадров, только самый свежий.

Откат: нет dxcam / не Windows / кадр чёрный (эксклюзивный полноэкранный режим) / поток упал /
зона вне монитора -> crop() возвращает None, и VisionManager снимает зону как раньше (mss).

Как проверить без игры: frame_source_test.py — подставной grabber (без dxcam и Windows).
Замер на ПК: python -c "from src.core.vision.frame_source import bench; bench()"
"""
from __future__ import annotations

import logging
import threading
import time

import numpy as np

logger = logging.getLogger(__name__)

ENABLED = True                 # False — всегда старый захват (mss), без потока
POLL_S = 0.002                 # пауза цикла захвата: grab() не блокирует, новых кадров нет -> None
BLACK_CHECK_FRAMES = 8         # столько первых кадров подряд чёрные -> DXGI не видит игру, отключаемся
RESTART_MAX = 5               # пересоздать захват после потери доступа DXGI не больше стольких раз
RESTART_DELAY_S = 2.0
STALE_DEAD_S = 1.0             # поток не жив и кадр старше этого -> не отдаём (откат на mss)


def _dxcam_grabber(monitor: dict):
    """
    Фабрика захвата через dxcam (ленивый импорт: модуль и тесты работают без него).
    Возвращает функцию grab() -> BGR-кадр всего монитора или None (кадр не обновился).
    output_idx — номер монитора у DXGI; mss monitors[1] — основной, у DXGI это 0.
    """
    # COM этого (свежего) потока — до импорта dxcam/comtypes. Код возврата не важен:
    # S_FALSE "уже инициализирован" тоже годится; главное — не в чужом потоке.
    try:
        import ctypes
        ctypes.windll.ole32.CoInitializeEx(None, 0x2)   # COINIT_APARTMENTTHREADED, как ждёт comtypes
    except Exception:
        pass
    import dxcam                                     # noqa: WPS433 — нужен только на Windows
    cam = dxcam.create(output_idx=0, output_color="BGR")
    if cam is None:
        raise RuntimeError("dxcam.create вернул None")
    return cam.grab, (lambda: getattr(cam, "release", lambda: None)())


class FrameSource:
    def __init__(self, monitor: dict, grabber_factory=None, clock=time.monotonic) -> None:
        self._mon = dict(monitor)
        self._factory = grabber_factory or _dxcam_grabber
        self._clock = clock
        self._frame: "tuple[np.ndarray, float] | None" = None   # (кадр, время) — пишет только поток
        self._stop = threading.Event()
        self._thread: "threading.Thread | None" = None
        self.disabled_reason: "str | None" = None if ENABLED else "выключен флагом"
        self.frames = 0
        self.grab_s = 0.0                              # суммарное время grab() — для PERF
        self._restarts = 0

    # --- поток ---
    INIT_WAIT_S = 3.0              # сколько start() ждёт, пока поток поднимет DXGI

    def start(self) -> bool:
        """
        Запустить поток захвата. False — недоступен (причина в disabled_reason), работаем по-старому.

        DXGI поднимаем ВНУТРИ своего потока (лог 2026-10-06 05:09: "[WinError -2147417850] Изменение
        режима для потока после его установки невозможно" = RPC_E_CHANGED_MODE). dxcam работает через
        COM (comtypes), а COM-режим задаётся на поток один раз. Главный поток приложения COM уже
        инициализировал по-своему (окно приложения, pywebview), и dxcam там падал — а в замере
        (`python -c ... bench()`) окна не было, поэтому там всё работало. В свежем потоке COM чистый.
        """
        if self.disabled_reason is not None:
            return False
        ready = threading.Event()
        self._thread = threading.Thread(target=self._boot, args=(ready,), name="FrameSource", daemon=True)
        self._thread.start()
        ready.wait(self.INIT_WAIT_S)
        return self.disabled_reason is None

    def _boot(self, ready: threading.Event) -> None:
        try:
            grab, release = self._factory(self._mon)
        except Exception as e:                         # нет dxcam / не Windows / DXGI не поднялся
            self.disabled_reason = "DXGI недоступен: %s" % e
            logger.info("FrameSource: %s — захват по-старому (mss)", self.disabled_reason)
            ready.set()
            return
        ready.set()
        self._run(grab, release)

    def stop(self) -> None:
        self._stop.set()

    def _run(self, grab, release) -> None:
        black = 0
        try:
            while not self._stop.is_set():
                t0 = time.perf_counter()
                f = grab()
                self.grab_s += time.perf_counter() - t0
                if f is not None:
                    if self.frames < BLACK_CHECK_FRAMES:
                        black = black + 1 if int(f.max()) == 0 else 0
                        if black >= BLACK_CHECK_FRAMES:
                            self.disabled_reason = "DXGI отдаёт чёрный кадр (эксклюзивный полноэкранный режим?)"
                            logger.warning("FrameSource: %s — захват по-старому (mss)", self.disabled_reason)
                            self._frame = None
                            return
                    h, w = f.shape[:2]
                    if (w, h) != (self._mon["width"], self._mon["height"]):
                        self.disabled_reason = "кадр DXGI %dx%d не совпадает с монитором %dx%d" % (
                            w, h, self._mon["width"], self._mon["height"])
                        logger.warning("FrameSource: %s — захват по-старому (mss)", self.disabled_reason)
                        self._frame = None
                        return
                    self._frame = (f, self._clock())       # присваивание ссылки атомарно — без лока
                    self.frames += 1
                self._stop.wait(POLL_S)
        except Exception as e:                             # поток зрения не должен ронять бота
            try:
                release()
            except Exception:
                pass
            # DXGI теряет доступ при Alt+Tab / смене разрешения / окне UAC — это нормально:
            # пересоздаём захват через RESTART_DELAY_S, до RESTART_MAX раз; пока — откат на mss.
            self._frame = None
            self._restarts += 1
            if self._restarts > RESTART_MAX or self._stop.wait(RESTART_DELAY_S):
                self.disabled_reason = "поток захвата упал: %s" % e
                logger.error("FrameSource: %s — захват по-старому (mss)", self.disabled_reason)
                return
            logger.info("FrameSource: захват прерван (%s) — пересоздаю (%d/%d)", e, self._restarts, RESTART_MAX)
            try:
                grab, release = self._factory(self._mon)
            except Exception as e2:
                self.disabled_reason = "DXGI не пересоздался: %s" % e2
                logger.error("FrameSource: %s — захват по-старому (mss)", self.disabled_reason)
                return
            self._run(grab, release)
            return
        try:
            release()
        except Exception:
            pass

    # --- чтение ---
    @property
    def active(self) -> bool:
        return self.disabled_reason is None and self._frame is not None

    def crop(self, roi: dict) -> "np.ndarray | None":
        """
        Вырезка зоны (координаты экрана, как у mss) из последнего кадра — копия, BGR uint8.
        None — источник недоступен или зона не целиком на мониторе: снимай по-старому.
        Кадр без изменений экрана не обновляется (так устроен DXGI) — он всё ещё верный.
        """
        fr = self._frame
        if fr is None or self.disabled_reason is not None:
            return None
        frame, t = fr
        if self._thread is not None and not self._thread.is_alive() and self._clock() - t > STALE_DEAD_S:
            return None
        x0 = int(roi["left"]) - self._mon["left"]
        y0 = int(roi["top"]) - self._mon["top"]
        x1, y1 = x0 + int(roi["width"]), y0 + int(roi["height"])
        if x0 < 0 or y0 < 0 or x1 > frame.shape[1] or y1 > frame.shape[0]:
            return None
        # copy(): потребители иногда пишут в кадр (рисуют отладку); общий кадр портить нельзя.
        return frame[y0:y1, x0:x1, :3].copy()

    def stats_take(self) -> "tuple[int, float]":
        n, s = self.frames, self.grab_s
        self.frames, self.grab_s = 0, 0.0
        return n, s


def bench(seconds: float = 3.0) -> None:
    """Замер на ПК: сколько кадров/с даёт DXGI и сколько стоит вырезка против mss."""
    import mss
    sct = mss.mss()
    mon = sct.monitors[1]
    fs = FrameSource(mon)
    if not fs.start():
        print("DXGI недоступен:", fs.disabled_reason)
        return
    time.sleep(seconds)
    n, s = fs.stats_take()
    roi = {"left": mon["left"] + 100, "top": mon["top"] + 100, "width": 400, "height": 200}
    t0 = time.perf_counter()
    for _ in range(100):
        fs.crop(roi)
    t_crop = (time.perf_counter() - t0) * 10
    t0 = time.perf_counter()
    for _ in range(20):
        sct.grab(roi)
    t_mss = (time.perf_counter() - t0) * 50
    fs.stop()
    print("DXGI: %.0f кадров/с, grab %.2f мс | вырезка 400x200: %.3f мс | mss той же зоны: %.2f мс"
          % (n / seconds, s / max(1, n) * 1000, t_crop, t_mss))
