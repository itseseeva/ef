"""
Снимок "скиллы готовы" (2026-10-10, по форме панели — 2026-10-11): одна логика для трёх входов —
кнопка "Снять скиллы" (app_launcher.capture_skills), автоснимок при первом "Старт"
(skill_bar.SkillBarWatcher) и скрипт tools/capture_ready_slots.py.

Что делает:
  1. находит панель на экране — по форме (bar_layout.detect_layout: любой аккаунт и бинды,
     клавиши читаются с табличек), если не вышло — старым шаблоном подписей "1 2 3 ... =";
  2. 4 с снимает только рамку панели (~15 кадров/с) и для каждого слота берёт САМЫЙ ЯРКИЙ кадр:
     скилл, который досиживал откат, за это время выйдет из него (готовый слот ярче себя на откате);
  3. сохраняет в src/config/skill_ready/: slot_<клавиша>.png, preview.png и meta.json
     (масштаб, раскладка с клавишами — по ней бот потом сразу знает, где какой скилл).

Ничего не нажимает — только смотрит на экран.
Как проверить без игры: skill_snap_test.py / bar_layout_test.py (кадры со скриншотов).
"""
from __future__ import annotations

import base64
import os
import time

import cv2
import numpy as np

from src.core.skill_check import SNAP_READY_RATIO, SNAP_SAME_NCC
from src.core.vision.skill_bar import (SNAP_DIR, BarGeometry, LayoutGeometry, ReadySnapshots,
                                       _norm_gray, locate_bar)

DARK_V = 60          # средняя яркость ниже этой — слот пустой или серый на откате: предупредим
# Пустой слот (без скилла — серая эмблема-солнце): тёмный И бесцветный. Замер 2026-10-11: пустой —
# яркость 30, насыщенность 9.5; скиллы — насыщенность 48-126, даже тёмный на откате ("5": 101).
EMPTY_V, EMPTY_S = 45, 25
CHANGED_RATIO = 1.25 # самый яркий кадр ярче самого тёмного во столько раз — слот выходил из отката


def locate(frame: np.ndarray, use_ocr: bool = True):
    """Панель на кадре монитора: LayoutGeometry (по форме) или BarGeometry (старый шаблон), или None."""
    from src.core.vision.bar_layout import detect_layout
    lay = detect_layout(frame, use_ocr=use_ocr)
    if lay is not None:
        return LayoutGeometry(lay)
    return locate_bar(frame)


def grab_frames(seconds: float, delay: float = 0.0, on_second=None, use_ocr: bool = True):
    """
    Снимает панель seconds секунд (~15 кадров/с). Возвращает (кадры, панель, (ox, oy)).
    Панель ищем ОДИН раз на полном кадре, дальше снимаем только её рамку: 60 полных кадров
    1920x1080 — ~370 МБ памяти, а 60 вырезок панели — ~3 МБ. Панель не найдена — ([полный кадр], None, (0, 0)).
    on_second(осталось_секунд) — раз в секунду, для отсчёта в интерфейсе.
    """
    import mss                                     # только для живого режима: со скриншотом не нужен
    for left in range(int(delay), 0, -1):
        print("  снимаю через %d с — переключись в игру..." % left, flush=True)
        time.sleep(1.0)
    with mss.mss() as sct:
        mon = dict(sct.monitors[1])                # тот же монитор, что смотрит бот (vision_manager)
        full = np.array(sct.grab(mon))[..., :3]
        # Последний снятый экран — на диск (для разбора, если что-то не нашлось/нашлось не так).
        # JPEG, а не PNG: ~50 мс против ~300, разбирать хватает.
        try:
            os.makedirs(SNAP_DIR, exist_ok=True)
            cv2.imwrite(os.path.join(SNAP_DIR, "last_capture.jpg"), full, [cv2.IMWRITE_JPEG_QUALITY, 92])
        except Exception:
            pass
        bar = locate(full, use_ocr)
        if bar is None:
            return [full], None, (0, 0)
        x0, y0, x1, y1 = bar.bounds()              # px кадра монитора
        roi = {"left": mon["left"] + x0, "top": mon["top"] + y0, "width": x1 - x0, "height": y1 - y0}
        frames = []
        t0 = time.monotonic()
        end, next_tick = t0 + seconds, t0
        while time.monotonic() < end:
            now = time.monotonic()
            if on_second is not None and now >= next_tick:
                on_second(max(1, int(round(end - now))))
                next_tick += 1.0
            frames.append(np.array(sct.grab(roi))[..., :3])
            time.sleep(0.065)                      # ~15 кадров/с, как поток панели в боте
    return frames, bar, (x0, y0)


def crop(frame: np.ndarray, bar, key: str, ox: int = 0, oy: int = 0) -> np.ndarray:
    """Вырезка слота; ox, oy — где на кадре монитора левый верх frame (кадр может быть вырезкой панели)."""
    x, y, w, h = bar.slot_rect(key)
    return frame[y - oy:y - oy + h, x - ox:x - ox + w, :3].copy()


def brightness(img: np.ndarray) -> float:
    return float(img.max(axis=2).mean())           # V из HSV без cvtColor — как в skill_bar.slot_stats


def is_empty_slot(img: np.ndarray) -> bool:
    """Слот без скилла: тёмный и почти серый. Это не "откат" — пугать человека нечем, бот его не жмёт."""
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    return float(hsv[..., 2].mean()) < EMPTY_V and float(hsv[..., 1].mean()) < EMPTY_S


def ui_order(bar) -> "list[str]":
    """Как на панели в игре: верхний ряд, потом нижний, слева направо."""
    return sorted(bar.keys, key=lambda k: (0 if bar.row(k) == "top" else 1, bar.slot_rect(k)[0]))


def preview(bar, snaps: "dict[str, np.ndarray]", notes: "dict[str, str]", path: str) -> None:
    """Все снимки в ряд, x3, с подписью клавиши и пометкой — чтобы проверить глазами за секунду."""
    tiles = []
    for key in ui_order(bar):
        img = snaps.get(key)
        tile = np.zeros((150, 130, 3), np.uint8)
        if img is not None:
            big = cv2.resize(img, (img.shape[1] * 3, img.shape[0] * 3), interpolation=cv2.INTER_NEAREST)
            tile[:min(150, big.shape[0]), :min(130, big.shape[1])] = big[:150, :130]
        note = notes.get(key, "")
        color = (80, 220, 80) if note == "ok" else (60, 160, 255)
        cv2.putText(tile, bar.label(key)[:4], (4, 128), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)
        cv2.putText(tile, note[:12], (40, 128), cv2.FONT_HERSHEY_SIMPLEX, 0.4, color, 1)
        tiles.append(tile)
    if tiles:
        cv2.imwrite(path, np.hstack(tiles))


def _png_data_url(img: np.ndarray, scale: int = 1) -> str:
    if scale != 1:
        img = cv2.resize(img, (img.shape[1] * scale, img.shape[0] * scale), interpolation=cv2.INTER_NEAREST)
    ok, buf = cv2.imencode(".png", img)
    return "data:image/png;base64," + base64.b64encode(buf.tobytes()).decode("ascii") if ok else ""


def build_snapshots(frames: "list[np.ndarray]", out: str = SNAP_DIR, source: str = "экран",
                    bar=None, origin: "tuple[int, int]" = (0, 0), use_ocr: bool = True) -> dict:
    """
    Кадры -> снимки готовых слотов на диск + ответ для интерфейса:
    {"ok", "message", "preview_png", "slots": [{"key", "row", "icon_png", "brightness", "ok", "status"}]}.
    "key" в ответе — как написано на панели ("Z", "-"), чтобы человек узнал свою клавишу.
    """
    ox, oy = origin
    if bar is None and frames:
        bar = locate(frames[0], use_ocr)
    if bar is None:
        # Кадр, на котором панель не нашлась, — на диск (last_fail.png): по нему видно, что было на экране.
        try:
            os.makedirs(out, exist_ok=True)
            if frames:
                cv2.imwrite(os.path.join(out, "last_fail.png"), frames[0])
        except Exception:
            pass
        return {"ok": False, "preview_png": None, "slots": [],
                "message": "Панель скиллов не найдена: игра должна быть на экране, панель скиллов видна"}

    old = ReadySnapshots.load(out)
    best: "dict[str, np.ndarray]" = {}
    notes: "dict[str, str]" = {}
    vals_best: "dict[str, float]" = {}
    for key in bar.keys:
        crops = [crop(f, bar, key, ox, oy) for f in frames]
        vals = [brightness(c) for c in crops]
        i = int(np.argmax(vals))                   # самый яркий кадр = скилл готов (см. шапку)
        snap, v = crops[i], vals[i]
        note = "ok"
        if max(vals) > CHANGED_RATIO * max(1.0, min(vals)):
            note = "выходил из отката - взял яркий кадр"
        # Был прошлый снимок, и сейчас слот — та же иконка, но заметно темнее: он на откате.
        # Оставляем старый снимок, иначе бот считал бы "готовым" тёмный слот.
        if old is not None and key in old.crops and old.crops[key].shape == snap.shape:
            o = old.crops[key]
            ncc = float(np.dot(_norm_gray(snap)[0], _norm_gray(o)[0]))
            if ncc >= SNAP_SAME_NCC and v < SNAP_READY_RATIO * brightness(o):
                snap, v, note = o, brightness(o), "на откате - оставил старый"
        if note == "ok" and is_empty_slot(snap):
            note = "пустой слот"
        elif v < DARK_V and note == "ok":
            note = "тёмный: пустой или откат?"
        best[key], notes[key], vals_best[key] = snap, note, v

    order = ui_order(bar)
    meta = {"captured": time.strftime("%Y-%m-%d %H:%M:%S"), "source": source,
            "labels_order": [bar.label(k) for k in order]}
    if isinstance(bar, LayoutGeometry):
        meta["layout"] = bar.layout.to_json()      # координаты кадра монитора; бот сдвинет на свой монитор
    # Старые снимки другого формата (позиции "-" против клавиш "minus") не должны мешать: чистим.
    try:
        for f in os.listdir(out) if os.path.isdir(out) else []:
            if f.startswith("slot_") and f.endswith(".png"):
                os.remove(os.path.join(out, f))
    except OSError:
        pass
    ReadySnapshots(best, bar.scale, meta).save(out)
    prev = os.path.join(out, "preview.png")
    preview(bar, best, notes, prev)

    # "Выходил из отката" — не проблема (взяли яркий кадр), плиткой "проверь" не пугаем.
    slots = [{"key": bar.label(k), "row": bar.row(k),
              "icon_png": _png_data_url(best[k], 2), "brightness": int(round(vals_best[k])),
              "ok": notes[k] in ("ok", "пустой слот") or notes[k].startswith("выходил"), "status": notes[k]}
             for k in order]
    bad = sum(1 for x in slots if not x["ok"])
    msg = "Снимки сохранены" if not bad else "Снимки сохранены, проверь отмеченные слоты: %d" % bad
    unread = getattr(getattr(bar, "layout", None), "unread", 0)
    if unread:
        msg += " (слотов без клавиши: %d — бот их не жмёт)" % unread
    pv = cv2.imread(prev) if os.path.exists(prev) else None
    return {"ok": True, "message": msg, "preview_png": _png_data_url(pv) if pv is not None else None,
            "slots": slots, "scale": bar.scale, "preview_path": prev}
