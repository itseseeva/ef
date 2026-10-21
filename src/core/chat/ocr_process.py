"""
OCR в ОТДЕЛЬНОМ процессе: бот и распознавание текста не делят процессор и GIL.

Зачем (2026-10-09, лог: "чтение чата заняло 8-15 с"): EasyOCR (нейросеть на torch) работал
внутри процесса бота и занимал все ядра — чат читался секундами, а бою не хватало процессора.
Теперь EasyOCR живёт в своём процессе:
  - torch ограничен OCR_THREADS ядрами, приоритет процесса ниже обычного — Windows всегда
    отдаёт процессор сначала боту;
  - модель загружается ОДИН раз при старте, дальше строка распознаётся по запросу.

Почему subprocess + свои "кадры" в pipe, а не multiprocessing: multiprocessing на Windows
запускает дочерний процесс через "spawn" — он заново импортирует главный скрипт
(app_launcher.py) и может второй раз запустить бота/окно, если там нет защиты __main__.
Отдельный python, который выполняет ТОЛЬКО этот файл, такой ловушки не имеет.

Протокол (stdin/stdout дочернего процесса, двоичный):
  запрос: <IHH id, высота, ширина> + пиксели (серая картинка, uint8);
  ответ:  <II  id, длина>          + текст в UTF-8.  id=0 — "готов", текст = имя движка.
stdout дочернего процесса — только канал; любые print/предупреждения уходят в stderr.

Как проверить без игры: python src/core/chat/ocr_process.py --selftest [картинка.png]
(поднимает процесс, распознаёт картинку, печатает время).
"""
from __future__ import annotations

import logging
import os
import queue
import struct
import subprocess
import sys
import threading
import time

import numpy as np

logger = logging.getLogger(__name__)

OCR_THREADS = 2              # ядер для torch в OCR-процессе: больше — отнимет процессор у боя
START_TIMEOUT_S = 90.0       # первый запуск EasyOCR грузит модели (до минуты на медленном диске)
CALL_TIMEOUT_S = 5.0         # строку не распознали за столько — пусто (лучше пропуск, чем зависание)

_REQ = struct.Struct("<IHH")
_RESP = struct.Struct("<II")
_BELOW_NORMAL_PRIORITY = 0x00004000
_NO_WINDOW = 0x08000000


class OcrProcess:
    """
    Вызывается как обычный OCR: ocr(картинка) -> строка. Внутри — запрос в процесс-распознаватель.
    Процесс упал — перезапускаем при следующем вызове (не чаще раза в RESTART_GAP_S).
    """
    RESTART_GAP_S = 10.0

    def __init__(self) -> None:
        self.name = "EasyOCR в отдельном процессе"
        self._proc: "subprocess.Popen | None" = None
        self._resp: "queue.Queue[tuple[int, str]]" = queue.Queue()
        self._lock = threading.Lock()          # один запрос за раз: протокол последовательный
        self._next_id = 1
        self._last_start = -1e9
        self._start()

    # --- процесс ---
    def _start(self) -> None:
        self._last_start = time.monotonic()
        flags = (_BELOW_NORMAL_PRIORITY | _NO_WINDOW) if os.name == "nt" else 0
        env = dict(os.environ, OMP_NUM_THREADS=str(OCR_THREADS), MKL_NUM_THREADS=str(OCR_THREADS))
        # В собранном exe (PyInstaller) отдельного python нет: sys.executable — сам EZF.exe, и он
        # запускается в режиме распознавателя ключом --ocr-worker (см. ezf_main.py). Из python — как раньше.
        if getattr(sys, "frozen", False):
            cmd = [sys.executable, "--ocr-worker"]
        else:
            cmd = [sys.executable, os.path.abspath(__file__), "--worker"]
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=_child_stderr(),
            creationflags=flags, env=env,
        )
        self._resp = queue.Queue()
        threading.Thread(target=self._reader, args=(self._proc, self._resp),
                         name="OcrProcessReader", daemon=True).start()
        try:
            rid, txt = self._resp.get(timeout=START_TIMEOUT_S)
        except queue.Empty:
            self.close()
            raise RuntimeError("OCR-процесс не ответил за %.0f с" % START_TIMEOUT_S)
        if rid != 0 or txt.startswith("ERROR"):
            self.close()
            raise RuntimeError("OCR-процесс не поднялся: %s" % txt)
        self.name = "%s (отдельный процесс, %d ядра)" % (txt, OCR_THREADS)

    @staticmethod
    def _reader(proc: subprocess.Popen, out: "queue.Queue") -> None:
        # Отдельный поток читает ответы: read() блокирующий, а ждать ответа хотим с таймаутом.
        f = proc.stdout
        try:
            while True:
                head = f.read(_RESP.size)
                if len(head) < _RESP.size:
                    return
                rid, n = _RESP.unpack(head)
                out.put((rid, f.read(n).decode("utf-8", "replace")))
        except Exception:
            return

    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def close(self) -> None:
        p, self._proc = self._proc, None
        if p is not None:
            try:
                p.kill()
            except Exception:
                pass

    # --- распознавание ---
    def __call__(self, img: np.ndarray) -> str:
        if img.ndim == 3:
            img = img[..., :3].max(axis=2)       # серый: подготовленные строки чата уже серые
        img = np.ascontiguousarray(img, dtype=np.uint8)
        h, w = img.shape[:2]
        with self._lock:
            if not self.alive():
                if time.monotonic() - self._last_start < self.RESTART_GAP_S:
                    return ""
                logger.warning("OCR-процесс упал — перезапускаю")
                try:
                    self._start()
                except Exception as e:
                    logger.error("OCR-процесс не перезапустился: %s", e)
                    return ""
            rid = self._next_id
            self._next_id = self._next_id % 0xFFFFFFF0 + 1
            try:
                self._proc.stdin.write(_REQ.pack(rid, h, w) + img.tobytes())
                self._proc.stdin.flush()
            except Exception:
                self.close()
                return ""
            end = time.monotonic() + CALL_TIMEOUT_S
            while True:
                left = end - time.monotonic()
                if left <= 0:
                    # Ответ опоздал: процесс, видимо, завис — убиваем, следующий вызов поднимет заново.
                    logger.warning("OCR-процесс не ответил за %.0f с — перезапуск", CALL_TIMEOUT_S)
                    self.close()
                    return ""
                try:
                    got, txt = self._resp.get(timeout=left)
                except queue.Empty:
                    continue
                if got == rid:                   # старые (опоздавшие) ответы пропускаем
                    return txt


def make_ocr_process():
    """OCR в отдельном процессе; не вышло — обычный OCR в этом процессе (как раньше)."""
    try:
        eng = OcrProcess()
        logger.info("Чат: OCR — %s", eng.name)
        return eng
    except Exception as e:
        logger.warning("Чат: OCR-процесс не поднялся (%s) — читаю в процессе бота", e)
        from src.core.chat.ocr_engine import make_ocr
        return make_ocr()


# ---------------- дочерний процесс ----------------
def _child_stderr():
    """Куда распознаватель пишет предупреждения/ошибки. В консоль бота, если она есть (из python);
    в exe без консоли sys.stderr — файл лога (ezf_main._redirect_to_log), отдаём процессу его.
    Нет ни того, ни другого — DEVNULL: писать в "пустоту" безопасно, а падать из-за лога — нет."""
    try:
        sys.stderr.fileno()
        return sys.stderr
    except (AttributeError, OSError, ValueError):
        return subprocess.DEVNULL


def _worker() -> None:
    chan_in = sys.stdin.buffer
    chan_out = sys.stdout.buffer
    sys.stdout = sys.stderr                      # любой print в процессе — НЕ в канал

    def send(rid: int, txt: str) -> None:
        b = txt.encode("utf-8")
        chan_out.write(_RESP.pack(rid, len(b)) + b)
        chan_out.flush()

    try:
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
        sys.path.insert(0, root)
        if os.environ.get("EZF_OCR_FAKE"):       # для теста протокола без EasyOCR/torch
            class _Fake:
                name = "fake"

                def __call__(self, img):
                    return "%dx%d" % img.shape[:2]
            eng = _Fake()
        else:
            import torch                         # noqa: WPS433
            torch.set_num_threads(OCR_THREADS)
            from src.core.chat.ocr_engine import EasyOcr
            eng = EasyOcr()
    except Exception as e:
        send(0, "ERROR %s: %s" % (type(e).__name__, e))
        return
    send(0, eng.name)
    while True:
        head = chan_in.read(_REQ.size)
        if len(head) < _REQ.size:
            return                               # бот закрылся — закрываемся и мы
        rid, h, w = _REQ.unpack(head)
        raw = chan_in.read(h * w)
        try:
            txt = eng(np.frombuffer(raw, np.uint8).reshape(h, w))
        except Exception as e:
            print("OCR-процесс: ошибка распознавания: %s" % e, file=sys.stderr)
            txt = ""
        send(rid, txt)


def _selftest(path: "str | None") -> None:
    import cv2
    t0 = time.perf_counter()
    eng = OcrProcess()
    print("старт: %.1f с — %s" % (time.perf_counter() - t0, eng.name))
    img = cv2.imread(path, cv2.IMREAD_GRAYSCALE) if path else None
    if img is None:
        img = np.full((48, 400), 255, np.uint8)
        cv2.putText(img, "privet 123", (10, 36), cv2.FONT_HERSHEY_SIMPLEX, 1.2, 0, 2)
    for _ in range(3):
        t0 = time.perf_counter()
        txt = eng(img)
        print("распознано за %.3f с: %r" % (time.perf_counter() - t0, txt))
    eng.close()


if __name__ == "__main__":
    if "--worker" in sys.argv:
        _worker()
    else:
        sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))
        logging.basicConfig(level=logging.INFO)
        args = [a for a in sys.argv[1:] if a != "--selftest"]
        _selftest(args[0] if args else None)
