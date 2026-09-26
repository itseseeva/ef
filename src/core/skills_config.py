"""
src/core/skills_config.py

Загрузка и разбор skills_config.json — ЕДИНСТВЕННОЕ место в проекте, которое
знает формат этого файла. И bot.py (боевая ротация в COMBAT), и F5-тест в
main.py читают конфиг ТОЛЬКО через load_skills_config() — если формат файла
когда-нибудь поменяется, править нужно будет одну функцию, а не синхронизировать
две копии одной и той же логики парсинга в разных файлах.
"""

import os
import sys
import json
import logging

# Тот же приём, что уже используется в bot.py/vision_manager.py — добавляем
# корень проекта в пути поиска модулей, чтобы абсолютный импорт 'src.xxx'
# работал и при прямом запуске этого файла (python skills_config.py), а не
# только когда его импортируют как часть пакета src.core.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from src.core.input_manager import InputManager

logger = logging.getLogger(__name__)


def load_skills_config(path: str) -> list[dict]:
    """
    Читает skills_config.json и возвращает список скиллов В ПОРЯДКЕ ИХ
    ЗАПИСИ В ФАЙЛЕ. Порядок — это одновременно и приоритет ротации в
    bot.py (сверху вниз, первый готовый — тот и кастуем), и порядок
    прогона в F5-тесте. dict в Python 3.7+ гарантированно хранит порядок
    вставки ключей, а json.load сохраняет порядок ровно таким, каким он
    был в файле — поэтому достаточно просто пройтись по .items(), не
    сортируя ничего вручную и не храня отдельный список "порядок слотов".

    Каждый элемент результата:
        {
            "slot": "1",                       # ключ из JSON, только для логов
            "combo": "RB+A",                   # как записал пользователь
            "steps": ["hold_RB", "press_A", "release_RB"],  # уже разобрано
            "cooldown": 8.0,
            "last_used": 0.0,                  # time.monotonic() последнего каста
        }

    'steps' считается ЗАРАНЕЕ, один раз при старте бота, через
    InputManager.parse_combo_string — чтобы не парсить одну и ту же строку
    заново на каждый тик боевой ротации (60 FPS цикл не должен платить за
    разбор строк, которые не меняются, пока бот работает).

    Скилл с некорректной строкой combo ПРОПУСКАЕТСЯ с логом ошибки, а не
    роняет весь бот при старте — тот же принцип "безопасный дефолт", что и
    у остального зрения/ввода в проекте: лучше бот стартует без одного
    сломанного скилла, чем не стартует вообще из-за одной опечатки в JSON.
    """
    with open(path, "r", encoding="utf-8") as f:
        raw: dict = json.load(f)

    skills: list[dict] = []
    for slot, entry in raw.items():
        # JSON не поддерживает комментарии — соглашение проекта: любой ключ,
        # начинающийся с "_" (например "_README"), это заметка для человека,
        # а не скилл, и молча пропускается, не попадая даже в лог ошибок.
        if slot.startswith("_"):
            continue

        combo = entry.get("combo", "")
        try:
            steps = InputManager.parse_combo_string(combo)
        except ValueError as e:
            logger.error(
                "skills_config.json: слот '%s' пропущен — %s", slot, e
            )
            continue

        skills.append({
            "slot": slot,
            "combo": combo,
            "steps": steps,
            "cooldown": float(entry.get("cooldown", 5.0)),
            "last_used": 0.0,
        })

    logger.info(
        "skills_config.json: загружено %d скилл(ов) из '%s'.", len(skills), path
    )
    return skills


if __name__ == "__main__":
    # Песочница: парсит конфиг БЕЗ геймпада и без игры — только текстом
    # проверяем, что все combo-строки в файле разбираются без ошибок.
    # Удобно гонять сразу после правки skills_config.json, до запуска
    # main.py и тем более до захода в игру. os/sys уже импортированы вверху
    # файла (нужны там для sys.path.insert) — повторный import здесь не нужен.
    logging.basicConfig(level=logging.INFO)

    default_path = os.path.join(
        os.path.dirname(__file__), "..", "config", "skills_config.json"
    )
    path = sys.argv[1] if len(sys.argv) > 1 else default_path

    print(f"Проверяю '{path}'...")
    result = load_skills_config(path)
    for skill in result:
        print(
            f"  слот '{skill['slot']}': combo='{skill['combo']}' -> "
            f"{skill['steps']} (cooldown={skill['cooldown']}с)"
        )
    print(f"Итого валидных скиллов: {len(result)}")
