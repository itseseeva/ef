"""
ezf_main.py — точка входа EZF.exe (PyInstaller, 2026-10-11). Из python по-прежнему: python app_launcher.py.

Один exe — три режима (ключом командной строки):
    EZF.exe                  -> обычная программа (окно, бот, чат)
    EZF.exe --ocr-worker     -> распознаватель текста для чата (его запускает сама программа,
                                см. src/core/chat/ocr_process.py: отдельного python в exe нет)
    EZF.exe --selftest       -> самопроверка собранного exe (tools/exe_selftest.py), окно не открывается

Почему не прямо app_launcher.py: распознавателю не нужны окно, бот и pywebview — он грузит только
EasyOCR и не тратит секунды и память на всё остальное.
"""
import os
import sys


def _app_dir() -> str:
    """Папка с EZF.exe (рядом с ней кладут .env). Из python — папка проекта."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _load_env() -> None:
    """Настройки рядом с exe: llm.env (ключ, модель, ник — сборка склеивает туда и старый .env).
    .env тоже читаем — для запуска из проекта, где файлы пока раздельные.
    Уже заданные переменные окружения не перетираем (override=False): так их можно подменить для теста."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    for name in ("llm.env", ".env"):
        for d in (_app_dir(), os.getcwd()):
            p = os.path.join(d, name)
            if os.path.isfile(p):
                load_dotenv(p, override=False)
                break


def _redirect_to_log() -> None:
    """exe собран без консоли (EZF.spec: console=False) — print и ошибки уходили бы в никуда
    (sys.stdout/sys.stderr = None), а traceback.print_exc() на None вообще падает.
    Направляем их в _internal\\EZF_log.txt: файл перезаписывается при каждом запуске, поэтому
    не растёт; если что-то сломалось у друга — он присылает этот файл.
    line buffering (buffering=1): каждая строка сразу на диске — лог не теряется при os._exit
    или падении процесса. Из python (есть консоль) — ничего не меняем.
    Проверить: собрать exe, запустить, открыть _internal\\EZF_log.txt — там строки запуска бота."""
    if not getattr(sys, "frozen", False) or (sys.stdout is not None and sys.stderr is not None):
        return
    try:
        base = getattr(sys, "_MEIPASS", None) or _app_dir()
        log = open(os.path.join(base, "EZF_log.txt"), "w", encoding="utf-8", errors="replace", buffering=1)
    except OSError:
        return                                   # папка только для чтения — живём без лога
    if sys.stdout is None:
        sys.stdout = log
    if sys.stderr is None:
        sys.stderr = log


def _std_pipes() -> None:
    """Режим --ocr-worker в exe без консоли: каналы обмена — pipe, которые даёт программа-родитель
    (ocr_process.py: stdin=PIPE, stdout=PIPE). Обычно Python сам видит их как sys.stdin/stdout.
    Страховка на случай, если он их не подхватил (оставил None): берём те же pipe напрямую у Windows
    (GetStdHandle) и оборачиваем в файлы. Без этого распознаватель упал бы, и чат остался бы без OCR."""
    if sys.stdin is not None and sys.stdout is not None:
        return
    try:
        import ctypes
        import msvcrt
        k32 = ctypes.windll.kernel32
        k32.GetStdHandle.restype = ctypes.c_void_p           # HANDLE — указатель, не int (иначе 64-бит обрежется)
        for attr, std_id, mode in (("stdin", -10, "rb"), ("stdout", -11, "wb")):
            if getattr(sys, attr) is None:
                fd = msvcrt.open_osfhandle(k32.GetStdHandle(std_id), 0)
                setattr(sys, attr, open(fd, mode.replace("b", ""), encoding="utf-8", closefd=False))
    except Exception:
        pass                                     # не вышло — _worker сообщит об ошибке, чат пойдёт без OCR


# Папки с .NET-библиотеками внутри _internal: pythonnet (мост Python <-> .NET), clr_loader (его загрузчик),
# webview (WinForms/WebView2 — само окно программы). Остальные DLL обычные (не .NET) — метка им не мешает.
_DOTNET_DIRS = ("pythonnet", "clr_loader", "webview")


def _unblock_dotnet() -> int:
    """Снимаем с .NET-библиотек метку "скачано из интернета" (Mark of the Web).
    Зачем: архив, присланный через Telegram/браузер, Windows помечает, и "Извлечь все" переносит метку
    на каждый файл. Старый .NET (Framework), через который работает окно pywebview, такие сборки
    грузить отказывается -> "Failed to resolve Python.Runtime.Loader.Initialize" и окно не открывается.
    Метка — это скрытый поток NTFS "<файл>:Zone.Identifier"; удаляем его — ровно то же делает галочка
    "Разблокировать" в свойствах файла. Делаем ДО import webview: после загрузки сборки уже поздно.
    Скорость: только 3 папки (~десятки файлов), не все тысячи из _internal — запуск не замедляется.
    Проверить без друга: скачай свой EZF.zip через браузер (или в PowerShell:
    Set-Content -Path _internal\\pythonnet\\runtime\\Python.Runtime.dll -Stream Zone.Identifier
    -Value "[ZoneTransfer]`nZoneId=3"), распакуй, запусти — окно должно открыться, в EZF_log.txt
    строка "снял метку интернета: N файлов"."""
    base = getattr(sys, "_MEIPASS", None)
    if not getattr(sys, "frozen", False) or not base:
        return 0
    removed = 0
    for sub in _DOTNET_DIRS:
        for d, _, names in os.walk(os.path.join(base, sub)):
            for n in names:
                try:
                    os.remove(os.path.join(d, n) + ":Zone.Identifier")
                    removed += 1
                except OSError:
                    pass          # метки нет (обычный случай) или диск без потоков (FAT32) — нечего снимать
    if removed:
        print("EZF: снял метку интернета: %d файлов (.NET-библиотеки окна)" % removed, flush=True)
    return removed


def _hide_internal() -> None:
    """Прячем служебную папку _internal (атрибут "скрытый"): у друга в папке видны только EZF.exe и
    llm.env. Делаем при запуске, а не при сборке — zip-архив атрибут "скрытый" не сохраняет.
    Ошибка тут ни на что не влияет (например, папка на флешке без атрибутов) — молча пропускаем."""
    if not getattr(sys, "frozen", False):
        return
    try:
        import ctypes
        path = os.path.join(_app_dir(), "_internal")
        k32 = ctypes.windll.kernel32
        attrs = k32.GetFileAttributesW(path)
        FILE_ATTRIBUTE_HIDDEN = 0x2
        if attrs != -1 and attrs != 0xFFFFFFFF and not attrs & FILE_ATTRIBUTE_HIDDEN:
            k32.SetFileAttributesW(path, attrs | FILE_ATTRIBUTE_HIDDEN)
    except Exception:
        pass


def main() -> int:
    if "--ocr-worker" in sys.argv:
        _std_pipes()
        from src.core.chat.ocr_process import _worker
        _worker()
        return 0
    _redirect_to_log()                        # до всего остального: ошибки импорта тоже попадут в лог
    _unblock_dotnet()                         # до import webview — иначе окно не откроется у скачавших архив
    _load_env()
    if "--selftest" in sys.argv:
        from tools.exe_selftest import main as selftest
        return selftest()
    # Двойной клик по exe: рабочая папка = папка exe (логи и относительные пути — рядом с программой).
    if getattr(sys, "frozen", False):
        os.chdir(_app_dir())
        _hide_internal()
    import app_launcher
    app_launcher.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
