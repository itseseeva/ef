"""
tools/capture_ready_slots.py — снимки "скилл готов" для каждого слота панели скиллов (из терминала).

То же, что кнопка "Снять скиллы" в программе и автоснимок при первом "Старт": вся логика —
в src/core/vision/ready_capture.py. Панель находится по форме, клавиши читаются с табличек —
работает на любом аккаунте и с любыми биндами. Поменял скилл/бинды — запусти снова.

Как пользоваться (бот на "Стоп", игра на экране, НЕ в бою, все скиллы ГОТОВЫ — без цифр отката):
    python tools/capture_ready_slots.py              -> через 5 с снимает панель 4 с подряд
    python tools/capture_ready_slots.py скрин.png    -> то же со скриншота (проверка без игры)
Ничего не нажимает — только смотрит на экран.
"""

import argparse
import os
import sys

import cv2

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
from src.core.vision.ready_capture import build_snapshots, grab_frames      # noqa: E402,F401
from src.core.vision.skill_bar import SNAP_DIR                               # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Снимки готовых скиллов для бота")
    ap.add_argument("image", nargs="?", help="скриншот вместо живого экрана (проверка без игры)")
    ap.add_argument("--seconds", type=float, default=4.0, help="сколько секунд снимать (по умолчанию 4)")
    ap.add_argument("--delay", type=float, default=5.0, help="пауза перед снимком, чтобы переключиться в игру")
    ap.add_argument("--out", default=SNAP_DIR, help="куда сохранить (по умолчанию src/config/skill_ready)")
    args = ap.parse_args()

    if args.image:
        img = cv2.imread(args.image)
        if img is None:
            print("Не открылся файл: %s" % args.image)
            return 1
        res = build_snapshots([img[..., :3]], args.out, args.image)
    else:
        frames, bar, origin = grab_frames(args.seconds, args.delay)
        res = build_snapshots(frames, args.out, "экран", bar, origin)
    if not res["ok"]:
        print(res["message"])
        return 1
    print("Масштаб панели %.2f" % res["scale"])
    print("\n слот | яркость | статус")
    print("------+---------+---------------------------------")
    for x in res["slots"]:
        print("  %-4s| %7d | %s" % (x["key"], x["brightness"], x["status"]))
    bad = [x["key"] for x in res["slots"] if not x["ok"]]
    print("\n%s" % res["message"])
    print("Сохранено: %s" % args.out)
    print("Проверь глазами: %s" % res["preview_path"])
    if bad:
        print("Внимание: слоты %s — посмотри на превью. Если там цифры отката, подожди и запусти ещё раз."
              % " ".join(bad))
    print("Готово. Бот подхватит снимки на следующем 'Старт'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
