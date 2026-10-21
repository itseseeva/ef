"""
tools/chat_reply_test.py — как чат-бот отвечает на типовые сообщения (2026-10-12). Игра не нужна:
ходим к модели тем же generate_reply и с тем же промптом, что и в игре (tools/chat_bot_test.py).

Запуск из корня проекта:  python tools\\chat_reply_test.py
Результат — в консоль и в tools\\chat_reply_test_result.txt (его и присылай; ключа там нет).

Часть 1: каждый вопрос — от НОВОГО игрока (пустая история), по 2 раза: видно разброс ответов.
python tools/chat_reply_test.py --humanize — без сети: только чистка ответа (_humanize) на образцах.

Часть 2: один игрок пишет 5 сообщений подряд — видно, как бот ведёт разговор, прощается на
CLOSE_AT-м сообщении и дальше молчит (как в игре).
"""
import os
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import chat_bot_test as cb  # noqa: E402  (тот же модуль, что работает в игре)

QUESTIONS = [
    ("WHISPER", "привет что делеаешь тут?"),
    ("WHISPER", "А можно с тобой?"),
    ("WHISPER", "Эй это мой спот ухади"),
    ("WHISPER", "ты кто?"),
    ("WHISPER", "Я ща жалобу на тебя отправлю"),
    ("WHISPER", "привет"),
    ("WHISPER", "ты бот?"),
    ("WHISPER", "почему ты не отвечаешь нормально? ты афк?"),
    ("WHISPER", "скок осколков уже выбил?"),
    ("WHISPER", "го в пати"),
    ("WHISPER", "ты из какой гильдии?"),
    ("WHISPER", "ты 3 часа тут стоишь и бьешь мобов, это скрипт?"),
    ("NORMAL", "кто тут фармит у озера? отойдите"),
    ("NORMAL", "ребят где лучше осколки фармить?"),
]
DIALOG = ["привет", "что фармишь?", "а где тут лучше осколки падают?", "ты бот что ли?", "ау"]
REPEATS = 2

out = []


def say(line: str = "") -> None:
    print(line)
    out.append(line)


# Реальные ответы модели из теста 2026-10-12 (до правок промпта) — образцы для чистки.
HUMANIZE_SAMPLES = ["фармлю мобов, а ты?", "я соло, спасибо. ты как?", "я тут первый, так что не уйду.",
                    "что за бред, я тут фармлю спокойно!", "11 осколков, фармлю потихоньку. а ты?",
                    "Нет. Сам ты бот!", "ты чё?"]


def humanize_only() -> int:
    import random
    rnd = random.Random(1)                  # фиксированное зерно — вывод одинаковый при каждом запуске
    for t in HUMANIZE_SAMPLES:
        print("%-42s -> %s" % (t, " | ".join(cb._humanize(t, rnd) for _ in range(3))))
    return 0


def main() -> int:
    if "--humanize" in sys.argv:
        return humanize_only()
    if not cb.LLM_API_KEY:
        print("нет ключа модели (llm.env / .env) — тест не запустится")
        return 1
    say("Модель: %s | сервис: %s | CLOSE_AT=%d" % (cb.LLM_MODEL, cb.LLM_BASE_URL.split("/")[2], cb.CLOSE_AT))
    say("\n=== Часть 1: каждый вопрос — новый игрок ===")
    n = 0
    for ch, q in QUESTIONS:
        say("\n> %s%s" % ("(общий чат) " if ch == "NORMAL" else "", q))
        for _ in range(REPEATS):
            n += 1
            t0 = time.perf_counter()
            r = cb.generate_reply("Тест%02d" % n, q, channel=ch, stage="chat")
            say("   %-45s  (%.1f с)" % (r or "(молчит: модель не ответила)", time.perf_counter() - t0))
    say("\n=== Часть 2: один игрок, %d сообщений подряд ===" % len(DIALOG))
    for i, q in enumerate(DIALOG, 1):
        if i > cb.CLOSE_AT:
            say("> %s\n   (игнор: уже попрощались)" % q)
            continue
        stage = "close" if i >= cb.CLOSE_AT else "chat"
        r = cb.generate_reply("Собеседник", q, channel="WHISPER", stage=stage)
        say("> %s\n   %s   [%s]" % (q, r or "(молчит: модель не ответила)", "прощание" if stage == "close" else "разговор"))
    if cb.llm_problem():
        say("\nПРОБЛЕМА: " + cb.llm_problem())
    path = os.path.join(ROOT, "tools", "chat_reply_test_result.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    print("\nсохранено: %s" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
