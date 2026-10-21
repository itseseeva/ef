"""
Окно игры: найти и вывести на передний план (2026-10-11).

Зачем: кнопка "Снять скиллы" фотографирует экран. Раньше она просто сворачивала окно программы
"в надежде", что под ним игра, — а под ним мог быть редактор кода, и панель скиллов не находилась.
Теперь программа сама находит окно игры и выводит его вперёд.

Как узнаём игру — так же, как tools/record_movement.py: по процессу TL.exe (надёжно), а если
процесс не прочитался (игра запущена от админа, а мы нет) — по заголовку окна. Своё окно
("EasyFarm — Throne and Liberty") исключаем по номеру процесса: в заголовке тоже есть название игры.

Ничего не нажимает: только Win32-вызовы окон (как Alt+Tab мышкой), без эмуляции клавиш.

Как проверить без игры: python game_window_test.py (фейковый список окон, Linux тоже).
Живая проверка: python -c "from src.core.game_window import focus_game; print(focus_game())"
"""
from __future__ import annotations

import ntpath
import os
import time

GAME_EXES = ("tl.exe",)                          # процесс игры (сравнение по имени файла)
GAME_TITLES = ("throne and liberty", "tl")       # или заголовок окна (полное совпадение / начало + пробел)


def is_game_exe(exe: str) -> bool:
    # ntpath, а не os.path: путь всегда виндовый (C:\...\TL.exe), а тесты гоняются и на Linux
    return bool(exe) and ntpath.basename(exe).lower() in GAME_EXES


def is_game_title(title: str) -> bool:
    t = (title or "").strip().lower()
    return any(t == g or t.startswith(g + " ") for g in GAME_TITLES)


def pick_game_window(windows, own_pid: int) -> "int | None":
    """
    Из списка окон [(hwnd, title, exe, pid, площадь)] — окно игры. Сначала по процессу TL.exe,
    потом по заголовку; среди подходящих — самое большое (у игры бывают служебные мелкие окна).
    Чистая функция без Win32 — поэтому тестируется на фейковом списке.
    """
    cands = [w for w in windows if w[3] != own_pid and w[4] > 0]
    by_exe = [w for w in cands if is_game_exe(w[2])]
    pool = by_exe or [w for w in cands if is_game_title(w[1])]
    if not pool:
        return None
    return max(pool, key=lambda w: w[4])[0]


# ---------------- Win32 ----------------

def _user32():
    import ctypes
    from ctypes import wintypes
    u = ctypes.WinDLL("user32", use_last_error=True)
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    # argtypes/restype обязательны: без них ctypes обрежет 64-битный HWND до int
    u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    u.GetWindowThreadProcessId.restype = wintypes.DWORD
    u.IsWindowVisible.argtypes = [wintypes.HWND]
    u.IsIconic.argtypes = [wintypes.HWND]
    u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    u.SetForegroundWindow.argtypes = [wintypes.HWND]
    u.BringWindowToTop.argtypes = [wintypes.HWND]
    u.GetForegroundWindow.restype = wintypes.HWND
    u.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.OpenProcess.restype = wintypes.HANDLE
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                             ctypes.POINTER(wintypes.DWORD)]
    k.GetCurrentThreadId.restype = wintypes.DWORD
    return ctypes, wintypes, u, k


def list_windows() -> "list[tuple[int, str, str, int, int]]":
    """Видимые окна верхнего уровня: (hwnd, заголовок, путь exe, pid, площадь). Свёрнутые — тоже
    (площадь берём 1, чтобы окно не выпало из выбора: свёрнутую игру мы как раз и разворачиваем)."""
    ctypes, wintypes, u, k = _user32()
    out = []
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(h, _lp):
        if not u.IsWindowVisible(h):
            return True
        buf = ctypes.create_unicode_buffer(256)
        u.GetWindowTextW(h, buf, 256)
        pid = wintypes.DWORD()
        u.GetWindowThreadProcessId(h, ctypes.byref(pid))
        exe = ""
        hp = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if hp:
            pbuf = ctypes.create_unicode_buffer(520)
            size = wintypes.DWORD(520)
            if k.QueryFullProcessImageNameW(hp, 0, pbuf, ctypes.byref(size)):
                exe = pbuf.value
            k.CloseHandle(hp)
        r = wintypes.RECT()
        u.GetWindowRect(h, ctypes.byref(r))
        area = max(0, r.right - r.left) * max(0, r.bottom - r.top)
        out.append((int(h), buf.value, exe, int(pid.value), area if not u.IsIconic(h) else max(area, 1)))
        return True

    u.EnumWindows(_cb, 0)                        # _cb живёт до конца функции — сборщик мусора его не тронет
    return out


def find_game_window() -> "int | None":
    return pick_game_window(list_windows(), os.getpid())


def bring_to_front(hwnd: int) -> bool:
    """
    Окно вперёд. Windows разрешает SetForegroundWindow только процессу, который сейчас на переднем
    плане, — поэтому зовём, пока окно программы ещё активно (пользователь только что нажал кнопку).
    Если отказала — приём с AttachThreadInput: на миг "склеиваем" очередь ввода нашего потока с
    потоком активного окна, и Windows считает нас вправе сменить фокус. Клавиш не эмулируем.
    """
    ctypes, wintypes, u, k = _user32()
    SW_RESTORE = 9
    if u.IsIconic(hwnd):
        u.ShowWindow(hwnd, SW_RESTORE)
    if u.SetForegroundWindow(hwnd) and u.GetForegroundWindow() == hwnd:
        return True
    fg = u.GetForegroundWindow()
    fg_thread = u.GetWindowThreadProcessId(fg, None) if fg else 0
    me = k.GetCurrentThreadId()
    attached = bool(fg_thread and fg_thread != me and u.AttachThreadInput(me, fg_thread, True))
    try:
        u.BringWindowToTop(hwnd)
        u.SetForegroundWindow(hwnd)
    finally:
        if attached:
            u.AttachThreadInput(me, fg_thread, False)
    time.sleep(0.05)
    return u.GetForegroundWindow() == hwnd


def focus_game() -> "tuple[bool, str, bool]":
    """(успех, пояснение для лога/интерфейса, игра была свёрнута). Не Windows / окна нет — (False, почему, False)."""
    if os.name != "nt":
        return False, "не Windows", False
    try:
        h = find_game_window()
        if h is None:
            return False, "окно игры не найдено (TL.exe не запущен?)", False
        was_min = bool(_user32()[2].IsIconic(h))
        if bring_to_front(h):
            return True, "окно игры выведено вперёд" + (" (было свёрнуто)" if was_min else ""), was_min
        return False, "Windows не дал вывести окно игры вперёд", was_min
    except Exception as e:                       # Win32 не должен ронять кнопку снимка
        return False, "ошибка поиска окна игры: %s" % e, False


# ---------------- Активно ли окно игры (2026-10-11) ----------------
# Лог друга: бота запускали кнопкой "Старт" в программе — активным оставалось окно программы, и все
# нажатия Windows отдавала ЕЙ, а не игре: в интерфейсе скиллы "жмутся", в игре — ничего. Теперь бот
# жмёт клавиши, только пока на переднем плане окно игры; иначе ждёт (и не печатает в чужие окна).

def _foreground_pid() -> int:
    """Номер процесса окна на переднем плане (0 — нет). Два вызова Win32, доли микросекунды."""
    ctypes, wintypes, u, _k = _user32()
    h = u.GetForegroundWindow()
    if not h:
        return 0
    pid = wintypes.DWORD()
    u.GetWindowThreadProcessId(h, ctypes.byref(pid))
    return int(pid.value)


def _find_game_pid() -> "int | None":
    """Процесс окна игры (по TL.exe / заголовку, см. pick_game_window) или None — игры не видно."""
    wins = list_windows()
    h = pick_game_window(wins, os.getpid())
    return next((w[3] for w in wins if w[0] == h), None) if h is not None else None


class GameFocus:
    """
    active() -> True, если можно жать клавиши: на переднем плане окно игры. Сравниваем ПРОЦЕСС
    окна, а не само окно: у игры бывают служебные окна (лаунчер, всплывашки) — это тоже она.
    Игры не нашли вовсе (другой exe, не Windows) — True: лучше работать как раньше, чем встать навсегда.

    Дёшево: передний план — раз в CHECK_EVERY_S (0.25 с), перечисление окон (~1-3 мс) — не чаще
    раза в REFIND_EVERY_S и только когда спереди НЕ игра. На 60 тиках в секунду не заметно.

    Как проверить без игры: GameFocus(fg_pid_fn=lambda: 5, find_fn=lambda: 7) — active() False;
    fg 7 — True; find_fn -> None — True (см. t46/game_window_test.py, блок F).
    """
    CHECK_EVERY_S = 0.25
    REFIND_EVERY_S = 2.0

    def __init__(self, fg_pid_fn=None, find_fn=None) -> None:
        self._fg_pid = fg_pid_fn or _foreground_pid
        self._find = find_fn or _find_game_pid
        self._game_pid: "int | None" = None
        self._next_check = 0.0
        self._next_find = 0.0
        self._ok = True
        self.reason = ""

    def active(self, now: "float | None" = None) -> bool:
        if os.name != "nt" and self._fg_pid is _foreground_pid:
            return True                                   # не Windows — проверять нечем
        now = time.monotonic() if now is None else now
        if now < self._next_check:
            return self._ok                               # early return: между проверками — прошлый ответ
        self._next_check = now + self.CHECK_EVERY_S
        try:
            fg = self._fg_pid()
            if self._game_pid is None or (fg != self._game_pid and now >= self._next_find):
                self._game_pid = self._find()             # игру перезапустили — у неё новый процесс
                self._next_find = now + self.REFIND_EVERY_S
            if self._game_pid is None:
                self._ok, self.reason = True, "окно игры не найдено — проверку пропускаю"
            elif fg == self._game_pid:
                self._ok, self.reason = True, "окно игры активно"
            else:
                self._ok, self.reason = False, "окно игры не активно (спереди другое окно)"
        except Exception as e:                            # Win32 не должен останавливать бота
            self._ok, self.reason = True, "проверка окна не удалась: %s" % e
        return self._ok
