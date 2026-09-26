"""
configure_order.py

GUI-редактор порядка ротации скиллов. Это НЕ часть бота и не относится ни к
одному состоянию FSM напрямую — офлайн-инструмент авторинга, который просто
меняет порядок ключей в skills_config.json. Но результат его работы читает
COMBAT: skills_config.load_skills_config() проходит по .items() файла в том
порядке, в котором они записаны, и это порядок приоритета каста (сверху вниз,
первый готовый скилл — тот и кастуется). Поэтому "перетащить квадратик выше"
здесь и "кастовать этот скилл раньше" в бою — одно и то же действие.

Почему НЕ настоящий drag-and-drop (иконка следует за курсором пиксель в
пиксель), а клик-выбор + клик-обмен:
  Полноценный drag заставляет перерисовывать канвас на каждое движение мыши
  и постоянно считать, под каким квадратом курсор сейчас — это оправдано в
  инструменте, которым пользуются постоянно (боевая ротация, HUD), но не в
  конфигураторе, который открывают раз в несколько недель. Клик-клик даёт
  тот же результат (поменять два скилла местами) в разы меньшим и более
  надёжным кодом: нет риска "залипшего" перетаскивания, если пользователь
  отпустил кнопку мыши за пределами окна.
  Альтернатива: честный drag-and-drop — интуитивнее для новичка, но лишний
  код и риск визуального дребезга ради инструмента, который открывают редко.

Как пользоваться:
  1. Положи готовые PNG-иконки в icons/<слот>.png (1.png ... 9.png, 0.png,
     -.png, =.png) рядом с этим файлом. Если иконки для какого-то слота нет —
     скрипт не упадёт, просто нарисует серый квадрат с подписью слота вместо
     иконки (тот же принцип "безопасный дефолт", что и в skills_config.py).
  2. Запусти: python configure_order.py
  3. Клик по квадрату — подсветится оранжевой рамкой (выбран). Клик по
     другому квадрату — эти два скилла меняются местами, номера приоритета
     (кружки в углу) и стрелки-цепь между квадратами пересчитываются сразу.
  4. "Сохранить" — записывает новый порядок в skills_config.json (combo и
     cooldown каждого скилла не трогаются, меняется только порядок ключей).

Песочница для тестирования: запусти скрипт в пустой папке без icons/ и с
любым skills_config.json — вся логика выбора/обмена/сохранения проверяется
на заглушках-квадратах, без единого реального скриншота иконки.
"""

import os
import json
import tkinter as tk
from tkinter import messagebox

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "skills_config.json")
ICONS_DIR = os.path.join(BASE_DIR, "icons")

# Геометрия сетки. Это координаты ОКНА РЕДАКТОРА на экране автора, а не
# координаты игры — запрет на хардкод из Claude.md касается координат,
# которые бот ищет НА ЭКРАНЕ ИГРЫ (там разрешение у всех разное), а не
# вёрстки собственного инструмента, которая всегда одна и та же.
GRID_COLS = 4
GRID_ROWS = 3
CELL_W = 140
CELL_H = 170
MARGIN = 60
ICON_SIZE = 64

COLOR_BG = "#1e1e1e"
COLOR_SQUARE = "#2d2d30"
COLOR_SQUARE_MISSING = "#3a2d2d"
COLOR_BORDER = "#555555"
COLOR_BORDER_SELECTED = "#ff8800"
COLOR_TEXT = "#d0d0d0"
COLOR_TEXT_DIM = "#888888"
COLOR_ARROW = "#4fb0c9"
COLOR_ARROW_LOOP = "#ff6600"
COLOR_BADGE_BG = "#4fb0c9"
COLOR_BADGE_TEXT = "#1e1e1e"


class ConfigureOrderApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Порядок ротации скиллов — easy_farm")
        self.root.configure(bg=COLOR_BG)

        self.comment_entries, self.order = self._load_config()
        # PhotoImage должен жить, пока используется — Tk не хранит своих
        # ссылок, только рисует то, что ему передали. Если не держать
        # словарь с иконками как атрибут объекта, сборщик мусора Python
        # соберёт их сразу после _load_icons(), и квадраты станут пустыми.
        self.icon_images: dict[str, tk.PhotoImage | None] = self._load_icons()

        self.selected_index: int | None = None

        canvas_w = MARGIN * 2 + GRID_COLS * CELL_W
        canvas_h = MARGIN * 2 + GRID_ROWS * CELL_H + 30  # +30 — место под дугу возврата
        self.canvas = tk.Canvas(
            root, width=canvas_w, height=canvas_h, bg=COLOR_BG, highlightthickness=0
        )
        self.canvas.pack(padx=16, pady=(16, 8))
        self.canvas.bind("<Button-1>", self._on_click)

        self.status_var = tk.StringVar(value="")
        status_label = tk.Label(
            root, textvariable=self.status_var, bg=COLOR_BG, fg=COLOR_TEXT_DIM
        )
        status_label.pack(pady=(0, 4))

        button_frame = tk.Frame(root, bg=COLOR_BG)
        button_frame.pack(pady=(0, 16))
        tk.Button(
            button_frame, text="Сохранить", command=self._on_save,
            bg=COLOR_ARROW, fg=COLOR_BADGE_TEXT, activebackground="#6fc8de",
            relief=tk.FLAT, padx=16, pady=6,
        ).pack(side=tk.LEFT, padx=8)
        tk.Button(
            button_frame, text="Закрыть без сохранения", command=root.destroy,
            bg=COLOR_SQUARE, fg=COLOR_TEXT, activebackground="#454548",
            relief=tk.FLAT, padx=16, pady=6,
        ).pack(side=tk.LEFT, padx=8)

        self._redraw()

    # ------------------------------------------------------------------ #
    # Загрузка данных
    # ------------------------------------------------------------------ #

    def _load_config(self) -> tuple[dict, list[tuple[str, dict]]]:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            raw: dict = json.load(f)

        # Тот же принцип, что в skills_config.load_skills_config(): ключи
        # с "_" в начале — заметки для человека (например "_README"), не
        # скиллы. Отделяем их сразу, чтобы дальше работать со списком
        # только настоящих скиллов и не сломать заметку случайным свапом.
        comment_entries = {k: v for k, v in raw.items() if k.startswith("_")}
        order = [(k, v) for k, v in raw.items() if not k.startswith("_")]
        return comment_entries, order

    def _load_icons(self) -> dict:
        icons = {}
        for slot, _entry in self.order:
            path = os.path.join(ICONS_DIR, f"{slot}.png")
            try:
                icons[slot] = tk.PhotoImage(file=path)
            except tk.TclError:
                # Файла нет или это не PNG — не роняем редактор из-за одной
                # недостающей картинки, просто у этого слота не будет иконки
                # (см. fallback-квадрат в _draw_square).
                icons[slot] = None
        return icons

    # ------------------------------------------------------------------ #
    # Отрисовка
    # ------------------------------------------------------------------ #

    def _cell_center(self, index: int) -> tuple[int, int]:
        row, col = divmod(index, GRID_COLS)
        cx = MARGIN + col * CELL_W + CELL_W // 2
        cy = MARGIN + row * CELL_H + CELL_H // 2
        return cx, cy

    def _redraw(self) -> None:
        self.canvas.delete("all")
        self._draw_chain_arrows()
        for index in range(len(self.order)):
            self._draw_square(index)

    def _draw_chain_arrows(self) -> None:
        # Обычные стрелки цепи: центр квадрата i -> центр квадрата i+1.
        # Рисуются ДО квадратов, чтобы сами квадраты легли поверх и визуально
        # "срезали" концы линий — тогда стрелка выглядит как "входит в
        # иконку", а не утыкается в её рамку снаружи.
        for i in range(len(self.order) - 1):
            x1, y1 = self._cell_center(i)
            x2, y2 = self._cell_center(i + 1)
            self.canvas.create_line(
                x1, y1, x2, y2, fill=COLOR_ARROW, width=2, arrow=tk.LAST
            )

        # Возврат с последнего скилла на первый — та же ротация начинается
        # заново, это тоже часть "цепи", просто визуально её нужно отличать
        # от обычного порядка (иначе новичок подумает, что это ошибка
        # разметки). Пунктир + другой цвет + маршрут по внешнему краю холста
        # (а не напрямую через всю сетку) — чтобы линия не перечёркивала
        # квадраты, которые лежат между последним и первым.
        if len(self.order) >= 2:
            last_cx, last_cy = self._cell_center(len(self.order) - 1)
            first_cx, first_cy = self._cell_center(0)
            canvas_h = int(self.canvas["height"])
            bottom_y = canvas_h - 15
            left_x = MARGIN // 2
            self.canvas.create_line(
                last_cx, last_cy + ICON_SIZE // 2 + 4,
                last_cx, bottom_y,
                left_x, bottom_y,
                left_x, first_cy,
                first_cx - ICON_SIZE // 2 - 4, first_cy,
                fill=COLOR_ARROW_LOOP, width=2, dash=(5, 3), arrow=tk.LAST,
            )

    def _draw_square(self, index: int) -> None:
        slot, entry = self.order[index]
        cx, cy = self._cell_center(index)
        half = ICON_SIZE // 2
        icon = self.icon_images.get(slot)

        border_color = COLOR_BORDER_SELECTED if index == self.selected_index else COLOR_BORDER
        border_width = 3 if index == self.selected_index else 1
        fill_color = COLOR_SQUARE if icon is not None else COLOR_SQUARE_MISSING

        # Тег "cell_{index}" на всех элементах квадрата — не ради текущего
        # кода (клик обрабатывается через геометрию, см. _on_click), а
        # чтобы при будущей доработке (например подсветка по hover) можно
        # было получить все объекты одного квадрата одним canvas.find_withtag,
        # а не пересчитывать координаты заново.
        tag = f"cell_{index}"

        self.canvas.create_rectangle(
            cx - half - 4, cy - half - 4, cx + half + 4, cy + half + 4,
            fill=fill_color, outline=border_color, width=border_width, tags=tag,
        )

        if icon is not None:
            self.canvas.create_image(cx, cy, image=icon, tags=tag)
        else:
            self.canvas.create_text(
                cx, cy, text=slot, fill=COLOR_TEXT, font=("Segoe UI", 18, "bold"),
                tags=tag,
            )

        # Подпись под квадратом: combo + cooldown — чтобы при перестановке
        # сразу видеть, ЧТО именно ты передвинул, не сверяясь с JSON глазами.
        combo = entry.get("combo", "?")
        cooldown = entry.get("cooldown", "?")
        self.canvas.create_text(
            cx, cy + half + 20, text=combo, fill=COLOR_TEXT, font=("Segoe UI", 9),
            tags=tag,
        )
        self.canvas.create_text(
            cx, cy + half + 36, text=f"{cooldown}с", fill=COLOR_TEXT_DIM,
            font=("Segoe UI", 8), tags=tag,
        )

        # Кружок-бейдж с номером приоритета в углу квадрата — тот самый
        # "1, 2, 3..." порядок каста, который отдельно от порядка слотов на
        # баре (слот '9' может быть первым в ротации, бейдж это и показывает).
        badge_x, badge_y = cx - half - 4, cy - half - 4
        self.canvas.create_oval(
            badge_x - 11, badge_y - 11, badge_x + 11, badge_y + 11,
            fill=COLOR_BADGE_BG, outline="", tags=tag,
        )
        self.canvas.create_text(
            badge_x, badge_y, text=str(index + 1), fill=COLOR_BADGE_TEXT,
            font=("Segoe UI", 9, "bold"), tags=tag,
        )

    # ------------------------------------------------------------------ #
    # Взаимодействие
    # ------------------------------------------------------------------ #

    def _index_at(self, x: int, y: int) -> int | None:
        # Геометрия сетки фиксированная и известная заранее, поэтому проще
        # и надёжнее посчитать "в какую ячейку попал клик" по формуле, чем
        # гонять canvas.find_overlapping и разбирать, какому индексу
        # принадлежит найденный item — при кликах по стрелкам между
        # квадратами find_overlapping вообще ничего бы не нашёл.
        col = (x - MARGIN) // CELL_W
        row = (y - MARGIN) // CELL_H
        if not (0 <= col < GRID_COLS and 0 <= row < GRID_ROWS):
            return None
        index = row * GRID_COLS + col
        return index if index < len(self.order) else None

    def _on_click(self, event: tk.Event) -> None:
        index = self._index_at(event.x, event.y)
        if index is None:
            return

        if self.selected_index is None:
            self.selected_index = index
        elif self.selected_index == index:
            self.selected_index = None  # повторный клик по тому же квадрату — снять выбор
        else:
            self.order[self.selected_index], self.order[index] = (
                self.order[index], self.order[self.selected_index],
            )
            self.selected_index = None

        self._redraw()

    def _on_save(self) -> None:
        # dict в Python 3.7+ хранит порядок вставки — собираем новый dict
        # строго в порядке self.order, и json.dump запишет файл в этом же
        # порядке. Заметки (_README и т.п.) кладём первыми, как они и были.
        new_data = dict(self.comment_entries)
        for slot, entry in self.order:
            new_data[slot] = entry

        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(new_data, f, ensure_ascii=False, indent=2)
            f.write("\n")

        self.status_var.set(f"Сохранено в {os.path.basename(CONFIG_PATH)} ✓")


def main() -> None:
    if not os.path.exists(CONFIG_PATH):
        # Явная ошибка в консоль вместо traceback из глубины Tkinter —
        # тот же принцип "безопасный дефолт с понятным логом", что везде
        # в проекте, просто здесь это print, а не logging (короткоживущий
        # GUI-скрипт, отдельного логгера не заводим).
        print(f"Не найден {CONFIG_PATH} — редактор нечего открывать.")
        return

    root = tk.Tk()
    try:
        ConfigureOrderApp(root)
    except json.JSONDecodeError as e:
        messagebox.showerror("Ошибка", f"skills_config.json повреждён: {e}")
        return
    root.mainloop()


if __name__ == "__main__":
    main()
