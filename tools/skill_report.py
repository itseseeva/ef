"""
Отчёт по скиллам после боя: что нажалось, подтвердилось ли нажатие и совпало ли с настройкой
цепочек в программе бота (src/config/skills_config.json).

Запуск (из папки EZF, после остановки бота):
    python tools/skill_report.py                  # последний прогон: input_debug.log
    python tools/skill_report.py --prev           # прошлый прогон: input_debug.prev.log
    python tools/skill_report.py путь/к/логу.log

Что читает из лога (пишет бот сам, ничего включать не надо):
    PRESS   6 (down ...                 — клавиша скилла реально нажата
    SKILLCHK 6 нажат: ...               — по панели видно: скилл ушёл в откат (нажатие сработало)
    SKILLCHK 6: до нажатия ... на откате — жали скилл, который был на откате (нажатие впустую)
    SKILLCHK 6: за 1.0 с отката нет     — нажатие не сработало (игра "съела")
    SKILLCHK 6 не нажался — жму ещё раз — повтор
    PRESS   6 на откате — переношу / пропуск — скилл не жали: был на откате
    CHAIN   START / END / снята обрывом / RESUME — цепочки

Печатает три таблицы и сохраняет их в skill_report.txt рядом с логом.
Чистый разбор текста: без игры, без зрения — можно гонять на любом старом логе.
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter, defaultdict

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

_T = r"(\d\d):(\d\d):(\d\d)\.(\d{3})"
RE_LINE = re.compile(_T + r"\s+(.*)$")
RE_PRESS = re.compile(r"^PRESS\s+(\S+) \(down")
RE_DEFER = re.compile(r"^PRESS\s+(\S+) на откате — переношу")
RE_DROP = re.compile(r"^PRESS\s+(\S+) пропуск: снова на откате")
RE_OK = re.compile(r"^SKILLCHK (\S+) нажат:")
RE_WASCD = re.compile(r"^SKILLCHK (\S+): до нажатия слот уже на откате")
RE_FAIL = re.compile(r"^SKILLCHK (\S+): за [\d.]+ с отката нет")
RE_RETRY = re.compile(r"^SKILLCHK (\S+) не нажался — жму ещё раз")
RE_NOCD = re.compile(r"^SKILLCHK (\S+): .*нет отката на панели")
RE_START = re.compile(r"^CHAIN\s+START '(.+?)': (\d+) скилл")
RE_END = re.compile(r"^CHAIN\s+END '(.+?)'")
RE_ABORT = re.compile(r"^CHAIN\s+'(.+?)' снята обрывом")
RE_WEAVE = re.compile(r"^WEAVE\s+'(.+?)' \(\[(.*?)\]\)")
# "готов по" — "панель" / "таймер" / "приоритет 'Имя' шаг 2/4" (2026-10-11): до ", каст" или ")"
RE_PRIO = re.compile(r"^PRIO\s+жму (\S+) \(из '(.*?)', готов по (.+?)(?:, каст [\d.]+ с)?\)$")
RE_PRIO_ORDER = re.compile(r"^PRIO\s+порядок приоритетов.*?: (.*)$")

# Имя клавиши в логе нажатий -> подпись слота (как в конфиге)
_KEY_ALIAS = {"minus": "-", "equals": "=", "equal": "="}


def _sec(m) -> float:
    h, mi, s, ms = (int(x) for x in m)
    return h * 3600 + mi * 60 + s + ms / 1000.0


def load_config(path: str) -> "list[dict]":
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    out = []
    for c in sorted(raw.get("chains", []), key=lambda c: c.get("order", 0)):
        keys = [str(s.get("slot") or s.get("key")) for s in c.get("steps", [])]
        out.append({
            "name": c.get("name", "?"), "keys": keys, "cd": float(c.get("cooldownSeconds") or 0),
            "trigger": c.get("triggerAfter", ""),
            "cast": {str(s.get("slot") or s.get("key")): float(s.get("castTimeSeconds") or 0)
                     for s in c.get("steps", [])},
        })
    # Одинаковые имена (две "Цепочка #4") различаем по числу шагов.
    names = Counter(c["name"] for c in out)
    for c in out:
        c["label"] = c["name"] if names[c["name"]] == 1 else "%s [%d]" % (c["name"], len(c["keys"]))
    return out


def parse_log(path: str) -> "list[tuple[float, str]]":
    rows = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = RE_LINE.match(line.rstrip("\n"))
            if m:
                rows.append((_sec(m.groups()[:4]), m.group(5)))
    return rows


def analyse(rows, chains):
    presses = []                 # [{t, key, verdict, chain, retry}]
    deferred, dropped = Counter(), Counter()
    open_press = defaultdict(list)          # key -> индексы нажатий без вердикта (по порядку)
    retry_next = set()
    cur = None                   # идущая цепочка: {"chain": dict|None, "label", "expected", "pressed", "t0", "resume"}
    blocks = []
    own_keys = {k for c in chains if len(c["keys"]) == 1 and c["trigger"] == "always" for k in c["keys"]}
    weave_pending = Counter()

    def find_chain(name, n):
        cand = [c for c in chains if c["name"] == name.replace(" (доигрываю)", "")]
        exact = [c for c in cand if len(c["keys"]) == n]
        return (exact or cand or [None])[0]

    last_cut = [None]

    def close(t, how):
        nonlocal cur
        if cur is not None:
            exp = cur["expected"]
            if how == "доиграна" and exp is not None and len(cur["pressed"]) < len(exp):
                how = "прервана (перевыбор цели)"     # очередь сброшена посреди цепочки
            if not how.startswith("доиграна") and cur["chain"] is not None:
                last_cut[0] = cur["chain"]
            cur["t1"], cur["how"] = t, how
            blocks.append(cur)
            cur = None

    for t, msg in rows:
        m = RE_START.match(msg)
        if m:
            close(t, "новая цепочка")
            name, n = m.group(1), int(m.group(2))
            resume = "(доигрываю)" in name
            # Доигрывание — остаток ПОСЛЕДНЕЙ прерванной цепочки (имена у цепочек могут совпадать).
            ch = last_cut[0] if resume and last_cut[0] is not None else find_chain(name, n)
            cur = {"chain": ch, "label": (ch["label"] if ch else name) + (" (доигрываю)" if resume else ""),
                   "expected": (ch["keys"] if ch and not resume else None), "n": n, "pressed": [],
                   "t0": t, "resume": resume}
            continue
        if RE_END.match(msg):
            close(t, "доиграна")
            continue
        if RE_ABORT.match(msg):
            close(t, "оборвана (цель пропала)")
            continue
        m = RE_WEAVE.match(msg)
        if m:
            for k in re.findall(r"'([^']+)'", m.group(2)):
                weave_pending[k] += 1
            continue
        m = RE_PRESS.match(msg)
        if m:
            key = _KEY_ALIAS.get(m.group(1), m.group(1))
            src = None
            if weave_pending[key] > 0:
                weave_pending[key] -= 1
                src = "по своему КД"
            elif key in retry_next:
                retry_next.discard(key)
                src = "повтор"
            elif cur is not None:
                src = cur["label"]
                cur["pressed"].append(key)
            elif key in own_keys:
                src = "по своему КД"
            presses.append({"t": t, "key": key, "verdict": None, "src": src or "-"})
            open_press[key].append(len(presses) - 1)
            continue
        m = RE_DEFER.match(msg)
        if m:
            deferred[_KEY_ALIAS.get(m.group(1), m.group(1))] += 1
            continue
        m = RE_DROP.match(msg)
        if m:
            dropped[_KEY_ALIAS.get(m.group(1), m.group(1))] += 1
            continue
        m = RE_RETRY.match(msg)
        if m:
            retry_next.add(m.group(1))
            continue
        for rx, verdict in ((RE_OK, "ok"), (RE_WASCD, "was_cd"), (RE_FAIL, "fail"), (RE_NOCD, "no_cd")):
            m = rx.match(msg)
            if m:
                key = m.group(1)
                if open_press[key]:
                    presses[open_press[key].pop(0)]["verdict"] = verdict
                break
    close(rows[-1][0] if rows else 0.0, "лог кончился")
    return presses, deferred, dropped, blocks


def table(headers, rows) -> str:
    w = [max(len(str(h)), *(len(str(r[i])) for r in rows)) if rows else len(str(h)) for i, h in enumerate(headers)]
    line = "+" + "+".join("-" * (x + 2) for x in w) + "+"
    out = [line, "| " + " | ".join(str(h).ljust(w[i]) for i, h in enumerate(headers)) + " |", line]
    for r in rows:
        out.append("| " + " | ".join(str(c).ljust(w[i]) for i, c in enumerate(r)) + " |")
    out.append(line)
    return "\n".join(out)


def report(log_path: str, cfg_path: str) -> str:
    chains = load_config(cfg_path)
    rows = parse_log(log_path)
    presses, deferred, dropped, blocks = analyse(rows, chains)
    out = []
    dur = (rows[-1][0] - rows[0][0]) if rows else 0.0
    out.append("Лог: %s  (%.0f с, %d нажатий скиллов)" % (log_path, dur, len(presses)))
    out.append("Конфиг: %s" % cfg_path)

    # --- 1. По скиллам ---
    in_cfg = defaultdict(list)
    for c in chains:
        for i, k in enumerate(c["keys"]):
            in_cfg[k].append("%s шаг %d" % (c["label"], i + 1))
    keys = sorted(set(in_cfg) | {p["key"] for p in presses} | set(deferred) | set(dropped))
    own_cd = {c["keys"][0]: c["cd"] for c in chains if len(c["keys"]) == 1 and c["trigger"] == "always"}
    t1 = []
    for k in keys:
        ps = [p for p in presses if p["key"] == k]
        v = Counter(p["verdict"] for p in ps)
        oks = [p["t"] for p in ps if p["verdict"] == "ok"]
        gaps = [b - a for a, b in zip(oks, oks[1:])]
        avg = "%.1f с" % (sum(gaps) / len(gaps)) if gaps else "-"
        cd = ("%.0f с" % own_cd[k]) if k in own_cd else "-"
        good = v["ok"]
        bad = v["was_cd"] + v["fail"]
        verdict = ("НЕ НАЖИМАЛСЯ" if not ps and k in in_cfg else
                   "нет в настройке!" if k not in in_cfg else
                   "отлично" if bad == 0 and good else
                   "есть промахи" if good else "не подтверждён")
        t1.append([k, len(in_cfg.get(k, [])) or "-", len(ps), good, v["was_cd"], v["fail"],
                   deferred[k] + dropped[k], avg, cd, verdict])
    out.append("\n1) СКИЛЛЫ")
    out.append(table(["Клавиша", "Шагов в цепочках", "Нажато", "Сработал (откат пошёл)",
                      "Жал на откате", "Не сработал", "Не жал: на откате", "Интервал факт",
                      "КД в настройке", "Итог"], t1))

    # --- 2. По цепочкам ---
    t2 = []
    for c in chains:
        bs = [b for b in blocks if b["chain"] is c and not b["resume"]]
        rs = [b for b in blocks if b["chain"] is c and b["resume"]]
        full = sum(1 for b in bs if b["how"] == "доиграна")
        cut = sum(1 for b in bs if b["how"].startswith(("оборвана", "прервана")))
        st = [b["t0"] for b in bs]
        gaps = [b - a for a, b in zip(st, st[1:])]
        if len(c["keys"]) == 1 and c["trigger"] == "always":
            own = [p["t"] for p in presses if p["key"] == c["keys"][0] and p["verdict"] == "ok"]
            g = [b - a for a, b in zip(own, own[1:])]
            t2.append([c["label"], " ".join(c["keys"]), "по своему КД", len(own), "-", "-",
                       "%.1f с" % (sum(g) / len(g)) if g else "-", "%.0f с" % c["cd"]])
            continue
        t2.append([c["label"], " ".join(c["keys"]), c["trigger"], len(bs), full, "%d (+%d доиграно)" % (cut, len(rs)),
                   "%.1f с" % (sum(gaps) / len(gaps)) if gaps else "-", "%.0f с" % c["cd"]])
    out.append("\n2) ЦЕПОЧКИ")
    out.append(table(["Цепочка", "Скиллы по настройке", "Запуск", "Запусков", "Доиграна целиком",
                      "Оборвана", "Повтор факт", "Повтор в настройке"], t2))

    # --- 3. Каждый запуск: что жали против настройки ---
    t3 = []
    for b in blocks:
        exp = b["expected"]
        got = b["pressed"]
        if exp is None:
            cmp_ = "доигрывание: %d из %d" % (len(got), b["n"])
        else:
            miss = Counter(exp) - Counter(got)
            extra = Counter(got) - Counter(exp)
            if not miss and not extra and got == exp:
                cmp_ = "как в настройке"
            else:
                parts = []
                if miss:
                    parts.append("не нажаты: " + " ".join(sorted(miss.elements())))
                if extra:
                    parts.append("лишние: " + " ".join(sorted(extra.elements())))
                if not miss and not extra:
                    parts.append("порядок другой")
                cmp_ = "; ".join(parts)
        verd = []
        for k in got:
            p = next((p for p in presses if p["key"] == k and b["t0"] <= p["t"] <= b["t1"] + 0.01
                      and not p.get("_used")), None)
            if p is None:
                verd.append(k)
                continue
            p["_used"] = True
            mark = {"ok": "+", "was_cd": "к", "fail": "x", "no_cd": "?", None: "?"}[p["verdict"]]
            verd.append(k + mark)
        h, rem = divmod(int(b["t0"]), 3600)
        t3.append(["%02d:%02d:%02d" % (h, rem // 60, rem % 60), b["label"], " ".join(exp) if exp else "-",
                   " ".join(verd) or "-", b["how"], cmp_])
    out.append("\n3) КАЖДЫЙ ЗАПУСК ЦЕПОЧКИ  (+ сработал, к — жал на откате, x — не сработал, ? — не проверено)")
    out.append(table(["Время", "Цепочка", "По настройке", "Нажато факт", "Конец", "Сравнение"], t3))
    # --- 4. Ротация по приоритетам (если бот в этом режиме) ---
    prio = [(t, m.group(1), m.group(2), m.group(3)) for t, msg in rows for m in [RE_PRIO.match(msg)] if m]
    order = next((m.group(1) for _, msg in rows for m in [RE_PRIO_ORDER.match(msg)] if m), None)
    if prio:
        out.append("\n4) РОТАЦИЯ ПО ПРИОРИТЕТАМ" + ("  — порядок: " + order if order else ""))
        stat = defaultdict(lambda: {"n": 0, "panel": 0, "timer": 0, "t": []})
        for t, lab, src, why in prio:
            st = stat[lab]
            st["n"] += 1
            st["panel" if why.startswith("панел") else "timer"] += 1
            st["t"].append(t)
        rows4 = []
        for lab, st in stat.items():
            g = [b - a for a, b in zip(st["t"], st["t"][1:])]
            rows4.append([lab, st["n"], st["panel"], st["timer"], "%.1f с" % (sum(g) / len(g)) if g else "-"])
        out.append(table(["Действие", "Выбрано раз", "Готов по панели", "По таймеру", "Интервал"], rows4))
        seq = " ".join(lab for _, lab, _, _ in prio[:40])
        out.append("Первые действия по порядку: " + seq)
    return "\n".join(out)


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    log = args[0] if args else os.path.join(ROOT, "input_debug.prev.log" if "--prev" in sys.argv else "input_debug.log")
    cfg = os.path.join(ROOT, "src", "config", "skills_config.json")
    text = report(log, cfg)
    print(text)
    dst = os.path.join(os.path.dirname(os.path.abspath(log)), "skill_report.txt")
    with open(dst, "w", encoding="utf-8") as f:
        f.write(text + "\n")
    print("\nСохранено: %s" % dst)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")       # кириллица в консоли Windows
    main()
