import sys
import os
import time
import pyperclip
import requests
import json
from dotenv import load_dotenv

load_dotenv()


def _load_llm_env() -> None:
    """
    llm.env рядом с программой (2026-10-11): через какой сервис чат ходит к модели. Из России
    OpenRouter отвечает "Access denied by security policy" — модель берём у российского посредника
    с тем же форматом запросов (OpenAI-совместимый API). Нет файла — всё как раньше (OpenRouter из .env).
    """
    dirs = [os.path.dirname(os.path.abspath(sys.executable))] if getattr(sys, "frozen", False) else []
    dirs += [os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")), os.getcwd()]
    for d in dirs:
        p = os.path.join(d, "llm.env")
        if os.path.isfile(p):
            load_dotenv(p, override=True)
            return


_load_llm_env()
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
LLM_BASE_URL = (os.getenv("LLM_BASE_URL") or "https://openrouter.ai/api/v1").rstrip("/")
LLM_API_KEY = os.getenv("LLM_API_KEY") or OPENROUTER_API_KEY
_IS_OPENROUTER = "openrouter.ai" in LLM_BASE_URL
import random
import re as _re
import threading

# --- LLM: скорость ---
# Session = keep-alive: TLS-рукопожатие с сервером модели один раз, а не на каждый ответ (~0.2-0.5 с).
_HTTP = requests.Session()
LLM_MODEL = os.getenv("LLM_MODEL") or "openai/gpt-4o-mini"
if not _IS_OPENROUTER and LLM_MODEL.startswith("openai/"):
    # "openai/..." — имя модели в OpenRouter. У посредников с API как у OpenAI имя без приставки.
    LLM_MODEL = LLM_MODEL[len("openai/"):]
LLM_MAX_TOKENS = 70          # потолок, а не цель: длину держит промпт. 30 обрезал ответ на полуслове
                             # (2026-10-09: "...дроп осколки, у") — русское слово = 2-4 токена
LLM_TIMEOUT = (3.0, 8.0)     # (соединение, ответ): зависли — fallback-фраза, а не ждём вечно

# --- Сценарий разговора ---
# Считаем сообщения КАЖДОГО собеседника отдельно: первые — общаемся как живой игрок,
# с N-го — сворачиваем разговор отмазкой (2026-10-09: раньше отшивал с первого же ЛС).
# Счётчик — ОДИН на ник, общий для ЛС и общего чата (просьба 2026-10-09): игрок, который пишет
# и туда и туда, — это один разговор. 1-3 сообщение — болтаем, 4-е — прощаемся, дальше игнор
# (2026-10-12: было 3 — "повысь лимит на игнор + 1 сообщение").
CLOSE_AT = 4
TALK_TTL_S = 1800.0          # 30 мин: столько длится игнор после прощания (и память разговора)

# Промпт (2026-10-12, тест tools/chat_reply_test.py): правила "пиши коротко, без запятых" модель
# выполняла через раз — писала "я соло, спасибо. ты как?". Few-shot (образцы ответов прямо в
# промпте) меняет стиль сильнее любых запретов: модель копирует МАНЕРУ примеров. Пунктуацию и
# регистр дочищает _humanize() в коде — там это работает всегда, а не "как модель решит".
BASE_PROMPT = """Ты обычный живой игрок в MMORPG Throne and Liberty, прямо сейчас фармишь мобов.
Пишешь как человек в игровом чате: очень коротко, по-русски, сленгом, без вежливостей
("спасибо", "как дела", "хорошего дня" — не пишешь). Без эмодзи. Не повторяй вопрос собеседника.
Ты не бот и не ИИ: на такие вопросы коротко отрицай, без оправданий и без возмущения.
Не выдумывай места, механики и названия игры: спросили где/как фармить — "хз" или
"тут норм падает". Не объясняй, что делаешь, если не спросили."""

# Образцы ответов (few-shot) — КАЖДЫЙ запрос получает свой случайный набор (2026-10-12): с тремя
# постоянными образцами модель отвечала ими слово в слово ("игрок как и ты" — всем подряд), а
# одинаковая фраза разным игрокам — узнаваемый шаблон. Здесь по 6-7 вариантов на тип вопроса, в
# промпт идут STYLE_PER_TOPIC случайных: стиль один, конкретные слова каждый раз другие.
# Так же устроены отмазки (EXCUSES). Добавить тип вопроса = добавить строку в список.
STYLE_EXAMPLES = [
    ("привет", ["ку", "прив", "здарова", "хай", "ку ку", "привет"]),
    ("что делаешь?", ["фармлю", "осколки бью", "мобов бью", "качаюсь", "да гриндю тут", "кач"]),
    ("можно с тобой? / го в пати", ["не я соло", "сорян один качаюсь", "не сам", "соло иду",
                                    "без пати пока", "не, один"]),
    ("это мой спот уходи", ["тут всем места хватит", "спот не подписан вроде", "ща доделаю и уйду",
                            "я раньше пришел", "место большое", "бей рядом места много"]),
    ("ты кто?", ["игрок как и ты", "а что", "никто просто фармлю", "а тебе зачем", "обычный игрок",
                 "да просто качаюсь"]),
    ("жалобу на тебя кину", ["ну кидай", "за что жалобу то", "кидай я ничего не нарушаю", "ок кидай",
                             "жалуйся за что", "ну давай"]),
    ("ты бот?", ["нет", "сам ты бот", "лол нет", "не", "нет конечно", "с чего бы"]),
    ("это скрипт? 3 часа стоишь", ["нет руками бью", "просто долго сижу", "не сам", "какой скрипт",
                                   "руками", "нет просто делать нечего"]),
    ("ты афк?", ["не тут", "тут я", "не просто занят", "тут", "отвлекся", "не, здесь"]),
    ("сколько осколков?", ["ну есть немного", "штук N", "мало пока", "N где то", "пока N"]),
    ("ты из какой гильдии?", ["без ги пока", "не в ги", "нет ги", "соло без ги", "пока без гильдии"]),
    ("где лучше фармить?", ["хз", "хз я тут сижу", "не знаю если честно", "хз сам ищу", "без понятия"]),
    ("как дела?", ["норм", "нормас", "пойдет", "норм а че", "живой"]),
]
STYLE_PER_TOPIC = 2


def _style_examples(rnd=random) -> str:
    """Блок образцов для промпта: на каждый тип вопроса — STYLE_PER_TOPIC случайных вариантов.
    rnd.sample — без повторов внутри типа; rnd подменяется в тесте (random.Random(seed))."""
    lines = ["- %s -> %s" % (q, " / ".join(rnd.sample(v, min(STYLE_PER_TOPIC, len(v)))))
             for q, v in STYLE_EXAMPLES]           # list comprehension: строка на тип, без append в цикле
    return ("Как ты обычно отвечаешь (это СТИЛЬ, а не готовые фразы — меняй слова, не склеивай два "
            "примера в один ответ; N — число из твоих фактов):\n" + "\n".join(lines))

CHAT_RULE = """Сейчас НЕ отшивай. Ответь коротко, как нормальный игрок, ТОЛЬКО на то, что спросили.
Свои факты НЕ перечисляй: спросили про осколки — ответь только про осколки. Поздоровались —
поздоровайся в ответ. Без грубостей. 1-6 слов. Встречный вопрос ("а ты?") — очень редко."""

CLOSE_RULE = """Теперь сворачивай разговор: ты занят фармом и болтать не хочешь. Если спросили — можешь
ответить в 1-3 слова, но ОБЯЗАТЕЛЬНО добавь отмазку. Одна короткая
отмазка 2-6 слов, своими словами в духе примеров (не копируй дословно и не повторяй свои
прошлые ответы). Примеры: {examples}"""

NORMAL_RULE = """Это ОБЩИЙ чат, тебя видят все рядом. Ещё короче: 1-5 слов."""

# --- "Легенда" персонажа: факты ОДНИ на всю сессию (2026-10-09: одному игроку бот сказал
# "20 осколков", следующему "15" — модель каждый раз выдумывала заново). Числа растут со временем
# фарма, как у живого игрока.
_SESSION_START = time.time()
PERSONA = {
    "lvl": 55,                              # уровень персонажа (реальный — 55)
    "weapons": "лук и посох",               # реальное оружие персонажа
    "shards0": random.randint(3, 12),       # осколков было на старте
    "shards_per_h": random.randint(10, 18),
}


def _persona_facts() -> str:
    # Сколько фармит — НЕ сообщаем (просьба игрока 2026-10-09), но осколки растут со временем.
    mins = int((time.time() - _SESSION_START) / 60) + 25
    shards = PERSONA["shards0"] + int(PERSONA["shards_per_h"] * mins / 60)
    facts = (f"Твои факты (не выдумывай другие числа): уровень {PERSONA['lvl']}, осколков сейчас {shards}, "
             f"планируешь фармить ещё часик-два. Оружие {PERSONA['weapons']} — называй его ТОЛЬКО если "
             f"прямо спросили про оружие или класс. Сколько времени уже фармишь — не говори, "
             f"отвечай расплывчато (\"давно\", \"с утра\" и т.п.).")
    # Раньше тут было "Ты уже говорил другим: <полные тексты прошлых ответов>" — модель их
    # КОПИРОВАЛА (тест 2026-10-12: на "ты бот?" ответ "за что?" из ответа про жалобу, у разных
    # игроков одинаковые фразы). Цифры и так одни на сессию — их держит PERSONA.
    return facts

# База отмазок: в промпт идут 8 случайных — модель не зацикливается на одной фразе.
EXCUSES = [
    "сори занят", "спот занят ищи другой", "я почти афк", "квесты делаю не мешай",
    "нет времени сори", "без пати я соло", "дейлики бью сори", "гоу мимо я занят",
    "не до болтовни ща", "качаюсь сори", "не отвлекай плз", "фармлю не пиши",
    "я тут чисто на опыт", "соло играю сорян", "ща контракты добью и уйду", "просто гринжу",
    "не могу занят качем", "ищу респ не мешай", "сорян не до пати", "я тут афк фармлю",
    "потом напиши", "занят пока", "позже отпишу", "ща не могу", "мобов бью потом",
    "некогда сори", "ща не до этого", "отпишу позже ок", "давай потом", "сек занят",
    "бегу на респ потом", "пати не ищу", "не в пати сорян", "ща гринд не до чата",
    "у меня контракты горят", "дроп выбиваю не отвлекай", "кд жду не до чата",
    "доделаю и отпишу", "я на фарме сори", "не щас бро", "мимо бро занят",
    "сорян чат не читаю", "ща жена позовет уйду", "работаю и фармлю одновременно сори",
    "через часик может", "не сегодня бро", "пока без меня", "у меня свои дела в игре",
    "ну занят я", "сорян молча фармлю",
]
# Запасных фраз для разговора больше НЕТ (2026-10-12): модель не ответила -> молчим. Тест показал,
# что при битом ключе бот на "ты бот?" отвечал случайным "ага" — молчание выглядит естественнее.
# Прощание (stage "close") без модели — отмазка из EXCUSES: она подходит к любому сообщению.

# Сервис модели отказал навсегда (401/403: ключ не принят, доступ закрыт) — дальше не стучимся до
# перезапуска: каждый запрос с битым ключом приближает "Too many requests". Текст — в интерфейс
# (app_launcher.get_status -> vision_warnings) и в лог.
_llm_dead: "str | None" = None
# 429 "Too many requests" — временно: пауза, потом пробуем снова.
LLM_PAUSE_ON_429_S = 30.0
_llm_pause_until = 0.0


def llm_problem() -> "str | None":
    """Почему чат-бот сейчас не может ответить (None — всё в порядке). Для интерфейса."""
    if _llm_dead:
        return _llm_dead
    if time.monotonic() < _llm_pause_until:
        return "чат-бот: сервис модели просит подождать (Too many requests) — пауза %d с" % LLM_PAUSE_ON_429_S
    return None


def _llm_error(status: int, data) -> None:
    """Разбор ошибки сервиса: навсегда (ключ/доступ) или временно (лимит). Ключ не печатаем."""
    global _llm_dead, _llm_pause_until
    detail = data.get("detail") or data.get("error") or data if isinstance(data, dict) else data
    detail = str(detail)[:160]
    low = detail.lower()
    if status in (401, 403) or "invalid api key" in low or "access denied" in low:
        if _llm_dead is None:
            _llm_dead = ("чат-бот: сервис модели не принял ключ или закрыл доступ (%s) — проверь "
                         "LLM_API_KEY в llm.env и баланс, потом перезапусти бота" % detail)
            print("[LLM] " + _llm_dead)
    elif status == 429 or "too many" in low:
        _llm_pause_until = time.monotonic() + LLM_PAUSE_ON_429_S
        print("[LLM] лимит запросов (%s) — пауза %.0f с, пока молчим" % (detail, LLM_PAUSE_ON_429_S))
    else:
        print("[LLM] Ошибка API (%s): %s" % (status, detail))

_histories: "dict[str, list]" = {}        # ник -> последние реплики разговора (ЛС и общий чат вместе)
_hist_lock = threading.Lock()             # ответы генерятся параллельно — список общий


# Хвосты-встречные вопросы, которые модель лепит почти в каждый ответ ("фармлю мобов, а ты?").
_TAIL_Q = _re.compile(r"[\s,.]*(?:а ты|ты как|а у тебя|а сам|ты чё|ты че)\s*\?*\s*$")
_TAIL_KEEP = 0.3              # такой хвост оставляем в 30% ответов — иногда живые игроки так и пишут
_COMMA_KEEP = 0.25
CLOSE_MIN_WORDS = 3           # прощание короче — дописываем отмазку из EXCUSES            # запятую оставляем в 25% случаев: в игровом чате их почти не ставят


def _humanize(reply: str, rnd=random) -> str:
    """
    Чистка ответа модели "под игровой чат" — то, что модель делает через раз, код делает всегда:
      - всё маленькими, "ё" -> "е" (в чате ё почти не пишут);
      - без точки/восклицания/многоточия в конце, "!" внутри убираем совсем;
      - запятые — в основном убираем (кроме 25%);
      - хвост "а ты?" — в 70% срезаем (если после этого что-то осталось).
    rnd — источник случайности: в тесте подставляем random.Random(seed), ответы повторяемы.
    Песочница: python tools/chat_reply_test.py --humanize (без сети, только эта функция).
    """
    s = reply.strip().lower().replace("ё", "е").replace("!", "")
    if rnd.random() >= _TAIL_KEEP:
        cut = _TAIL_Q.sub("", s)
        if len(cut.split()) >= 1:
            s = cut
    # Запятые: list comprehension по частям — каждая запятая решается отдельно.
    parts = s.split(",")
    s = "".join(p + ("," if i < len(parts) - 1 and rnd.random() < _COMMA_KEEP else "")
                for i, p in enumerate(parts))
    s = _re.sub(r"\s+", " ", s).strip()
    s = _re.sub(r"[.…]+$", "", s).strip()     # точка/многоточие в конце — так в чате не пишут
    s = _re.sub(r"\.\s+", " ", s)            # "фармлю. ты" -> "фармлю ты": одна "фраза" без точек
    return s


def generate_reply(sender: str, message: str, channel="WHISPER", stage="chat") -> "str | None":
    """
    stage: "chat" — общаемся по делу, "close" — сворачиваем разговор отмазкой.
    None — отвечать НЕ надо (модель недоступна, а для разговора запасных фраз нет): вызывающий молчит.
    """
    global _llm_dead
    fallback = random.choice(EXCUSES) if stage == "close" else None
    if not LLM_API_KEY:
        if _llm_dead is None:
            _llm_dead = "чат-бот: нет ключа модели — впиши LLM_API_KEY в llm.env рядом с программой"
            print("[LLM] " + _llm_dead)
        return fallback
    if _llm_dead is not None or time.monotonic() < _llm_pause_until:
        return fallback                        # early return: не стучимся в заведомо закрытую дверь
    rule = CHAT_RULE if stage == "chat" else CLOSE_RULE.format(examples="; ".join(random.sample(EXCUSES, 8)))
    prompt = (BASE_PROMPT + "\n\n" + _style_examples() + "\n\n" + _persona_facts() + "\n" + rule
              + ("\n" + NORMAL_RULE if channel == "NORMAL" else "")
              + "\nВся переписка выше — ОДИН разговор с этим игроком (и в ЛС, и в общем чате)."
              + " Отвечай по смыслу именно на его последнее сообщение, с учётом того, о чём уже говорили."
              + " Если в разговоре уже здоровались — НЕ здоровайся снова."
              + " Зовут фармить вместе / в пати — откажи коротко: ты соло.")
    key = sender
    # Канал помечаем в самой реплике: модель видит ОДИН разговор с игроком, где часть — в ЛС,
    # часть — в общем чате (2026-10-09: на "а можно с тобой пофармить?" в ЛС бот ответил
    # "привет, чё как?" — история ЛС была пустой, общий чат с этим ником модель не видела).
    message = ("(в ЛС) " if channel == "WHISPER" else "(в общем чате) ") + message
    with _hist_lock:
        hist = list(_histories.get(key, []))
    messages = [{"role": "system", "content": prompt}] + hist + [{"role": "user", "content": message}]
    try:
        t0 = time.perf_counter()
        body = {
            "model": LLM_MODEL,
            "messages": messages,
            "temperature": 1.0,            # 1.2 давал бессвязные ответы; 1.0 — разнообразнее 0.9 (тест 2026-10-12)
            "max_tokens": LLM_MAX_TOKENS,
        }
        if _IS_OPENROUTER:
            # OpenRouter сам выберет самого БЫСТРОГО по задержке провайдера этой модели. Другим
            # сервисам это поле не шлём: API как у OpenAI отвечает ошибкой на незнакомые поля.
            body["provider"] = {"sort": "latency"}
        r = _HTTP.post(
            LLM_BASE_URL + "/chat/completions",
            headers={"Authorization": f"Bearer {LLM_API_KEY}"},
            json=body,
            timeout=LLM_TIMEOUT,
        )
        try:
            data = r.json()
        except ValueError:
            data = r.text                          # сервис вернул не JSON (страница ошибки)
        print(f"[LLM] {sender}: ответ за {time.perf_counter() - t0:.2f} с ({stage})")
        if r.status_code != 200 or not isinstance(data, dict) or "choices" not in data:
            _llm_error(r.status_code, data)
            return fallback
        reply = data["choices"][0]["message"]["content"] or ""
        reply = reply.strip().strip('"').strip("'").replace("\n", " ").replace("\r", "")
        if not reply:
            return fallback
        if data["choices"][0].get("finish_reason") == "length":
            # Модель упёрлась в потолок токенов — обрезаем до последнего целого слова/фразы.
            cut = max(reply.rfind(","), reply.rfind(" "))
            reply = reply[:cut].rstrip(" ,") if cut > 0 else reply
        reply = _humanize(reply) or fallback
        if not reply:
            return None
        if stage == "close" and len(reply.split()) < CLOSE_MIN_WORDS:
            # Прощание без отмазки (тест 2026-10-12: на 4-м сообщении "ты бот?" -> просто "нет", а дальше
            # бот молчит — выглядит обрывом). Образец "ты бот? -> нет" сильнее правила в промпте,
            # поэтому гарантируем в коде: короткий ответ + отмазка ("нет" -> "нет сорян занят").
            reply = reply + " " + random.choice(EXCUSES)
        with _hist_lock:
            h = _histories.setdefault(key, [])
            h += [{"role": "user", "content": message}, {"role": "assistant", "content": reply}]
            del h[:-12]                        # 6 последних обменов репликами с ЭТИМ игроком
        return reply
    except Exception as e:
        print(f"[LLM] Ошибка: {e}")
        return fallback

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import cv2
import mss
import numpy as np

from src.core.chat.chat_reader import ChatReader, WHISPER, region_rect
from src.core.chat.ocr_process import make_ocr_process
from src.core.chat.chat_input import ChatInputWatch, input_rect
from src.core.input_manager import InputManager
import ctypes
import ctypes.wintypes

class KEYBDINPUT(ctypes.Structure):
    _fields_ = [('wVk', ctypes.wintypes.WORD), 
                ('wScan', ctypes.wintypes.WORD), 
                ('dwFlags', ctypes.wintypes.DWORD), 
                ('time', ctypes.wintypes.DWORD), 
                ('dwExtraInfo', ctypes.POINTER(ctypes.c_ulong))]

class _I(ctypes.Union):
    _fields_ = [('ki', KEYBDINPUT), ('pad', ctypes.c_ulong*8)]

class INPUT(ctypes.Structure):
    _anonymous_ = ('i',)
    _fields_ = [('type', ctypes.wintypes.DWORD), ('i', _I)]

import random

def type_unicode(text: str, delay: float = -1.0):
    """Печатает текст посимвольно (через WinAPI)."""
    for char in text:
        ki_down = KEYBDINPUT(0, ord(char), 0x0004, 0, None)
        inp_down = INPUT(type=1, ki=ki_down)
        ctypes.windll.user32.SendInput(1, ctypes.byref(inp_down), ctypes.sizeof(INPUT))
        time.sleep(0.01)
        
        ki_up = KEYBDINPUT(0, ord(char), 0x0004 | 0x0002, 0, None)
        inp_up = INPUT(type=1, ki=ki_up)
        ctypes.windll.user32.SendInput(1, ctypes.byref(inp_up), ctypes.sizeof(INPUT))
        
        if delay < 0:
            time.sleep(random.uniform(0.03, 0.09))
        else:
            time.sleep(delay)

def press_enter_key():
    VK_RETURN = 0x0D
    ki_down = KEYBDINPUT(VK_RETURN, 0, 0, 0, None)
    ctypes.windll.user32.SendInput(1, ctypes.byref(INPUT(type=1, ki=ki_down)), ctypes.sizeof(INPUT))
    time.sleep(0.15)
    ki_up = KEYBDINPUT(VK_RETURN, 0, 0x0002, 0, None)
    ctypes.windll.user32.SendInput(1, ctypes.byref(INPUT(type=1, ki=ki_up)), ctypes.sizeof(INPUT))
    time.sleep(0.1)

def press_esc_key():
    VK_ESCAPE = 0x1B
    ki_down = KEYBDINPUT(VK_ESCAPE, 0, 0, 0, None)
    ctypes.windll.user32.SendInput(1, ctypes.byref(INPUT(type=1, ki=ki_down)), ctypes.sizeof(INPUT))
    time.sleep(0.06)
    ki_up = KEYBDINPUT(VK_ESCAPE, 0, 0x0002, 0, None)
    ctypes.windll.user32.SendInput(1, ctypes.byref(INPUT(type=1, ki=ki_up)), ctypes.sizeof(INPUT))
    time.sleep(0.1)


def open_chat(watch) -> bool:
    """
    Enter -> ждём, что строка ввода ВИДНА открытой (золотая рамка), до 1 с; не открылась —
    ещё раз Enter. Не открылась и тогда — False: печатать НЕЛЬЗЯ, буквы ушли бы в игру как
    горячие клавиши (2026-10-09: раньше печатали вслепую по паузам).
    """
    for _ in range(2):
        press_enter_key()
        if watch is None or watch.wait(True, 1.0):
            return True
    print("[Чат-бот] строка ввода не открылась — ответ пропускаю, чтобы не печатать в игру")
    return False


def press_space_key():
    VK_SPACE = 0x20
    ki_down = KEYBDINPUT(VK_SPACE, 0, 0, 0, None)
    ctypes.windll.user32.SendInput(1, ctypes.byref(INPUT(type=1, ki=ki_down)), ctypes.sizeof(INPUT))
    time.sleep(0.05)
    ki_up = KEYBDINPUT(VK_SPACE, 0, 0x0002, 0, None)
    ctypes.windll.user32.SendInput(1, ctypes.byref(INPUT(type=1, ki=ki_up)), ctypes.sizeof(INPUT))
    time.sleep(0.1)

def press_numpad_divide(inp):
    """Нажимает аппаратную кнопку Numpad / (которая всегда печатает слэш независимо от языка)."""
    VK_DIVIDE = 0x6F
    ki_down = KEYBDINPUT(VK_DIVIDE, 0, 0, 0, None)
    ctypes.windll.user32.SendInput(1, ctypes.byref(INPUT(type=1, ki=ki_down)), ctypes.sizeof(INPUT))
    time.sleep(0.01)
    ki_up = KEYBDINPUT(VK_DIVIDE, 0, 0x0002, 0, None)
    ctypes.windll.user32.SendInput(1, ctypes.byref(INPUT(type=1, ki=ki_up)), ctypes.sizeof(INPUT))

def reply_to_chat(sender: str, message: str, inp: InputManager, watch=None):
    """Отвечает игроку эмулируя печать через команду /reply."""
    print(f"Печатаю ответ: {message}")
    
    # Нажимаем Enter, чтобы открыть чат — и ПРОВЕРЯЕМ по экрану, что строка открылась
    if not open_chat(watch):
        return
    time.sleep(0.1)
    
    # 1. Печатаем / (через Numpad Divide, чтобы обойти русскую раскладку)
    press_numpad_divide(inp)
    time.sleep(0.05)
    
    # 2. Печатаем reply (через Юникод, обходит русскую раскладку)
    type_unicode("reply")
    
    # 3. Нажимаем пробел (аппаратно!), чтобы игра переключилась в ЛС
    press_space_key()
    time.sleep(0.1) # Ждём анимацию интерфейса
    
    # 4. Печатаем само сообщение (через Юникод)
    type_unicode(message)
    
    # 5. Небольшая пауза перед отправкой
    time.sleep(0.1)
    
    # 6. Нажимаем Enter, чтобы отправить (это отправит сообщение и ЗАКРОЕТ чат)
    press_enter_key()
    time.sleep(0.1)
    
    # 7. Возврат в обычный чат (/s)
    # Открываем чат ЗАНОВО (с проверкой: не открылся — /s не печатаем, буквы ушли бы в игру)
    if watch is not None:
        watch.wait(False, 1.0)          # дождаться, что отправка закрыла строку
    if not open_chat(watch):
        return
    time.sleep(0.1)
    
    # Печатаем /s
    press_numpad_divide(inp)
    time.sleep(0.05)
    type_unicode("s")
    
    # Пробел для переключения режима (чат остаётся открытым)
    press_space_key()
    time.sleep(0.1)
    
    # Enter для закрытия пустой строки чата и возврата в игру
    press_enter_key()
    time.sleep(0.1)


def _norm_txt(t: str) -> str:
    # Сравниваем без регистра, пробелов и знаков: OCR путает "," "." и пробелы чаще, чем буквы.
    return "".join(ch for ch in t.lower() if ch.isalnum())


def _sim(a: str, b: str) -> float:
    import difflib
    return difflib.SequenceMatcher(None, _norm_txt(a), _norm_txt(b)).ratio()


import re as _re
_JUNK_TOKEN = _re.compile(r"^(?!\d+$)(?=.*[\d#@$%^&*_|\\/<>\[\]{}~`])\S{1,3}$")   # числа ("50") не трогаем


def _junk_token(t: str) -> bool:
    """
    "Слово" — мусор OCR? Ник моба/надпись с фона, попавшие в полосу строки, читаются как
    "яаав-Т_т\"", "2a-", "Fu\"о", "{4". Мусор — если: смесь латиницы и кириллицы, цифры вперемешку
    с буквами, служебные знаки внутри, или букв меньше 75% знаков. Чистая пунктуация (? ! .) — не мусор.
    """
    core = t.strip("?!.,:;()-\"'«»")
    if not core:
        return False
    if core.isdigit():
        return False
    lat = sum("a" <= ch.lower() <= "z" for ch in core)
    cyr = sum("а" <= ch.lower() <= "я" or ch.lower() == "ё" for ch in core)
    dig = sum(ch.isdigit() for ch in core)
    if lat and cyr:
        return True
    if dig and (lat or cyr):
        return True
    if any(ch in core for ch in "_{}#@$%^&*\\/<>~`=+"):
        return True
    if (lat + cyr + dig) < 0.75 * len(core):
        return True
    return len(core) == 1 and lat == 1                  # одинокая латинская буква — мусор


def clean_ocr_text(text: str) -> str:
    """
    Убрать мусор OCR перед LLM и сравнением: время мелким шрифтом ("#z", "1u"), рамки ("["),
    надписи с фона игры, попавшие в строку чата.
    Тест: clean_ocr_text("Сколько у тебя уже осколков ? . ' 2a- яаав-Т_т\" ; m '") -> "Сколько у тебя уже осколков ?"
    """
    text = text.replace("[", " ").replace("]", " ").replace("|", " ")
    toks = [t for t in text.split() if not _junk_token(t) and not _JUNK_TOKEN.match(t)]
    while toks and not any(ch.isalnum() for ch in toks[-1]) and toks[-1] not in ("?", "!", "?!"):
        toks.pop()                                      # хвост из одиночных знаков "." "'" ";"
    return " ".join(toks)


def is_own_message(text, sent_messages):
    """
    Наш же ответ, прочитанный обратно из чата (эхо). Порог 0.6, а не 0.8 (2026-10-09, "по 2
    ответа на 1"): наш шёпот в чате OCR читает с ошибками ("сори занят" -> "cори зaнят ."),
    0.8 не ловил — бот принимал своё эхо за новое сообщение собеседника и отвечал ещё раз.
    """
    a = _norm_txt(text)
    if not a:
        return True
    for sent in sent_messages:
        b = _norm_txt(sent)
        if not b:
            continue
        if _sim(a, b) > 0.6 or (len(a) >= 4 and (a in b or b in a)):
            return True
    return False


REPLIED_TTL_S = 120.0       # столько помним, на что уже ответили
REPLY_COOLDOWN_S = 8.0      # одному нику — не чаще одного ответа за столько; новое за это время копим


def _words(t: str) -> "set[str]":
    return {w for w in ("".join(ch if ch.isalnum() else " " for ch in t.lower())).split() if len(w) >= 2}


def looks_like_own(text: str, sent_messages: list) -> bool:
    """
    Жёсткая проверка "это наш ответ": кроме похожести строки (is_own_message) — совпадение
    СЛОВ: 60%+ слов прочитанного есть в одном нашем ответе. OCR может склеить наш ответ со
    временем/мусором ("9 штук сейчас. А у тебя? 2219 #z") — похожесть строки падает, а слова те же.
    """
    if is_own_message(text, sent_messages):
        return True
    w = _words(text)
    if len(w) < 2:
        return False
    return any(len(w & _words(s)) >= 0.6 * len(w) for s in sent_messages)


def seen_anywhere(text: str, replied: list, now: float) -> bool:
    """
    То же сообщение уже было — ОТ ЛЮБОГО ника и в любом канале (2026-10-09: на одно ЛС бот ответил
    дважды — в ЛС и в общий: второе прочтение пришло с другим ником/каналом, и проверка "этот ник
    это уже писал" его пропустила). Похожесть >= 0.7 за REPLIED_TTL_S — не новое.
    """
    return any(now - t <= REPLIED_TTL_S and _sim(text, old) >= 0.7 for _, old, t in replied)


def already_replied(sender: str, text: str, replied: list, now: float) -> bool:
    """
    Второй рубеж от двойных ответов: то же сообщение того же игрока, прочитанное повторно
    (закрытый чат "вспомнил" его, OCR дал другое прочтение -> читалка сочла новым).
    Похожесть >= 0.6 на уже отвеченное этим игроком за REPLIED_TTL_S — не отвечаем.
    Тест без игры: already_replied("Ник", "прив тут?", [("Ник", "Привет, тут ?", time.monotonic())], time.monotonic()) -> True
    """
    replied[:] = [r for r in replied if now - r[2] <= REPLIED_TTL_S]
    return any(s == sender and _sim(text, t) >= 0.6 for s, t, _ in replied)

def reply_to_normal_chat(message: str, inp: InputManager, watch=None):
    """Отвечает в обычный (белый) чат."""
    print(f"Печатаю ответ в обычный чат: {message}")
    
    # Нажимаем Enter, чтобы открыть чат (он уже по умолчанию в режиме /s) — с проверкой по экрану
    if not open_chat(watch):
        return
    time.sleep(0.1)
    
    # Печатаем само сообщение (через Юникод)
    type_unicode(message)
    
    # Небольшая пауза перед отправкой
    time.sleep(0.1)
    
    # Нажимаем Enter, чтобы отправить
    press_enter_key()
    time.sleep(0.1)


def chat_loop(bot_instance, stop_event=None):
    """
    stop_event (threading.Event) — выключатель "Чат-бот" в программе: set() -> цикл выходит на
    следующем опросе (~0.12 с). Без него чат живёт, пока работает бот (bot_instance._running).
    """
    ocr = None
    try:
        print("Инициализация OCR для чат-бота...")
        # OCR — в отдельном процессе (src/core/chat/ocr_process.py): не отнимает процессор у боя.
        ocr = make_ocr_process()
        if ocr is None:
            print("Ошибка: OCR не инициализирован. Чат-бот не запущен.")
            return
            
        from src.core.chat.chat_reader import NORMAL
        reader = ChatReader(ocr)
        
        # Используем тот же InputManager, что и основной бот, если он есть
        inp = getattr(bot_instance, "input_manager", InputManager())
        
        print("Чат-бот запущен в фоновом потоке. Жду сообщений...")
    
        sent_messages = []
        replied = []                    # (ник, текст, время) — на что уже ответили
        pending = {}                    # ник -> {"ch": канал, "texts": [...]} — ждут ответа (кулдаун ника)
        cooldown_until = {}             # ник -> раньше этого времени ему не отвечаем
        known_senders = []              # ники, которые уже видели (для склейки "Shinka" / "Shinka4")

        def canon(sender: str) -> str:
            # OCR читает один ник по-разному (значок рядом с ником в ЛС -> "Shinka4"). Похожий на
            # известный (>= 0.75 или начало совпадает) — это он; иначе ник — разные собеседники
            # и защита "уже ответили этому нику" не срабатывает.
            for k in known_senders:
                if _sim(sender, k) >= 0.75 or (min(len(k), len(sender)) >= 4
                                               and (k.lower().startswith(sender.lower())
                                                    or sender.lower().startswith(k.lower()))):
                    return k
            known_senders.append(sender)
            return sender
        talks = {}                      # ник -> [сколько сообщений написал (ЛС+общий), когда последнее]
        # Свой ник: из .env (MY_NICK=IceQueen) или узнаём по первому эху в общем чате.
        own_names = {n for n in [os.getenv("MY_NICK", "").strip()] if n}
        from concurrent.futures import ThreadPoolExecutor
        llm_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="llm")
        
        with mss.mss() as sct:
            mon = sct.monitors[1]
            roi = region_rect(mon["left"], mon["top"], mon["width"], mon["height"])
            iroi = input_rect(mon["left"], mon["top"], mon["width"], mon["height"])
            # Строка ввода чата открыта? — по золотой рамке поля (src/core/chat/chat_input.py)
            watch = ChatInputWatch(lambda: np.array(sct.grab(iroi))[..., :3], mon["width"])
            
            while getattr(bot_instance, "_running", True) and not (stop_event is not None and stop_event.is_set()):
                frame = np.array(sct.grab(roi))[..., :3]
                t0 = time.perf_counter()
                
                messages = reader.poll(frame)
                t_ocr = time.perf_counter() - t0
                if t_ocr > 0.5:
                    n_ln, n_ocr, s_ocr = getattr(reader, "last_stats", (0, 0, 0.0))
                    print(f"[Чат-бот] ВНИМАНИЕ: чтение чата {t_ocr:.2f} с — строк {n_ln}, "
                          f"через OCR {n_ocr} ({s_ocr / max(1, n_ocr):.2f} с на строку)")
                
                for m in messages:
                    print(f"DEBUG OCR NEW MSG: sender={m.sender}, text={m.text}, channel={m.channel}")
                    if not m.sender:
                        continue
                    m_sender = m.sender if m.sender in own_names else canon(m.sender)
                    m_text = clean_ocr_text(m.text)
                    if len(_words(m_text)) == 0:
                        print("  -> пропуск: мусор OCR, слов нет")
                        continue
                        
                    # Игнорируем эхо своих ответов и уже отвеченное (повторное прочтение)
                    # Сообщения от НАШЕГО персонажа — никогда не отвечаем (2026-10-09: строка-перенос
                    # нашего же ответа "...А у / тебя?" прочиталась мусором "7.атюти" под ником
                    # IceQueen, эхо не узнали — бот ответил сам себе). Свой ник бот узнаёт сам:
                    # чей ник стоит над нашим эхом в общем чате — тот и наш.
                    if m_sender in own_names:
                        print(f"  -> пропуск: это пишет наш персонаж ({m_sender})")
                        continue
                    if looks_like_own(m_text, sent_messages):
                        print("  -> пропуск: это наш же ответ (эхо)")
                        if m.channel == NORMAL and m_sender not in own_names:
                            own_names.add(m_sender)
                            print(f"[Чат-бот] запомнил свой ник: {m_sender}")
                        continue
                    now_m = time.monotonic()
                    if seen_anywhere(m_text, replied, now_m) or any(
                            _sim(m_text, t) >= 0.7 for p in pending.values() for t in p["texts"]):
                        print("  -> пропуск: это сообщение уже было (повторное прочтение)")
                        continue
                    replied.append((m_sender, m_text, now_m))

                    # Попрощались с этим ником (CLOSE_AT-е его сообщение, сейчас 4-е, в любом канале) — чистый игнор
                    # TALK_TTL_S (30 мин) и в ЛС, и в общем чате.
                    tk = talks.get(m_sender)
                    if tk is not None and tk[0] >= CLOSE_AT and time.monotonic() - tk[1] <= TALK_TTL_S:
                        print(f"  -> пропуск: с {m_sender} уже попрощались (игнор)")
                        continue

                    # В очередь ника. Писал и в ЛС, и в общий — отвечаем ОДИН раз, в ЛС.
                    p = pending.setdefault(m_sender, {"ch": m.channel, "texts": []})
                    if m.channel == WHISPER:
                        p["ch"] = WHISPER
                    p["texts"].append(m_text)

                # Отвечаем тем, у кого прошёл кулдаун: одному нику — один ответ за раз.
                whispers_by_sender = {}
                normal_by_sender = {}
                now_r = time.monotonic()
                for snd in list(pending):
                    if now_r < cooldown_until.get(snd, 0.0):
                        continue
                    p = pending.pop(snd)
                    (whispers_by_sender if p["ch"] == WHISPER else normal_by_sender)[snd] = p["texts"]
                    cooldown_until[snd] = now_r + REPLY_COOLDOWN_S

                has_messages = bool(whispers_by_sender) or bool(normal_by_sender)
                
                if has_messages:
                    # Сначала генерируем ответы (бот продолжает фармить)
                    # Запросы к LLM — ПАРАЛЛЕЛЬНО (пул потоков): 3 собеседника = ~1 с ожидания, а не ~3.
                    # Сеть — это ожидание, а не работа процессора, поэтому потоки тут дают выигрыш даже с GIL.
                    t_detect = time.perf_counter()

                    def _stage(sender, n_new):
                        # Один счётчик на ник (ЛС + общий чат). Тишина дольше TALK_TTL_S — с нуля.
                        now_t = time.monotonic()
                        c = talks.get(sender)
                        if c is None or now_t - c[1] > TALK_TTL_S:
                            c = [0, now_t]
                        c[0] += n_new
                        c[1] = now_t
                        talks[sender] = c
                        return ("close" if c[0] >= CLOSE_AT else "chat"), c[0]

                    w_jobs = []
                    for sender, texts in whispers_by_sender.items():
                        stage, n = _stage(sender, len(texts))
                        print(f"\n[Чат-бот] ЛС от {sender} (сообщение №{n}, {stage}). Генерирую ответ...")
                        w_jobs.append((sender, llm_pool.submit(generate_reply, sender, " ".join(texts), "WHISPER", stage)))
                    n_jobs = []
                    for sender, texts in normal_by_sender.items():
                        if talks.get(sender, [0])[0] >= CLOSE_AT:
                            continue                  # в этом же опросе уже попрощались в ЛС
                        stage, n = _stage(sender, len(texts))
                        print(f"\n[Чат-бот] Общий чат от {sender} (сообщение №{n}, {stage}). Генерирую ответ...")
                        n_jobs.append((sender, llm_pool.submit(generate_reply, sender, " ".join(texts), "NORMAL", stage)))
                    # None — модель не ответила: этому игроку молчим. Моржовый оператор := (Python 3.8+)
                    # берёт результат и проверяет его в одном выражении, без второго вызова f.result().
                    whisper_replies = [(snd, r) for snd, f in w_jobs if (r := f.result())]
                    normal_replies = [(snd, r) for snd, f in n_jobs if (r := f.result())]
                    if not whisper_replies and not normal_replies:
                        print("[Чат-бот] модель не ответила — молчим, фарм не прерываю")
                    else:
                        print(f"[Чат-бот] ответы от LLM за {time.perf_counter() - t_detect:.2f} с")
                        # Свои ответы запоминаем ДО печати: эхо может прочитаться, пока печатаем следующий.
                        for _, reply_text in whisper_replies + normal_replies:
                            sent_messages.append(reply_text)
                        del sent_messages[:-20]

                        # Ответы сгенерированы! Теперь ставим паузу, чтобы напечатать их
                        print("[Чат-бот] Ответы готовы. Ставлю бота на паузу для печати в чат...")
                        bot_instance._paused_by_chat = True
                        # Без sleep(0.5): set_chat_lock сам дожидается, пока поток ввода доделает
                        # текущее действие, и отпускает все клавиши — ждать "на всякий случай" не нужно.
                        if hasattr(inp, 'set_chat_lock'):
                            inp.set_chat_lock(True)
                        else:
                            inp.cancel_pending_actions()
                        time.sleep(random.uniform(0.04, 0.09))
                    
                        try:
                            for sender, reply_text in whisper_replies:
                                reply_to_chat(sender, reply_text, inp, watch)
                                time.sleep(random.uniform(0.25, 0.45))   # было 1.0; рандом — не ровный ритм
                            
                            for sender, reply_text in normal_replies:
                                reply_to_normal_chat(reply_text, inp, watch)
                                time.sleep(random.uniform(0.25, 0.45))
                        finally:
                            # ГЛАВНОЕ: бой — только когда строка ввода ТОЧНО закрыта. Esc — только если она
                            # видна открытой (Esc при закрытом чате открыл бы меню игры). Не закрылась —
                            # держим замок и пробуем ещё: лучше постоять секунду, чем печатать скиллы в чат.
                            for _ in range(10):
                                if watch.ensure_closed(press_esc_key):
                                    break
                                print("[Чат-бот] строка чата не закрывается — жду и пробую снова")
                                time.sleep(0.3)
                            print("[Чат-бот] Все ответы отправлены. Снимаю паузу, продолжаем фарм!\\n")
                            if hasattr(inp, 'set_chat_lock'):
                                inp.set_chat_lock(False)
                            bot_instance._paused_by_chat = False
            

                dt = time.perf_counter() - t0
                time.sleep(max(0.03, 0.12 - dt))     # опрос 0.12 с: сторож дешёвый, OCR — только новых строк
    except Exception as e:
        import traceback
        print(f"\\n[ЧАТ-БОТ КРИТИЧЕСКАЯ ОШИБКА] {e}\\n{traceback.format_exc()}")
    finally:
        # Закрыть процесс-распознаватель вместе с чат-ботом (иначе висел бы в диспетчере задач).
        if ocr is not None and hasattr(ocr, "close"):
            ocr.close()


def start_chat_thread(bot_instance, stop_event=None):
    import threading
    t = threading.Thread(target=chat_loop, args=(bot_instance, stop_event), daemon=True, name="ChatBot")
    t.start()
    return t

if __name__ == "__main__":
    class DummyBot:
        _running = True
        _paused_by_chat = False
        input_manager = InputManager()
    
    dbot = DummyBot()
    start_chat_thread(dbot)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        dbot._running = False