"""
tools/build_movement_model.py — записи движений (movement_records/*.jsonl) -> модель
src/config/movement_model.json: библиотека коротких "фраз" движения + статистика.

Идея (motion matching): бот не проигрывает запись целиком, а складывает движение из
настоящих кусочков твоего бега по 1-3 с — подбирая их под задачу (не уйти далеко, в бою
не отходить от моба). Тайминги каждого нажатия — живые, а сочетания каждый раз новые.

Фраза = последовательность СОСТОЯНИЙ клавиш: [[0, "w"], [420, "w+d"], [910, "d"], ...]
(мс от начала фразы -> какие клавиши зажаты). Состояния, а не события "нажал/отпустил":
при склейке двух фраз проигрыватель сравнивает состояния и не делает лишнего
"отпустил W — тут же нажал W" на стыке (такой дребезг выдал бы бота).

Запуск (после каждой новой записи): python tools/build_movement_model.py
Как проверить без игры: python tools/build_movement_model.py --selftest
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import random
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
REC_DIR = os.path.join(ROOT, "movement_records")
OUT_PATH = os.path.join(ROOT, "src", "config", "movement_model.json")

MOVE_KEYS = ("w", "a", "s", "d", "space", "shift")
PHRASE_S = (1.0, 3.0)        # длина фразы: случайная в этих пределах, режем по ближайшей смене клавиш
PHRASE_MAX_S = 4.0           # без смены клавиш дольше (W зажата 6 с) — режем принудительно
IDLE_MAX_FRAC = 0.5          # фразы, где больше половины времени ничего не зажато, не берём
PASSES = 2                   # проходов нарезки с разными точками старта: фраз вдвое больше, они
                             # частично перекрываются — а перекрытие и есть "похоже, но не то же"
# Фраза годится для боя, если в ней нет прыжка/рывка (сбивают каст, тратят рывок), она
# не длиннее COMBAT_MAX_S и не уводит назад дольше COMBAT_MAX_BACK_S (уйдём из ближнего боя).
COMBAT_MAX_S = 2.0
COMBAT_MAX_BACK_S = 0.6
TURN_GAP_S = 0.06            # пауза мыши длиннее — жест поворота закончился
TURN_MIN_COUNTS = 150        # жесты слабее — это дрожь руки, а не поворот
TURN_MAX_S = 2.5
COMBAT_PHRASE_S = (0.5, 1.5)  # отдельный проход коротких фраз: в бою движение мелкое (шаг, стрейф),
                              # а из обычной нарезки (1-3 с) для боя годилась лишь каждая четвёртая


# ---------------------------------------------------------------------------------------
# Чтение записи
# ---------------------------------------------------------------------------------------

def read_segments(lines: "list[str]") -> "list[list[tuple[float, frozenset]]]":
    """
    JSONL одной записи -> непрерывные отрезки (между start/focus и stop/unfocus), каждый —
    список (t, зажатые клавиши) в моменты смены состояния. Мышь и кнопки мыши пока не
    нужны: камерой в этой версии рулит бот (слежение за целью / поиск).
    """
    segments: "list[list[tuple[float, frozenset]]]" = []
    cur: "list[tuple[float, frozenset]] | None" = None
    held: "set[str]" = set()
    for line in lines:
        e = json.loads(line)
        if "meta" in e:
            continue
        ev = e.get("ev")
        if ev in ("start", "focus"):
            held = set()
            cur = [(e["t"], frozenset())]
        elif ev in ("stop", "unfocus"):
            if cur is not None:
                cur.append((e["t"], frozenset()))
                segments.append(cur)
            cur = None
        elif "k" in e and cur is not None and e["k"] in MOVE_KEYS:
            (held.add if e["d"] else held.discard)(e["k"])
            state = frozenset(held)
            if e["t"] <= cur[-1][0] and len(cur) > 1:
                # Два события в одну и ту же миллисекунду (отпустил D и нажал A "разом"):
                # одно состояние, без мгновенного промежуточного "только W".
                cur[-1] = (cur[-1][0], state)
                if cur[-2][1] == state:
                    cur.pop()
            elif state != cur[-1][1]:
                cur.append((e["t"], state))
    if cur is not None and len(cur) > 1:                  # запись оборвалась без stop
        cur.append((cur[-1][0], frozenset()))
        segments.append(cur)
    return segments


def read_turns(lines: "list[str]") -> "list[dict]":
    """
    Жесты поворота камеры: непрерывные куски движения мыши (без пауз > TURN_GAP_S) с
    заметным итоговым сдвигом по горизонтали. В ROAM бот поворачивает камеру ИМИ:
    берёт жест похожей величины и масштабирует до нужного угла — скорость руки (разгон,
    торможение, дрожь) остаётся твоей, а не "ровная линия" скрипта.
    """
    turns: "list[dict]" = []
    cur: "list[tuple[float, int, int]]" = []
    live = False

    def close() -> None:
        if len(cur) >= 3:
            net = sum(p[1] for p in cur)
            dur = cur[-1][0] - cur[0][0]
            if abs(net) >= TURN_MIN_COUNTS and 0.05 <= dur <= TURN_MAX_S:
                t0 = cur[0][0]
                turns.append({"dur": round(dur, 3), "dx": net,
                              "pts": [[int(round((t - t0) * 1000)), dx, dy] for t, dx, dy in cur]})
        cur.clear()

    for line in lines:
        e = json.loads(line)
        ev = e.get("ev")
        if ev in ("start", "focus"):
            live = True
        elif ev in ("stop", "unfocus"):
            close()
            live = False
        elif "m" in e and live:
            if cur and e["t"] - cur[-1][0] > TURN_GAP_S:
                close()
            cur.append((e["t"], int(e["m"][0]), int(e["m"][1])))
    close()
    return turns


def key_holds(segments) -> "dict[str, list[float]]":
    """Длительности удержания каждой клавиши (для статистики и проверки "похоже на человека")."""
    holds: "dict[str, list[float]]" = {k: [] for k in MOVE_KEYS}
    for seg in segments:
        since: "dict[str, float]" = {}
        prev: frozenset = frozenset()
        for t, st in seg:
            for k in st - prev:
                since[k] = t
            for k in prev - st:
                if k in since:
                    holds[k].append(t - since.pop(k))
            prev = st
    return holds


# ---------------------------------------------------------------------------------------
# Нарезка на фразы
# ---------------------------------------------------------------------------------------

def _state_name(st: frozenset) -> str:
    return "+".join(k for k in MOVE_KEYS if k in st)      # порядок MOVE_KEYS: "w+d", а не "d+w"


def phrase_features(steps: "list[tuple[float, frozenset]]", dur: float) -> dict:
    """
    Чем фраза двигает персонажа, в "секундах бега": fwd (+ вперёд / − назад), side
    (+ вправо / − влево). Диагональ (W+D) — по 0.707 на ось, как в большинстве игр.
    Назад обычно медленнее — S считаем за 0.7. Это грубая оценка без калибровки, её
    хватает, чтобы "не убегать далеко" и "в бою не пятиться от моба".
    """
    fwd = side = idle = back = 0.0
    jumps = dodges = 0
    prev: frozenset = frozenset()
    for i, (t, st) in enumerate(steps):
        t_next = steps[i + 1][0] if i + 1 < len(steps) else dur
        d = max(0.0, t_next - t)
        f = (1.0 if "w" in st else 0.0) - (0.7 if "s" in st else 0.0)
        s = (1.0 if "d" in st else 0.0) - (1.0 if "a" in st else 0.0)
        norm = 0.707 if f and s else 1.0
        fwd += f * norm * d
        side += s * norm * d
        if not (st & {"w", "a", "s", "d"}):
            idle += d
        if "s" in st:
            back += d
        jumps += "space" in st and "space" not in prev
        dodges += "shift" in st and "shift" not in prev
        prev = st
    return {"fwd": round(fwd, 3), "side": round(side, 3), "idle": round(idle, 3), "back": round(back, 3),
            "jumps": jumps, "dodges": dodges}


def cut_phrases(segments, rng: random.Random, passes: int = PASSES,
                phrase_s: "tuple[float, float]" = PHRASE_S) -> "list[dict]":
    """
    Каждый отрезок режем на фразы: старт — в момент смены клавиш, длина — случайная из
    PHRASE_S, конец — первая смена клавиш после неё (не посреди удержания). Зажатые на
    старте клавиши фраза начинает уже зажатыми (состояние в t=0), на конце — отпускает.
    """
    out: "list[dict]" = []
    for p in range(passes):
        for seg in segments:
            times = [t for t, _ in seg]
            i = rng.randrange(0, min(len(seg), 6)) if p else 0   # второй проход — со сдвигом старта
            while i < len(seg) - 1:
                t0 = seg[i][0]
                want = rng.uniform(*phrase_s)
                j = i + 1
                while j < len(seg) - 1 and seg[j][0] - t0 < want:
                    j += 1
                t_end = min(seg[j][0], t0 + PHRASE_MAX_S)
                steps = [(t - t0, st) for t, st in seg[i:j] if t < t_end]
                dur = t_end - t0
                i = j if seg[j][0] <= t_end else next(k for k in range(i + 1, len(seg)) if times[k] >= t_end)
                if dur < phrase_s[0] * 0.8 or not steps:
                    continue
                feats = phrase_features(steps, dur)
                if feats["idle"] > IDLE_MAX_FRAC * dur:
                    continue
                out.append({
                    "dur": round(dur, 3),
                    "steps": [[int(round(t * 1000)), _state_name(st)] for t, st in steps],
                    **feats,
                    "combat_ok": (feats["jumps"] == 0 and feats["dodges"] == 0 and dur <= COMBAT_MAX_S
                                  and feats["back"] <= COMBAT_MAX_BACK_S),
                })
    return out


def _pct(a: "list[float]", q: float) -> float:
    if not a:
        return 0.0
    s = sorted(a)
    return s[min(len(s) - 1, int(q * (len(s) - 1) + 0.5))]


def build(paths: "list[str]", seed: int = 7) -> dict:
    rng = random.Random(seed)            # фиксированный seed: та же запись -> та же модель (воспроизводимо)
    segments = []
    turns: "list[dict]" = []
    for p in paths:
        with open(p, encoding="utf-8") as f:
            lines = f.read().splitlines()
        segments += read_segments(lines)
        turns += read_turns(lines)
    for n, tr in enumerate(turns):
        tr["id"] = n
    phrases = cut_phrases(segments, rng) + cut_phrases(segments, rng, passes=1, phrase_s=COMBAT_PHRASE_S)
    for n, ph in enumerate(phrases):
        ph["id"] = n                     # номер — для "не повторять недавние фразы" в контроллере
    holds = key_holds(segments)
    total = sum(seg[-1][0] - seg[0][0] for seg in segments)
    stats = {k: {"n": len(v), "p10": round(_pct(v, 0.1), 3), "med": round(_pct(v, 0.5), 3),
                 "p90": round(_pct(v, 0.9), 3)} for k, v in holds.items() if v}
    return {
        "version": 1,
        "built": time.strftime("%Y-%m-%d %H:%M:%S"),
        "sources": [os.path.basename(p) for p in paths],
        "recorded_s": round(total, 1),
        "hold_stats": stats,
        "phrases": phrases,
        "turns": turns,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Модель движения из записей movement_records/")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    paths = sorted(glob.glob(os.path.join(REC_DIR, "*.jsonl")))
    if not paths:
        print("Нет записей в", REC_DIR, "— сначала tools/record_movement.py")
        return 1
    model = build(paths)
    ph = model["phrases"]
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(model, f, ensure_ascii=False, separators=(",", ":"))
    print("Записей: %d, %.1f мин движения" % (len(paths), model["recorded_s"] / 60))
    print("Фраз: %d (для боя годятся %d), жестов поворота камеры: %d"
          % (len(ph), sum(p["combat_ok"] for p in ph), len(model["turns"])))
    print("Удержание клавиш (медиана): " + ", ".join("%s %.2f с" % (k, v["med"]) for k, v in model["hold_stats"].items()))
    print("Сохранено:", OUT_PATH)
    return 0


def selftest() -> int:
    fails = 0

    def check(name, cond, info=""):
        nonlocal fails
        print(("PASS " if cond else "FAIL ") + name + ((" | %s" % (info,)) if info != "" else ""))
        fails += not cond

    def ev(t, k, d):
        return json.dumps({"t": t, "k": k, "d": d})

    lines = [json.dumps({"meta": {}}), json.dumps({"t": 0.0, "ev": "start"}),
             ev(0.5, "w", 1), ev(1.5, "d", 1), ev(2.0, "d", 0), ev(2.0, "a", 1), ev(2.6, "a", 0),
             ev(3.0, "space", 1), ev(3.15, "space", 0), ev(4.0, "w", 0), ev(4.2, "s", 1), ev(4.6, "s", 0),
             json.dumps({"t": 5.0, "ev": "unfocus"}), ev(5.5, "w", 1),          # вне игры — не попадёт
             json.dumps({"t": 6.0, "ev": "focus"}), ev(6.1, "d", 1), ev(7.4, "d", 0),
             json.dumps({"t": 8.0, "ev": "stop"})]
    segs = read_segments(lines)
    check("B1: два отрезка (до alt-tab и после)", len(segs) == 2, [len(s) for s in segs])
    check("B2: W вне игры не попала в запись", all("w" not in st for _, st in segs[1]))
    check("B3: смена D -> A в один момент — одно состояние 'w+a', без промежуточного",
          [(_state_name(st)) for _, st in segs[0]][:5] == ["", "w", "w+d", "w+a", "w"], [_state_name(st) for _, st in segs[0]])
    h = key_holds(segs)
    check("B4: удержания W/D/пробела посчитаны", abs(h["w"][0] - 3.5) < 1e-9 and abs(h["space"][0] - 0.15) < 1e-9
          and len(h["d"]) == 2, h)
    f = phrase_features([(0.0, frozenset({"w"})), (1.0, frozenset({"w", "d"})), (2.0, frozenset({"s"}))], 2.5)
    check("B5: признаки фразы: вперёд 1+0.707-0.35, вбок +0.707, назад 0.5",
          abs(f["fwd"] - (1 + 0.707 - 0.35)) < 1e-3 and abs(f["side"] - 0.707) < 1e-3 and f["back"] == 0.5, f)
    ph = cut_phrases(segs, random.Random(1), passes=1)
    check("B6: фразы нарезаны, длина 0.8-4 с", ph and all(0.8 <= p["dur"] <= PHRASE_MAX_S + 1e-9 for p in ph),
          [p["dur"] for p in ph])
    check("B7: фраза с прыжком не годится для боя", all(not p["combat_ok"] for p in ph if p["jumps"]))
    check("B8: шаги фразы начинаются с 0 мс и идут по возрастанию",
          all(p["steps"][0][0] == 0 and all(a[0] < b[0] for a, b in zip(p["steps"], p["steps"][1:])) for p in ph))
    mlines = [json.dumps({"t": 0.0, "ev": "start"})]
    mlines += [json.dumps({"t": round(1.0 + 0.01 * i, 3), "m": [30, 1]}) for i in range(20)]     # жест +600
    mlines += [json.dumps({"t": round(2.0 + 0.01 * i, 3), "m": [2, 0]}) for i in range(10)]      # дрожь +20
    mlines += [json.dumps({"t": round(3.0 + 0.01 * i, 3), "m": [-40, 0]}) for i in range(10)]    # жест -400
    mlines += [json.dumps({"t": 4.0, "ev": "stop"})]
    tr = read_turns(mlines)
    check("B9: жесты поворота: два (+600 и -400), дрожь отброшена", [t["dx"] for t in tr] == [600, -400], [t["dx"] for t in tr])
    check("B10: точки жеста от 0 мс", tr and tr[0]["pts"][0][0] == 0 and tr[0]["pts"][-1][0] == 190, tr[0]["pts"][-1] if tr else None)
    print("FAILS", fails)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
