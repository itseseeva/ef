"""
tools/pack_release.py — готовим release\\EZF к отправке другу (2026-10-11). Запускает build_exe.bat сам.

    python tools\\pack_release.py env   -> llm.env в сборке = .env + llm.env проекта (один файл настроек)
    python tools\\pack_release.py zip   -> убирает служебный selftest.txt и пакует release\\EZF.zip
    python tools\\pack_release.py config -> ДО сборки: цепочки скиллов, сохранённые в прошлом exe
                                          (release\\EZF\\_internal\\src\\config), — в проект, чтобы новый
                                          exe (и друг) получил твою последнюю настройку

Почему python, а не команды bat: склейка через copy /b теряет перевод строки между файлами
(последняя строка .env слипается с первой строкой llm.env), а встроенный в PowerShell архиватор
спотыкается на больших файлах torch. zipfile из стандартной библиотеки умеет ZIP64 (архивы > 4 ГБ).
Ключи и прочие значения НЕ печатаются — только имена переменных.

Проверить локально: создай папку release\\EZF с парой файлов и .env/llm.env в корне, запусти
оба режима, открой архив — внутри должна быть папка EZF без selftest.txt.
"""
import os
import sys
import time
import zipfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT = os.path.join(ROOT, "release", "EZF")
ZIP = os.path.join(ROOT, "release", "EZF.zip")
SKIP = {"selftest.txt", ".env", os.path.join("_internal", "EZF_log.txt")}   # служебное: другу не идёт
# EZF_log.txt — лог самопроверки при сборке; у друга программа создаст свой при первом запуске.


def _lines(path: str) -> "list[str]":
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8-sig") as f:              # utf-8-sig: Блокнот иногда пишет BOM
        return [ln.rstrip("\r\n") for ln in f]


def _name(line: str) -> str:
    s = line.strip()
    if not s or s.startswith("#") or "=" not in s:
        return ""
    return s.split("=", 1)[0].strip().removeprefix("export ").strip()


def merge_env() -> int:
    """Один файл настроек. При совпадении имён побеждает llm.env (он новее: сервис-посредник)."""
    llm = _lines(os.path.join(ROOT, "llm.env"))
    taken = {_name(ln) for ln in llm} - {""}
    # taken — множество (set): проверка "имя уже есть?" за O(1); list comprehension сохраняет порядок строк .env
    extra = [ln for ln in _lines(os.path.join(ROOT, ".env")) if _name(ln) and _name(ln) not in taken]
    out = llm + (["", "# из .env"] + extra if extra else [])
    if not any(_name(ln) for ln in out):
        print("   нет ни llm.env, ни .env — чат-бот в exe не ответит, пока не положишь llm.env рядом с EZF.exe")
        return 0                                              # не ошибка сборки: бот без чата работает
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "llm.env"), "w", encoding="utf-8", newline="\r\n") as f:
        f.write("\n".join(out) + "\n")
    stale = os.path.join(OUT, ".env")
    if os.path.isfile(stale):
        os.remove(stale)
    names = [n for n in (_name(ln) for ln in out) if n]
    print("   llm.env: %s (значения не показываю)" % ", ".join(names))
    return 0


def take_config() -> int:
    """
    Настройки цепочек правят в программе — в exe они сохраняются ВНУТРИ сборки
    (release\\EZF\\_internal\\src\\config\\skills_config.json), а сборка берёт файл из проекта.
    Без этого шага новый exe выходил со старыми цепочками. Берём файл из сборки, если он новее
    и отличается по содержимому; старый файл проекта — в .bak (откатить руками, если что).
    """
    import json
    import shutil
    src = os.path.join(OUT, "_internal", "src", "config", "skills_config.json")
    dst = os.path.join(ROOT, "src", "config", "skills_config.json")
    if not os.path.isfile(src):
        print("   цепочки: прошлой сборки нет — беру из проекта")
        return 0

    def chains(p):
        try:
            with open(p, encoding="utf-8-sig") as f:
                d = json.load(f)
            d.pop("exportDate", None)                  # дата экспорта меняется при каждом сохранении
            return d
        except (OSError, ValueError):
            return None
    new, old = chains(src), chains(dst)
    if new is None:
        print("   цепочки: файл в прошлой сборке не читается — беру из проекта")
        return 0
    if new == old:
        print("   цепочки: в проекте те же, что в прошлой сборке")
        return 0
    if old is not None and os.path.getmtime(src) <= os.path.getmtime(dst):
        print("   цепочки: в проекте новее, чем в прошлой сборке — беру из проекта")
        return 0
    if os.path.isfile(dst):
        shutil.copy2(dst, dst + ".bak")
    shutil.copy2(src, dst)
    print("   цепочки: взял из прошлой сборки (твои последние), старые — в skills_config.json.bak")
    return 0


def make_zip() -> int:
    if not os.path.isfile(os.path.join(OUT, "EZF.exe")):
        print("   нет release\\EZF\\EZF.exe — сначала сборка")
        return 1
    for n in SKIP:
        p = os.path.join(OUT, n)
        if os.path.isfile(p):
            os.remove(p)
    t0 = time.perf_counter()
    tmp = ZIP + ".part"                     # пишем во временный файл: оборванная упаковка не оставит битый EZF.zip
    files = 0
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as z:
        for d, _, names in os.walk(OUT):
            for n in names:
                p = os.path.join(d, n)
                z.write(p, os.path.join("EZF", os.path.relpath(p, OUT)))   # в архиве — папка EZF\...
                files += 1
    os.replace(tmp, ZIP)
    size = os.path.getsize(ZIP) / 2**20
    print("   release\\EZF.zip: %d файлов, %.0f МБ, %.0f с" % (files, size, time.perf_counter() - t0))
    return 0


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    sys.exit(merge_env() if mode == "env" else make_zip() if mode == "zip"
             else take_config() if mode == "config" else 2)
