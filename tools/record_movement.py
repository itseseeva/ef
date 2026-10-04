"""
tools/record_movement.py — запись ТВОИХ движений в игре: клавиши движения + повороты мыши.

Зачем: будущий контроллер движения бота (SearchPhase.ROAM) возьмёт из записей короткие
"фразы" движения (1-3 с) и статистику таймингов — и будет двигаться в твоём стиле, но
каждый раз по-новому. Целиком запись НЕ проигрывается (см. обсуждение 2026-10-04).

Как пользоваться (бот при этом ВЫКЛЮЧЕН):
    python tools/record_movement.py
    -> в игре F9 = старт/пауза записи, F10 = закончить и сохранить (или Ctrl+C в консоли).
    Набрать 10-15 минут бега по месту фарма, можно частями. Каждый запуск скрипта —
    отдельный файл в movement_records/, контроллер потом прочитает их все.
    Не меняй DPI мыши и чувствительность камеры в игре между записью и работой бота:
    повороты пишутся в "сырых" единицах мыши, бот будет крутить камеру в тех же.

Что пишется (и ничего больше):
    - клавиши из MOVE_KEYS (W A S D, пробел, Shift): нажал / отпустил + время;
    - движение мыши (сырые счётчики сенсора) — суммами за каждые 10 мс;
    - кнопки мыши (ЛКМ/ПКМ/колесо-кнопка): нажал / отпустил.
    Остальные клавиши (чат, пароли) отбрасываются до записи — сверка со списком MOVE_KEYS.
    Пока активно НЕ окно игры — ничего не пишется (зажатые клавиши закрываются отпусканием).

Античит: скрипт только СЛУШАЕТ через Raw Input (RIDEV_INPUTSINK — Windows сама присылает
копию ввода в наше скрытое окно). Он ничего не нажимает и не встраивается в цепочку ввода,
как это делают хуки SetWindowsHookEx, — для игры он невидим так же, как Discord/OBS.

Формат файла (JSONL — по одному JSON на строку, легко читать потоком):
    {"meta": {...}}                       первая строка: экран, клавиши, единицы
    {"t": 0.0, "ev": "start"}             F9: начало отрезка записи (t — секунды от запуска)
    {"t": 1.234, "k": "w", "d": 1}        клавиша W нажата (d=0 — отпущена)
    {"t": 1.240, "m": [12, -1]}           мышь сдвинулась на (dx, dy) за эти 10 мс
    {"t": 2.0, "b": "rmb", "d": 1}        кнопка мыши
    {"t": 9.9, "ev": "unfocus"}           игра потеряла фокус (alt-tab) — пауза
    {"t": 60.0, "ev": "stop"}             F9/F10: конец отрезка

Как проверить без игры: python tools/record_movement.py --selftest
    (логика записи на искусственных событиях; работает и не на Windows).
"""

from __future__ import annotations

import argparse
import io
import json
import ntpath
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, TextIO

# --- что пишем ------------------------------------------------------------------------
# VK-коды Windows -> имя в файле. Всё, чего нет в словаре, отбрасывается (whitelist
# надёжнее blacklist: новая клавиша по умолчанию НЕ пишется, а не наоборот).
MOVE_KEYS: "dict[int, str]" = {
    0x57: "w", 0x41: "a", 0x53: "s", 0x44: "d",
    0x20: "space", 0x10: "shift",
}
VK_F9, VK_F10 = 0x78, 0x79
MOUSE_BIN_S = 0.010          # мышь шлёт до 1000 событий/с — копим суммы по 10 мс: файл в ~10 раз меньше,
                             # а для фраз движения 10 мс — с запасом (кадр игры при 60 FPS = 16.7 мс)
PAUSE_SPLIT_S = 5.0          # тишина дольше — "отошёл от компьютера": в статистике не считаем
FOCUS_CACHE_S = 0.2          # проверять активное окно не чаще (это системные вызовы, а событий мыши — тысячи)
GAME_EXES = ("tl.exe",)                          # процесс игры (сравнение по имени файла)
GAME_TITLES = ("throne and liberty", "tl")       # или заголовок окна (полное совпадение / начало)
OUT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "movement_records"))


# =======================================================================================
# Логика записи — без Windows, чтобы её можно было проверить где угодно (--selftest)
# =======================================================================================

@dataclass
class Recorder:
    """
    Получает события ввода (уже разобранные) и пишет JSONL. Не знает про Windows:
    всё системное — в run_windows(). Так логику можно гонять на искусственных событиях.
    """
    out: TextIO
    meta: dict = field(default_factory=dict)
    recording: bool = False
    _t0: "float | None" = None
    _header_written: bool = False
    _down: "set[str]" = field(default_factory=set)        # что сейчас зажато (для отсева автоповтора)
    _focused: bool = True
    _bin_t: "float | None" = None
    _bin_dx: int = 0
    _bin_dy: int = 0
    # статистика для итога
    presses: "dict[str, int]" = field(default_factory=dict)
    hold_total_s: "dict[str, float]" = field(default_factory=dict)
    _pressed_at: "dict[str, float]" = field(default_factory=dict)
    mouse_abs_dx: int = 0
    mouse_abs_dy: int = 0
    active_s: float = 0.0
    _last_event_t: "float | None" = None
    skipped_synthetic: int = 0
    skipped_absolute: int = 0

    # --- служебное ---
    def _rel(self, now: float) -> float:
        if self._t0 is None:
            self._t0 = now
        return round(now - self._t0, 4)

    def _write(self, obj: dict) -> None:
        if not self._header_written:
            self.out.write(json.dumps({"meta": self.meta}, ensure_ascii=False) + "\n")
            self._header_written = True
        # separators без пробелов: файл компактнее, json всё равно читается штатно
        self.out.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")

    def _touch(self, now: float) -> None:
        """Время "активности": промежутки дольше PAUSE_SPLIT_S (отошёл) не считаем."""
        if self._last_event_t is not None:
            gap = now - self._last_event_t
            if gap <= PAUSE_SPLIT_S:
                self.active_s += gap
        self._last_event_t = now

    def _release_all(self, now: float) -> None:
        """Закрыть зажатые клавиши отпусканием: у каждого нажатия в файле есть пара."""
        for name in sorted(self._down):
            self._key_event(name, False, now)
        self._down.clear()

    def _key_event(self, name: str, down: bool, now: float) -> None:
        self._write({"t": self._rel(now), "k": name, "d": 1 if down else 0})
        if down:
            self.presses[name] = self.presses.get(name, 0) + 1
            self._pressed_at[name] = now
        else:
            t_on = self._pressed_at.pop(name, None)
            if t_on is not None:
                self.hold_total_s[name] = self.hold_total_s.get(name, 0.0) + (now - t_on)

    # --- управление ---
    def toggle(self, now: float) -> bool:
        """F9: старт/пауза. Возвращает новое состояние."""
        if self.recording:
            self.stop(now)
        else:
            self.recording = True
            self._last_event_t = None
            self._write({"t": self._rel(now), "ev": "start"})
        return self.recording

    def stop(self, now: float) -> None:
        if not self.recording:
            return
        self.flush_mouse(now, force=True)
        self._release_all(now)
        self._write({"t": self._rel(now), "ev": "stop"})
        self.recording = False

    def set_focus(self, focused: bool, now: float) -> None:
        """Игра потеряла фокус (alt-tab): закрываем зажатые клавиши, ввод не пишем."""
        if focused == self._focused:
            return
        self._focused = focused
        if self.recording:
            self.flush_mouse(now, force=True)
            if not focused:
                self._release_all(now)
            self._write({"t": self._rel(now), "ev": "focus" if focused else "unfocus"})

    @property
    def live(self) -> bool:
        return self.recording and self._focused

    # --- события ---
    def on_key(self, vk: int, down: bool, now: float) -> None:
        name = MOVE_KEYS.get(vk)
        if name is None or not self.live:
            return                                        # early return: чужая клавиша / пауза
        if down and name in self._down:
            return                                        # автоповтор Windows (зажатая клавиша шлёт "нажата" ~30 раз/с)
        if not down and name not in self._down:
            return                                        # отпускание без нажатия (нажали до старта записи)
        (self._down.add if down else self._down.discard)(name)
        self._touch(now)
        self._key_event(name, down, now)

    def on_mouse_button(self, name: str, down: bool, now: float) -> None:
        if not self.live:
            return
        self._touch(now)
        self._write({"t": self._rel(now), "b": name, "d": 1 if down else 0})

    def on_mouse_move(self, dx: int, dy: int, now: float) -> None:
        if not self.live or (dx == 0 and dy == 0):
            return
        self._touch(now)
        if self._bin_t is not None and now - self._bin_t >= MOUSE_BIN_S:
            self.flush_mouse(now, force=True)
        if self._bin_t is None:
            self._bin_t = now
        self._bin_dx += dx
        self._bin_dy += dy

    def flush_mouse(self, now: float, force: bool = False) -> None:
        """Записать накопленный сдвиг мыши. Без force — только если корзина "созрела" (таймер)."""
        if self._bin_t is None:
            return
        if not force and now - self._bin_t < MOUSE_BIN_S:
            return
        if self._bin_dx or self._bin_dy:
            self._write({"t": self._rel(self._bin_t), "m": [self._bin_dx, self._bin_dy]})
            self.mouse_abs_dx += abs(self._bin_dx)
            self.mouse_abs_dy += abs(self._bin_dy)
        self._bin_t, self._bin_dx, self._bin_dy = None, 0, 0

    # --- итог ---
    def summary(self) -> str:
        lines = ["Записано активного времени: %d:%02d (паузы дольше %.0f с не считаются; цель — 10-15 мин всего)"
                 % (int(self.active_s) // 60, int(self.active_s) % 60, PAUSE_SPLIT_S)]
        if self.presses:
            parts = ["%s: %d нажатий, в среднем %.0f мс" % (k, n, 1000 * self.hold_total_s.get(k, 0.0) / n)
                     for k, n in sorted(self.presses.items(), key=lambda kv: -kv[1])]
            lines.append("Клавиши — " + "; ".join(parts))
        else:
            lines.append("Клавиши — ни одного нажатия (игра была активна? F9 нажимал?)")
        lines.append("Мышь — по горизонтали %d, по вертикали %d (сырые единицы)" % (self.mouse_abs_dx, self.mouse_abs_dy))
        if self.skipped_synthetic:
            lines.append("Отброшено программных событий (не от рук): %d — бот или макрос был включён?"
                         % self.skipped_synthetic)
        if self.skipped_absolute:
            lines.append("Отброшено 'абсолютных' событий мыши (удалённый рабочий стол/планшет): %d" % self.skipped_absolute)
        return "\n".join(lines)


def is_game_window(title: str, exe: str, any_window: bool = False) -> bool:
    """Окно игры? По имени процесса (надёжно) или по заголовку. any_window — фильтр выключен."""
    if any_window:
        return True
    # ntpath, а не os.path: путь всегда виндовый (C:\...\TL.exe), а selftest гоняется и на Linux
    if ntpath.basename(exe).lower() in GAME_EXES:
        return True
    t = title.strip().lower()
    return any(t == g or t.startswith(g + " ") for g in GAME_TITLES)


# =======================================================================================
# Windows: Raw Input в скрытом окне
# =======================================================================================

def run_windows(any_window: bool = False) -> int:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    LRESULT = ctypes.c_ssize_t

    # --- структуры Raw Input (раскладка как в C для 64-битного Python) ---
    class RAWINPUTDEVICE(ctypes.Structure):
        _fields_ = [("usUsagePage", wintypes.USHORT), ("usUsage", wintypes.USHORT),
                    ("dwFlags", wintypes.DWORD), ("hwndTarget", wintypes.HWND)]

    class RAWINPUTHEADER(ctypes.Structure):
        _fields_ = [("dwType", wintypes.DWORD), ("dwSize", wintypes.DWORD),
                    ("hDevice", wintypes.HANDLE), ("wParam", wintypes.WPARAM)]

    class RAWMOUSE(ctypes.Structure):
        # В C после usFlags идёт union с ULONG — он выровнен на 4 байта, отсюда _pad.
        _fields_ = [("usFlags", wintypes.USHORT), ("_pad", wintypes.USHORT),
                    ("usButtonFlags", wintypes.USHORT), ("usButtonData", wintypes.USHORT),
                    ("ulRawButtons", wintypes.ULONG), ("lLastX", wintypes.LONG),
                    ("lLastY", wintypes.LONG), ("ulExtraInformation", wintypes.ULONG)]

    class RAWKEYBOARD(ctypes.Structure):
        _fields_ = [("MakeCode", wintypes.USHORT), ("Flags", wintypes.USHORT),
                    ("Reserved", wintypes.USHORT), ("VKey", wintypes.USHORT),
                    ("Message", wintypes.UINT), ("ExtraInformation", wintypes.ULONG)]

    class _RAWDATA(ctypes.Union):
        _fields_ = [("mouse", RAWMOUSE), ("keyboard", RAWKEYBOARD)]

    class RAWINPUT(ctypes.Structure):
        _fields_ = [("header", RAWINPUTHEADER), ("data", _RAWDATA)]

    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                    ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                    ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                    ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]

    # argtypes обязательны: без них ctypes обрежет 64-битные HWND/LPARAM до int и упадёт
    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.DefWindowProcW.restype = LRESULT
    user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                       ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                       wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.GetRawInputData.argtypes = [wintypes.HANDLE, wintypes.UINT, wintypes.LPVOID,
                                       ctypes.POINTER(wintypes.UINT), wintypes.UINT]
    user32.GetRawInputData.restype = wintypes.UINT
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.restype = LRESULT
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, wintypes.LPVOID]
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.RegisterRawInputDevices.argtypes = [wintypes.LPVOID, wintypes.UINT, wintypes.UINT]
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE       # без этого 64-битный адрес модуля обрезался бы до int
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                    ctypes.POINTER(wintypes.DWORD)]

    WM_INPUT, WM_TIMER = 0x00FF, 0x0113
    RID_INPUT, RIM_TYPEMOUSE, RIM_TYPEKEYBOARD = 0x10000003, 0, 1
    RIDEV_INPUTSINK = 0x00000100          # получать ввод, даже когда наше окно не активно
    RI_KEY_BREAK = 0x01                   # флаг "клавиша отпущена"
    MOUSE_MOVE_ABSOLUTE = 0x01
    HWND_MESSAGE = wintypes.HWND(-3)      # невидимое окно только для сообщений
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    BTN = {0x0001: ("lmb", True), 0x0002: ("lmb", False), 0x0004: ("rmb", True),
           0x0008: ("rmb", False), 0x0010: ("mmb", True), 0x0020: ("mmb", False)}

    # --- скрытое окно-приёмник ---
    def _wndproc(hwnd, msg, wparam, lparam):
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
    wndproc = WNDPROC(_wndproc)           # держим ссылку: иначе сборщик мусора удалит колбэк и Windows упадёт
    hinst = kernel32.GetModuleHandleW(None)
    wc = WNDCLASSW(lpfnWndProc=wndproc, hInstance=hinst, lpszClassName="EZF_MoveRecorder")
    if not user32.RegisterClassW(ctypes.byref(wc)):
        print("Не удалось зарегистрировать окно (ошибка %d)" % ctypes.get_last_error())
        return 1
    hwnd = user32.CreateWindowExW(0, wc.lpszClassName, "", 0, 0, 0, 0, 0, HWND_MESSAGE, None, hinst, None)
    devs = (RAWINPUTDEVICE * 2)(RAWINPUTDEVICE(0x01, 0x02, RIDEV_INPUTSINK, hwnd),   # мышь
                                RAWINPUTDEVICE(0x01, 0x06, RIDEV_INPUTSINK, hwnd))   # клавиатура
    if not user32.RegisterRawInputDevices(devs, 2, ctypes.sizeof(RAWINPUTDEVICE)):
        print("Не удалось подписаться на Raw Input (ошибка %d)" % ctypes.get_last_error())
        return 1
    user32.SetTimer(hwnd, 1, 50, None)    # раз в 50 мс: дописать хвост мыши, статус в консоль

    # --- активное окно: игра или нет (с кэшем, это системные вызовы) ---
    proc_cache: "dict[int, tuple[str, str]]" = {}

    def window_info(h) -> "tuple[str, str]":
        key = int(h or 0)
        if key in proc_cache:
            return proc_cache[key]
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(h, buf, 256)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
        exe = ""
        hp = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if hp:
            pbuf = ctypes.create_unicode_buffer(520)
            size = wintypes.DWORD(520)
            if kernel32.QueryFullProcessImageNameW(hp, 0, pbuf, ctypes.byref(size)):
                exe = pbuf.value
            kernel32.CloseHandle(hp)
        proc_cache[key] = (buf.value, exe)
        return proc_cache[key]

    focus_state = {"at": -1.0, "game": False}

    def game_focused(now: float) -> bool:
        if now - focus_state["at"] >= FOCUS_CACHE_S:
            title, exe = window_info(user32.GetForegroundWindow())
            focus_state["game"] = is_game_window(title, exe, any_window)
            focus_state["at"] = now
        return focus_state["game"]

    # --- файл ---
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, time.strftime("move_%Y%m%d_%H%M%S.jsonl"))
    sw, sh = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    meta = {"version": 1, "created": time.strftime("%Y-%m-%d %H:%M:%S"), "screen": [sw, sh],
            "keys": sorted(MOVE_KEYS.values()), "mouse_bin_s": MOUSE_BIN_S,
            "mouse_units": "raw counts (lLastX/lLastY) — те же единицы, что MOUSEEVENTF_MOVE у SendInput"}
    f = open(path, "w", encoding="utf-8", buffering=1 << 16)
    rec = Recorder(out=f, meta=meta)

    print("Запись движений. В игре: F9 — старт/пауза, F10 — закончить. Бот должен быть выключен.")
    print("Файл:", path)
    buf = ctypes.create_string_buffer(1024)
    size = wintypes.UINT()
    hdr_size = ctypes.sizeof(RAWINPUTHEADER)
    msg = wintypes.MSG()
    last_status = 0.0
    quit_requested = False

    try:
        while not quit_requested and user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            now = time.perf_counter()
            if msg.message == WM_INPUT:
                size.value = ctypes.sizeof(buf)
                if user32.GetRawInputData(msg.lParam, RID_INPUT, buf, ctypes.byref(size), hdr_size) != 0xFFFFFFFF:
                    ri = RAWINPUT.from_buffer(buf)
                    rec.set_focus(game_focused(now), now)
                    if not ri.header.hDevice:
                        # hDevice == 0 — событие создано программой (SendInput), а не руками: не пишем
                        rec.skipped_synthetic += rec.live
                    elif ri.header.dwType == RIM_TYPEKEYBOARD:
                        kb = ri.data.keyboard
                        down = not (kb.Flags & RI_KEY_BREAK)
                        if kb.VKey == VK_F9 and down:
                            title, exe = window_info(user32.GetForegroundWindow())
                            state = rec.toggle(now)
                            print("\n%s  | активное окно: '%s' (%s) — %s" % (
                                "● ЗАПИСЬ" if state else "■ ПАУЗА", title, os.path.basename(exe) or "?",
                                "игра" if is_game_window(title, exe, any_window)
                                else "НЕ игра: ввод пишется только пока активна игра (или запусти с --any-window)"))
                        elif kb.VKey == VK_F10 and down:
                            quit_requested = True
                        else:
                            rec.on_key(kb.VKey, down, now)
                    elif ri.header.dwType == RIM_TYPEMOUSE:
                        m = ri.data.mouse
                        if m.usFlags & MOUSE_MOVE_ABSOLUTE:
                            rec.skipped_absolute += rec.live
                        else:
                            rec.on_mouse_move(m.lLastX, m.lLastY, now)
                        for flag, (name, down) in BTN.items():
                            if m.usButtonFlags & flag:
                                rec.on_mouse_button(name, down, now)
            elif msg.message == WM_TIMER:
                rec.set_focus(game_focused(now), now)
                rec.flush_mouse(now)
                if now - last_status >= 1.0:
                    last_status = now
                    if rec.recording:
                        sys.stdout.write("\r● ЗАПИСЬ %d:%02d%s   " % (
                            int(rec.active_s) // 60, int(rec.active_s) % 60,
                            "" if rec.live else "  (игра не активна — пауза)"))
                        sys.stdout.flush()
            user32.DispatchMessageW(ctypes.byref(msg))   # WM_INPUT обязательно через DefWindowProc: Windows чистит буфер
    except KeyboardInterrupt:
        pass
    finally:
        now = time.perf_counter()
        rec.stop(now)
        f.close()
        user32.DestroyWindow(hwnd)
        if not rec._header_written:
            os.remove(path)                   # ни разу не жали F9 — пустой файл не оставляем
            print("\nЗапись не начиналась (F9 не нажимали) — файл не создан.")
        else:
            print("\n" + rec.summary())
            print("Сохранено:", path)
    return 0


# =======================================================================================
# Самопроверка без игры
# =======================================================================================

def selftest() -> int:
    fails = 0

    def check(name: str, cond: bool, info: object = "") -> None:
        nonlocal fails
        print(("PASS " if cond else "FAIL ") + name + ((" | %s" % (info,)) if info != "" else ""))
        fails += not cond

    def lines(buf: io.StringIO) -> "list[dict]":
        return [json.loads(x) for x in buf.getvalue().splitlines()]

    out = io.StringIO()
    r = Recorder(out=out, meta={"test": 1})
    r.on_key(0x57, True, 0.0)                                 # до F9 — не пишется
    check("S1: до старта ничего не пишется", out.getvalue() == "")
    r.toggle(1.0)
    r.on_key(0x57, True, 1.1)
    for i in range(10):
        r.on_key(0x57, True, 1.1 + 0.03 * i)                  # автоповтор
    r.on_key(0x51, True, 1.2)                                 # Q — не из списка
    r.on_key(0x57, False, 1.6)
    ev = lines(out)
    keys = [e for e in ev if "k" in e]
    check("S2: заголовок meta первой строкой", "meta" in ev[0] and ev[0]["meta"] == {"test": 1})
    check("S3: автоповтор отсеян — одно нажатие и одно отпускание W", [(e["k"], e["d"]) for e in keys] == [("w", 1), ("w", 0)], keys)
    check("S4: чужая клавиша (Q) не записана", all(e.get("k") != "q" for e in ev))
    check("S5: время от запуска, удержание 0.5 с", abs(keys[1]["t"] - keys[0]["t"] - 0.5) < 1e-6 and
          abs(r.hold_total_s["w"] - 0.5) < 1e-9)

    # мышь: 100 событий по (1, 0) за 50 мс -> ~5 корзин, сумма сохраняется
    for i in range(100):
        r.on_mouse_move(1, 0, 2.0 + 0.0005 * i)
    r.flush_mouse(2.2)
    ms = [e for e in lines(out) if "m" in e]
    check("S6: мышь копится корзинами по 10 мс (5 корзин на 50 мс)", 4 <= len(ms) <= 6, len(ms))
    check("S7: сумма сдвигов мыши не теряется", sum(e["m"][0] for e in ms) == 100)

    # alt-tab с зажатой D: отпускание пишется сразу, ввод вне игры не пишется
    r.on_key(0x44, True, 3.0)
    r.set_focus(False, 3.5)
    r.on_key(0x41, True, 3.6)
    r.on_mouse_move(50, 0, 3.7)
    r.set_focus(True, 4.0)
    ev = lines(out)
    tail = [e for e in ev if e.get("t", -1) >= 2.0]          # t в файле — от старта (F9 на 1.0): 3.0 -> 2.0
    check("S8: потеря фокуса закрыла D отпусканием и отметилась",
          [(e.get("k"), e.get("d"), e.get("ev")) for e in tail[:3]] == [("d", 1, None), ("d", 0, None), (None, None, "unfocus")], tail[:3])
    check("S9: вне игры A и мышь не записаны", not any(e.get("k") == "a" for e in tail) and
          not any("m" in e and e["t"] > 2.5 for e in tail))
    # отпускание A после возврата (нажата была вне игры) — не пишем "висячее" отпускание
    r.on_key(0x41, False, 4.1)
    check("S10: отпускание без записанного нажатия игнорируется", not any(e.get("k") == "a" for e in lines(out)))

    # стоп с зажатой клавишей
    r.on_key(0x20, True, 5.0)
    r.toggle(5.3)
    ev = lines(out)
    check("S11: стоп закрыл пробел отпусканием и записал stop",
          [(e.get("k"), e.get("d")) for e in ev[-2:-1]] == [("space", 0)] and ev[-1].get("ev") == "stop", ev[-2:])
    n = len(ev)
    r.on_key(0x57, True, 6.0)
    check("S12: после стопа ничего не пишется", len(lines(out)) == n)

    # активное время: пауза 30 с не считается
    r2 = Recorder(out=io.StringIO())
    r2.toggle(0.0)
    r2.on_key(0x57, True, 0.0); r2.on_key(0x57, False, 2.0)
    r2.on_key(0x57, True, 32.0); r2.on_key(0x57, False, 33.0)
    check("S13: пауза дольше 5 с не входит в активное время", abs(r2.active_s - 3.0) < 1e-9, r2.active_s)

    check("S14: окно игры по процессу TL.exe", is_game_window("что угодно", r"C:\Games\TL\Binaries\TL.exe"))
    check("S15: окно игры по заголовку", is_game_window("Throne and Liberty", "") and is_game_window("TL", ""))
    check("S16: браузер/Discord — не игра", not is_game_window("TLDR - YouTube", "chrome.exe") and
          not is_game_window("Discord", "Discord.exe"))
    check("S17: --any-window пишет в любом окне", is_game_window("Discord", "Discord.exe", any_window=True))

    t0 = time.perf_counter()
    r3 = Recorder(out=io.StringIO()); r3.toggle(0.0)
    for i in range(100_000):
        r3.on_mouse_move(1, 1, i * 0.001)
    us = (time.perf_counter() - t0) / 100_000 * 1e6
    check("P1: событие мыши обрабатывается за %.1f мкс (мышь шлёт до 1000/с — нужно << 1000 мкс)" % us, us < 100)
    print("FAILS", fails)
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Запись движений (клавиши движения + мышь) для контроллера ROAM")
    ap.add_argument("--selftest", action="store_true", help="проверка логики записи без игры")
    ap.add_argument("--any-window", action="store_true", help="писать в любом активном окне (если игру не узнаёт)")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if sys.platform != "win32":
        print("Запись работает только в Windows (нужен Raw Input). Проверка логики: --selftest")
        return 1
    return run_windows(any_window=args.any_window)


if __name__ == "__main__":
    sys.exit(main())
