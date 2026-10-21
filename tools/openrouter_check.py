"""
tools/openrouter_check.py — работает ли чат-бот (OpenRouter) с этого компьютера БЕЗ VPN (2026-10-11).

Как пользоваться: ВЫКЛЮЧИ VPN, потом
    python tools\\openrouter_check.py          (или двойной клик check_openrouter.bat)
Ключ берётся из .env (OPENROUTER_API_KEY) и в вывод НЕ печатается.

Что проверяет, по шагам — чтобы было видно, ГДЕ рвётся:
  1. DNS: находит ли компьютер адрес openrouter.ai (блокировки часто ломают уже здесь);
  2. соединение: открывается ли защищённое соединение (TLS) с сервером;
  3. API без ключа: отвечает ли OpenRouter вообще (список моделей);
  4. тестовое сообщение с ключом в модель чат-бота (openai/gpt-4o-mini);
  5. если модель "недоступна в регионе" — пробует запасные модели и говорит, на какую переключить чат.
Ничего не меняет в программе. Тестовое сообщение стоит доли цента.
"""
import json
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
HOST = "openrouter.ai"
API = "https://openrouter.ai/api/v1"
MAIN_MODEL = "openai/gpt-4o-mini"            # как в tools/chat_bot_test.py (LLM_MODEL)
SERVICE = "OpenRouter"


def _env_file(name: str) -> dict:
    """Строки КЛЮЧ=значение из файла рядом с проектом/exe (без python-dotenv)."""
    dirs = [os.path.dirname(os.path.abspath(sys.executable))] if getattr(sys, "frozen", False) else [ROOT]
    for d in dirs + [os.getcwd()]:
        p = os.path.join(d, name)
        if os.path.isfile(p):
            out = {}
            with open(p, encoding="utf-8-sig") as f:
                for line in f:
                    if line.strip().startswith("#"):
                        continue
                    k, sep, v = line.strip().partition("=")
                    if sep:
                        out[k.strip().removeprefix("export ").strip()] = v.strip().strip('"').strip("'")
            return out
    return {}


def apply_llm_env() -> None:
    """llm.env (как у чат-бота): другой сервис вместо OpenRouter — проверяем именно его."""
    global HOST, API, MAIN_MODEL, SERVICE
    cfg = _env_file("llm.env")
    if not cfg.get("LLM_BASE_URL"):
        return
    from urllib.parse import urlparse
    API = cfg["LLM_BASE_URL"].rstrip("/")
    HOST = urlparse(API).hostname or HOST
    MAIN_MODEL = cfg.get("LLM_MODEL") or MAIN_MODEL
    if "openrouter.ai" not in API and MAIN_MODEL.startswith("openai/"):
        MAIN_MODEL = MAIN_MODEL[len("openai/"):]      # как в чат-боте: у посредников имя без приставки
    SERVICE = HOST
    if cfg.get("LLM_API_KEY"):
        os.environ["LLM_API_KEY"] = cfg["LLM_API_KEY"]
# Запасные — у разных поставщиков разные правила по регионам; берём те, что есть в списке моделей.
SPARE_MODELS = ["google/gemini-2.0-flash-001", "deepseek/deepseek-chat", "deepseek/deepseek-chat-v3-0324",
                "meta-llama/llama-3.3-70b-instruct", "qwen/qwen-2.5-72b-instruct", "mistralai/mistral-small-3.1-24b-instruct"]
TIMEOUT = 15


def say(ok, text):
    print(("  OK    " if ok is True else "  FAIL  " if ok is False else "  ..    ") + text, flush=True)


def load_key() -> str:
    """Ключ: из llm.env (если проверяем другой сервис) или OPENROUTER_API_KEY из окружения/.env."""
    if SERVICE != "OpenRouter":
        return os.environ.get("LLM_API_KEY", "").strip()
    k = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if k:
        return k
    dirs = [os.path.dirname(os.path.abspath(sys.executable))] if getattr(sys, "frozen", False) else [ROOT]
    for d in dirs + [os.getcwd()]:
        p = os.path.join(d, ".env")
        if not os.path.isfile(p):
            continue
        with open(p, encoding="utf-8-sig") as f:
            for line in f:
                name, sep, val = line.strip().partition("=")
                if sep and name.strip().removeprefix("export ").strip() == "OPENROUTER_API_KEY":
                    return val.strip().strip('"').strip("'")
    return ""


def step_dns():
    t = time.perf_counter()
    ips = sorted({a[4][0] for a in socket.getaddrinfo(HOST, 443, proto=socket.IPPROTO_TCP)})
    say(True, "DNS: %s -> %s (%.0f мс)" % (HOST, ", ".join(ips[:3]), (time.perf_counter() - t) * 1000))


def step_tls():
    t = time.perf_counter()
    ctx = _ctx()
    with socket.create_connection((HOST, 443), timeout=TIMEOUT) as s:
        with ctx.wrap_socket(s, server_hostname=HOST) as tls:
            v = tls.version()
    say(True, "Соединение: TLS %s с %s (%.0f мс)" % (v, HOST, (time.perf_counter() - t) * 1000))


def _ctx():
    ctx = ssl.create_default_context()
    try:
        import certifi                                   # если есть — тот же набор сертификатов, что у requests
        ctx.load_verify_locations(certifi.where())
    except Exception:
        pass
    return ctx


UA = {"requests": "python-requests/2.32.3",          # как у чат-бота (он на requests)
      "браузер": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36",
      "urllib": "Python-urllib/3"}
USE_UA = "requests"


def http(method: str, path: str, key: str = "", body: "dict | None" = None, ua: "str | None" = None) -> "tuple[int, dict | str]":
    """Запрос к API только встроенными средствами Python (urllib) — без пакета requests."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", UA[ua or USE_UA])
    if key:
        req.add_header("Authorization", "Bearer " + key)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=_ctx()) as r:
            raw, code = r.read(), r.status
    except urllib.error.HTTPError as e:
        raw, code = e.read(), e.code
    try:
        return code, json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return code, raw.decode("utf-8", "replace")[:300]


def step_models(sess=None, key: str = "") -> "set[str]":
    """Список моделей. 403 — пробуем разными "представлениями" программы: так видно, блок по стране
    (403 у всех) или фильтр по типу программы (у одного 403, у другого нет)."""
    global USE_UA
    t = time.perf_counter()
    tried = []
    for ua in ("requests", "браузер", "urllib"):
        code, body = http("GET", "/models", key=key, ua=ua)
        tried.append("%s: HTTP %d" % (ua, code))
        if code == 200 and isinstance(body, dict):
            USE_UA = ua
            break
    else:
        raise RuntimeError("%s; ответ: %s" % ("; ".join(tried), str(body)[:150]))
    ids = {m.get("id") for m in body.get("data", [])}
    say(True, "%s отвечает: моделей в списке %d (%.0f мс)" % (SERVICE, len(ids), (time.perf_counter() - t) * 1000))
    return ids


def ask(sess, key, model) -> "tuple[bool, str]":
    t = time.perf_counter()
    code, body = http("POST", "/chat/completions", key, {"model": model, "max_tokens": 20,
                      "messages": [{"role": "user", "content": "Ответь одним словом: привет"}]})
    dt = (time.perf_counter() - t) * 1000
    if not isinstance(body, dict):
        body = {"error": {"message": body}}
    if code == 200 and body.get("choices"):
        text = (body["choices"][0].get("message") or {}).get("content", "").strip()
        return True, "%s ответила за %.0f мс: %r" % (model, dt, text[:60])
    err = (body.get("error") or {})
    msg = str(err.get("message") or body)[:300]
    return False, "%s: HTTP %d — %s" % (model, code, msg)


def main() -> int:
    apply_llm_env()
    print("Проверка чат-бота: сервис %s, модель %s. VPN должен быть ВЫКЛЮЧЕН.\n" % (SERVICE, MAIN_MODEL))
    for v in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
        if os.environ.get(v):
            say(None, "в системе задан прокси %s — проверка пойдёт через него, а не напрямую" % v)
    sess = None                                          # только встроенный urllib: работает в любом Python

    try:
        step_dns()
    except Exception as e:
        say(False, "DNS: адрес %s не находится (%s) — похоже на блокировку на уровне DNS." % (HOST, e))
        return 1
    try:
        step_tls()
    except Exception as e:
        say(False, "Соединение с %s не открывается (%s: %s) — сайт блокируется." % (HOST, type(e).__name__, e))
        return 1
    key = load_key()
    if not key:
        say(False, "нет ключа (%s) — сеть в порядке, но сообщение отправить нечем"
            % ("LLM_API_KEY в llm.env" if SERVICE != "OpenRouter" else "OPENROUTER_API_KEY в .env"))
        return 1
    say(True, "ключ найден (%d символов, сам ключ не показываю)" % len(key))
    ids: "set[str]" = set()
    try:
        ids = step_models(sess, key)
    except Exception as e:
        if SERVICE == "OpenRouter":
            say(False, "OpenRouter отказывает (%s)" % e)
            if "403" in str(e):
                print("\nИТОГ: сеть до OpenRouter есть, но OpenRouter ОТКАЗЫВАЕТ этому подключению ('Access denied by\n"
                      "      security policy') — так он отвечает на запросы из некоторых стран. Без VPN/прокси чат\n"
                      "      через OpenRouter работать не будет.")
            return 1
        say(None, "список моделей не получен (%s) — не страшно, проверяю сообщением" % str(e)[:120])

    ok, info = ask(sess, key, MAIN_MODEL)
    say(ok, "Тестовое сообщение: " + info)
    if ok:
        print("\nИТОГ: чат-бот БУДЕТ работать без VPN (%s, модель %s)." % (SERVICE, MAIN_MODEL))
        return 0

    low = info.lower()
    where = "llm.env" if SERVICE != "OpenRouter" else ".env"
    if "401" in info or "user not found" in low or "invalid" in low and "key" in low:
        print("\nИТОГ: сеть работает, но %s не принял ключ — проверь ключ в %s (он точно с этого сайта?)." % (SERVICE, where))
        return 1
    if "402" in info or "credit" in low or "balance" in low or "баланс" in low:
        print("\nИТОГ: сеть работает, но на балансе %s нет денег — пополни." % SERVICE)
        return 1
    if SERVICE != "OpenRouter":
        print("\nИТОГ: %s ответил ошибкой (текст выше). Пришли этот текст — разберу (обычно имя модели или адрес)." % SERVICE)
        return 1
    print("\n  Основная модель не ответила — пробую запасные (часть поставщиков закрыта для некоторых стран):")
    good = []
    for m in SPARE_MODELS:
        if ids and m not in ids:
            continue
        try:
            ok2, info2 = ask(sess, key, m)
        except Exception as e:
            ok2, info2 = False, "%s: %s" % (m, e)
        say(ok2, info2)
        if ok2:
            good.append(m)
    if good:
        print("\nИТОГ: OpenRouter без VPN доступен, но %s — нет. Работают: %s.\n"
              "      Напиши мне — переключу чат-бота на %s (одна строка LLM_MODEL)." % (MAIN_MODEL, ", ".join(good), good[0]))
    else:
        print("\nИТОГ: без VPN не ответила ни одна модель — для чата нужен VPN (или другой доступ). Пришли этот текст.")
    return 1


if __name__ == "__main__":
    code = main()
    if os.name == "nt" and not os.environ.get("EZF_NO_PAUSE"):
        input("\nНажми Enter, чтобы закрыть...")
    sys.exit(code)
