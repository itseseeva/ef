"""
src/core/blackbox.py

"Чёрный ящик" и сборщик образцов текста HP. Оба пишут на диск ТОЛЬКО из
отдельного потока: кодирование PNG занимает десятки миллисекунд, а FSM-поток
обязан укладываться в 16 мс на тик (60 FPS). Из FSM-потока в очередь кладётся
готовая ссылка на массив — это микросекунды.

Чёрный ящик (BlackBox). Во время боя хранит в кольце последние ~1.2 с кадров
панели (по 10 кадров в секунду). Когда в боте происходит "интересное" (цель
пропала, шлагбаум не открылся, охранник отказал в Tab...), сбрасывает на диск
папку  blackbox/<время>_<№>_<событие>/  с кадрами панели из кольца, кадром мира
и info.json (состояние FSM в этот момент). По такой папке видно глазами, что
бот видел за секунду ДО инцидента, — это и есть доказательство вместо догадок.

Сборщик (HpSampleCollector). Для расшифровки цифр HP нужны картинки всех цифр
0-9, а у нас есть только часть. Сборщик сам сохраняет по одной картинке на
каждое НОВОЕ число на панели в hp_samples/ (имя содержит процент HP и хеш
текста); после нескольких боёв там накопятся все цифры.

Как тестировать без игры: blackbox_test.py — подставляет фейковые кадры и
проверяет, что файлы появились, лимиты соблюдаются, а вызывающий поток не
блокируется.

Идиомы: queue.Queue(maxsize) + put_nowait — если диск тормозит, лишнее
выбрасывается (счётчик dropped), а бот ждать диск НЕ будет; daemon-поток не
мешает закрыть программу.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from collections import deque

import cv2
import numpy as np

logger = logging.getLogger(__name__)


class BackgroundWriter:
    """Один поток-писатель с ограниченной очередью заданий (fn, args)."""

    def __init__(self, maxsize: int = 64) -> None:
        self._q: "queue.Queue[tuple]" = queue.Queue(maxsize=maxsize)
        self.dropped = 0
        self._t = threading.Thread(target=self._run, name="blackbox-writer", daemon=True)
        self._t.start()

    def submit(self, fn, *args) -> bool:
        """Положить задание. False — очередь полна (диск не успевает), задание выброшено."""
        try:
            self._q.put_nowait((fn, args))
            return True
        except queue.Full:
            self.dropped += 1
            return False

    def _run(self) -> None:
        while True:
            fn, args = self._q.get()
            try:
                fn(*args)
            except Exception:                       # писатель не должен умирать из-за одного файла
                logger.exception("blackbox: ошибка записи")
            finally:
                self._q.task_done()

    def flush(self, timeout: float = 5.0) -> bool:
        """Дождаться опустошения очереди (для тестов и выхода). True — успели."""
        end = time.monotonic() + timeout
        while self._q.unfinished_tasks and time.monotonic() < end:
            time.sleep(0.01)
        return self._q.unfinished_tasks == 0


def _imwrite(path: str, img: np.ndarray, params=()) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cv2.imwrite(path, img, list(params))


def _dump_event(folder: str, ring_frames: "list[tuple[float, np.ndarray]]",
                panel_now: "np.ndarray | None", world: "np.ndarray | None",
                info: dict) -> None:
    """Всё тяжёлое (PNG/JPEG, JSON) — здесь, в потоке-писателе."""
    os.makedirs(folder, exist_ok=True)
    t_event = info.get("_t", 0.0)
    for i, (t, fr) in enumerate(ring_frames):
        _imwrite(os.path.join(folder, "panel_%02d_dt%+.1f.png" % (i, t - t_event)), fr)
    if panel_now is not None:
        _imwrite(os.path.join(folder, "panel_now.png"), panel_now)
    if world is not None:
        _imwrite(os.path.join(folder, "world.jpg"), world, (cv2.IMWRITE_JPEG_QUALITY, 85))
    clean = {k: v for k, v in info.items() if not k.startswith("_")}
    with open(os.path.join(folder, "info.json"), "w", encoding="utf-8") as f:
        json.dump(clean, f, ensure_ascii=False, indent=1, default=str)


class BlackBox:
    RING_SAMPLE_S = 0.1          # кольцо пополняется 10 раз в секунду
    RING_LEN = 12                # ~1.2 с истории

    def __init__(self, root_dir: str, enabled: bool = True, max_events: int = 60,
                 max_per_name: int = 15, writer: "BackgroundWriter | None" = None) -> None:
        self.root = root_dir
        self.enabled = enabled
        self.max_events = max_events
        self.max_per_name = max_per_name
        self.writer = writer or BackgroundWriter()
        self._ring: "deque[tuple[float, np.ndarray]]" = deque(maxlen=self.RING_LEN)
        self._next_ring_at = 0.0
        self._count = 0
        self._per_name: "dict[str, int]" = {}
        self._stamp = time.strftime("%H%M%S")

    def tick(self, now: float, panel_frame: "np.ndarray | None") -> None:
        """Каждый тик FSM: раз в RING_SAMPLE_S копирует кадр панели в кольцо (~50 КБ)."""
        if not self.enabled or panel_frame is None or now < self._next_ring_at:
            return
        self._next_ring_at = now + self.RING_SAMPLE_S
        self._ring.append((now, panel_frame.copy()))

    def event(self, now: float, name: str, info: "dict | None" = None,
              panel_frame: "np.ndarray | None" = None, world_frame: "np.ndarray | None" = None,
              use_ring: bool = True) -> "str | None":
        """
        Сохранить инцидент. Возвращает папку или None (выключено / исчерпан лимит).
        Лимиты — чтобы один частый тип события не забил диск: всего max_events на
        запуск и не больше max_per_name одного имени.
        """
        if not self.enabled:
            return None
        n = self._per_name.get(name, 0)
        if self._count >= self.max_events or n >= self.max_per_name:
            return None
        self._count += 1
        self._per_name[name] = n + 1
        folder = os.path.join(self.root, "%s_%03d_%s" % (self._stamp, self._count, name))
        data = dict(info or {})
        data["_t"] = now
        data["event"] = name
        ring = list(self._ring) if use_ring else []
        ok = self.writer.submit(
            _dump_event, folder, ring,
            None if panel_frame is None else panel_frame.copy(),
            world_frame, data,
        )
        return folder if ok else None


def _save_sample(path_mask: str, path_bar: str, mask: np.ndarray, bar: "np.ndarray | None") -> None:
    # x4 NEAREST: крошечный 10x98 px мне же потом читать глазами — крупнее удобнее.
    big = cv2.resize(mask.astype(np.uint8) * 255, None, fx=4, fy=4, interpolation=cv2.INTER_NEAREST)
    _imwrite(path_mask, big)
    if bar is not None:
        _imwrite(path_bar, cv2.resize(bar, None, fx=4, fy=4, interpolation=cv2.INTER_NEAREST))


class HpSampleCollector:
    """По одной картинке на каждое новое число HP (по хешу текста) — сырьё для шаблонов цифр."""

    def __init__(self, root_dir: str, enabled: bool = True, max_files: int = 400,
                 writer: "BackgroundWriter | None" = None) -> None:
        self.root = root_dir
        self.enabled = enabled
        self.max_files = max_files
        self.writer = writer or BackgroundWriter()
        self._seen: "set[int]" = set()

    def offer(self, text, hp_percent: float, panel_frame: "np.ndarray | None",
              bar_rect: "tuple[int, int, int, int] | None") -> bool:
        """text — hp_text.HpText или None. True — образец поставлен на запись."""
        if not self.enabled or text is None or text.sig in self._seen or len(self._seen) >= self.max_files:
            return False
        self._seen.add(text.sig)
        bar = None
        if panel_frame is not None and bar_rect and bar_rect[2] > 0:
            bx, by, bw, bh = bar_rect
            bar = panel_frame[by:by + bh, bx:bx + bw].copy()
        base = "hp%03d_%08x" % (int(round(hp_percent)), text.sig)
        return self.writer.submit(
            _save_sample,
            os.path.join(self.root, base + "_mask.png"),
            os.path.join(self.root, base + "_bar.png"),
            text.mask.copy(), bar,
        )
