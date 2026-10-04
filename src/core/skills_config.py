"""
src/core/skills_config.py

Загрузка и разбор конфига боевых цепочек — ЕДИНСТВЕННОЕ место в проекте,
которое знает формат JSON, экспортируемого React-приложением (EasyFarm Bot
Suite). bot.py читает цепочки ТОЛЬКО через load_skills_config() — если
формат экспорта фронтенда поменяется, править нужно одну функцию, а не
искать её логику по всему bot.py.

СМЕНА МОДЕЛИ (было: кулдаун на отдельный скилл, стало: кулдаун на цепочку):
раньше бот шёл по плоскому списку скиллов и кастовал по одному, каждый со
своим таймером. Теперь пользователь в интерфейсе собирает СКИЛЛ-ЦЕПОЧКИ
(несколько умений подряд как один залп), и кулдаун вешается на цепочку
целиком — причём ДИАПАЗОНОМ (min/max), а не одним числом, той же анти-детект
логикой рандомизации, что уже применяется везде в bot.py/input_manager.py
(см. bot.py._SEARCH_TAB_COOLDOWN и подобные). Старый плоский формат
сознательно не поддерживается — это осознанный breaking change, а не
недосмотр: проект ещё не в проде, а держать в одном файле разбор двух
разных форматов ради обратной совместимости, которая никому не нужна,
только множит места для рассинхрона.

Ожидаемый формат входного файла (кнопка экспорта в приложении):
{
  "_README": "...",
  "hotkeySlots": [...],   # раскладка панели умений для UI — боту не нужна,
                          # вся нужная для боя информация уже продублирована
                          # в каждом шаге "chains" ниже
  "chains": [
    {
      "id": "...", "name": "Цепочка #1", "order": 1,
      "cooldownMinSeconds": 12.0, "cooldownMaxSeconds": 18.0,
      "steps": [
        {"stepIndex": 1, "slot": "1", "combo": "RB+X", "skillName": "...",
         "repeatCount": 2,   # необязательное поле — сколько раз подряд
                             # нажать ЭТОТ шаг, прежде чем перейти к
                             # следующему (некоторые скиллы в игре нужно
                             # кликать несколько раз, например каналящиеся
                             # умения). По умолчанию 1, если поля нет вовсе
                             # (старые экспорты фронтенда без этого поля
                             # продолжают работать без изменений).
         "castTimeSeconds": 5.0},  # необязательное поле — реальное время
                             # каста ЭТОГО скилла в игре (сек). Если задано,
                             # следующий скилл (или повтор этого же, если
                             # repeatCount > 1) ждёт ~castTimeSeconds (±10%
                             # джиттер) вместо обычной короткой межскилльной
                             # паузы — нужно долгим кастам/каналам, чтобы
                             # следующее нажатие не срывало ещё не долетевший
                             # каст. Нет поля / null / 0 — обычная короткая
                             # пауза, как раньше.
        ...
      ]
    }
  ]
}
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

from src.core.input_manager import InputManager, input_debug_logger

logger = logging.getLogger(__name__)

# Дефолтные времена каста ПО СКИЛЛУ (не по шагу) — на случай, если
# пользователь ещё не выставил castTimeSeconds вручную через бейдж в
# интерфейсе (или вообще не в курсе, что у скилла долгий каст). Применяется
# ТОЛЬКО когда в JSON поле отсутствует ИЛИ равно 0 — явно заданное
# положительное значение из интерфейса всегда важнее этой таблицы.
#
# Компромисс, о котором стоит знать: интерфейс сейчас всегда пишет в JSON
# castTimeSeconds (0, если бейдж не трогали), а не пропускает поле вовсе —
# то есть бэкенд не может технически отличить "пользователь ни разу не
# кликал бейдж" от "пользователь explicitly вернул его в 0/выкл". Раз уж
# отличить нельзя, безопаснее считать 0 сигналом "ничего не настроено" и
# подставлять дефолт из таблицы — тот же принцип "безопасный дефолт", что и
# везде в этом файле (например обмен местами cooldownMin/cooldownMax). Кому
# ТОЧНО нужно выключить каст-паузу для скилла, у которого есть дефолт здесь,
# сейчас может это переопределить, поставив на бейдже любое другое
# небольшое значение (например 1 сек) — не идеально красиво, но правило
# "не даём боту случайно сорвать долгий каст" важнее.
#
# Ключ — skillId (стабильный, не меняется при правках названий в каталоге
# фронта), а не skillName. Имя скилла — только в комментарии для читаемости.
_DEFAULT_CAST_TIME_BY_SKILL_ID: dict[str, float] = {
    "skill_13": 5.0,  # "Внутренний покой" (Staff) — долгий канал
}


def _order_by_trigger(raw_chains: "list[dict]") -> "list[dict]":
    """
    Порядок цепочек как в поле "Пуск" приложения (2026-10-03): сначала та, что
    "со старта", потом та, что "после неё", и так далее. Раньше бот смотрел
    только на "order" (место в списке), и "Пуск: После Цепочка #2" сохранялся
    в файл, но на ротацию не влиял.

    Обход в глубину от "start": дети узла X — цепочки с triggerAfter == X (между
    собой — по "order"). Ссылка на несуществующую цепочку = "со старта". Цепочки,
    до которых от старта не дойти (зациклили: #1 после #2, #2 после #1), идут
    в конец по "order" — бот не теряет ни одной.
    Как проверить без игры: python src/core/skills_config.py путь_к_json —
    печатает цепочки в итоговом порядке.
    """
    by_order = sorted(raw_chains, key=lambda c: c.get("order", 0))
    ids = {c.get("id") for c in by_order}
    children: "dict[str, list[dict]]" = {}
    for c in by_order:
        parent = c.get("triggerAfter") or "start"
        if parent != "start" and parent not in ids:
            parent = "start"
        children.setdefault(parent, []).append(c)

    result: "list[dict]" = []
    seen: "set[int]" = set()          # id() объекта: у цепочки может не быть поля "id"
    stack = list(reversed(children.get("start", [])))
    while stack:                       # явный стек вместо рекурсии — цикл в данных её не уронит
        c = stack.pop()
        if id(c) in seen:
            continue
        seen.add(id(c))
        result.append(c)
        stack.extend(reversed(children.get(c.get("id"), [])))
    rest = [c for c in by_order if id(c) not in seen]
    if rest:
        logger.warning(
            "Цепочки %s не связаны со стартом (поле 'Пуск' зациклено?) — ставлю в конец.",
            [c.get("name") for c in rest],
        )
    return result + rest


def load_skills_config(path: str) -> list[dict]:
    """
    Читает JSON-экспорт приложения и возвращает список ЦЕПОЧЕК,
    отсортированный по полю "order" — это и есть приоритет ротации в
    bot.py (первая цепочка в списке с истёкшим кулдауном кастуется
    первой; тот же принцип, что раньше был у порядка ключей в плоском
    списке скиллов). Сортируем явно по "order", а не полагаемся на
    порядок элементов массива в файле — "order" единственное поле,
    которое реально управляет приоритетом (та же сортировка, что
    фронтенд сам делает в своём тестовом прогоне F5).

    Каждый элемент результата:
        {
            "name": "Цепочка #1 (Основная)",        # для логов
            "sequence": ["1", "2", "3"],              # slot-ключи, для логов
            "gamepad_steps": [[...], [...], [...]],   # steps каждого скилла
                                                       # цепочки, по порядку
            "cast_times": [None, 5.0, None],          # параллельный
                                                       # gamepad_steps список
                                                       # той же длины — время
                                                       # каста каждого шага
                                                       # или None (обычная
                                                       # короткая пауза)
            "cooldown_min": 12.0,
            "cooldown_max": 18.0,
            "next_ready_at": 0.0,   # time.monotonic()-дедлайн; 0.0 = готова
                                    # сразу на первом тике
        }

    "gamepad_steps" считается ЗАРАНЕЕ через InputManager.parse_combo_string
    — один раз при старте, а не на каждый тик боевой ротации (тот же принцип
    "не парсить строку заново 60 раз в секунду", что был и в старой версии
    для одиночных скиллов).

    Устойчивость к битым/неполным данным — тот же принцип "безопасный
    дефолт", что и раньше:
      - шаг цепочки без умения (slot и combo оба пустые — пустой квадратик
        "Пусто" в интерфейсе) молча пропускается, это не ошибка;
      - шаг с невалидной combo-строкой пропускается С ЛОГОМ ошибки, но не
        роняет всю цепочку — остальные шаги этой же цепочки продолжают
        работать;
      - цепочка, оставшаяся совсем без валидных шагов, пропускается целиком
        с предупреждением — лучше бот стартует без одной сломанной цепочки,
        чем не стартует вообще из-за одной опечатки в интерфейсе;
      - если в интерфейсе перепутали местами min/max кулдауна (max < min),
        меняем их местами и предупреждаем в логе, а не роняем бота в бою
        первым же вызовом random.uniform(min, max) с min > max.
    """
    with open(path, "r", encoding="utf-8") as f:
        raw: dict = json.load(f)

    raw_chains = _order_by_trigger(raw.get("chains", []))

    chains: list[dict] = []
    for raw_chain in raw_chains:
        chain_name = raw_chain.get("name") or raw_chain.get("id") or "безымянная цепочка"

        sequence: list[str] = []
        gamepad_steps: list[list[str]] = []
        # Параллельный gamepad_steps список (та же длина, тот же порядок,
        # включая повторы repeatCount ниже) — реальное время каста ЭТОГО
        # шага в секундах, или None, если у шага нет кастомного времени.
        # Отдельный список, а не поле внутри gamepad_steps: gamepad_steps
        # — это list[list[str]], уже используемый как есть в нескольких
        # местах (warmup в app_launcher.py берёт chains[0]["gamepad_steps"][0]
        # напрямую) — проще завести новый параллельный список, чем менять
        # формат существующего и чинить все места, которые на него завязаны.
        cast_times: list[float | None] = []

        for raw_step in raw_chain.get("steps", []):
            slot = raw_step.get("slot")
            # Клавиатурная версия (2026-10-02): "slot" — это и есть клавиша
            # (любая из приложения: буквы, цифры, F1-F12, знаки, Space...; см.
            # InputManager.canonical_key), поэтому парсим именно его. Поле "combo"
            # (геймпадное RB+X и т.п.) в экспорте ещё есть, но боту оно
            # больше не нужно — намеренно игнорируем.
            if not slot:
                continue

            try:
                steps = InputManager.parse_combo_string(slot)
            except ValueError as e:
                logger.error(
                    "Цепочка '%s': шаг слота '%s' пропущен — %s",
                    chain_name, slot, e,
                )
                # И в input_debug.log: раньше пропуск был виден только в консоли,
                # и шаг с P просто молча не жался.
                input_debug_logger.warning(
                    "CONFIG  '%s': шаг '%s' ПРОПУЩЕН — бот не знает такую клавишу", chain_name, slot,
                )
                continue

            # Некоторые скиллы в игре нужно нажать несколько раз подряд
            # (каналящиеся/накапливающиеся умения) — repeatCount задаёт,
            # сколько раз ПОВТОРИТЬ этот же шаг, прежде чем перейти к
            # следующему. Разворачиваем повтор ЗДЕСЬ, добавляя один и тот
            # же распарсенный combo в gamepad_steps N раз, а не храня
            # отдельное поле "count" на элементе — _act_combat_rotation() в
            # bot.py уже сейчас идёт простым циклом по gamepad_steps и
            # кастует каждый элемент через execute_combo(), с обычной
            # рандомизированной паузой между ними (_SKILL_CAST_GAP_BANDS_S в
            # InputManager). Продублировав шаг, получаем "нажать N раз с
            # паузами" бесплатно, без единой строки изменений в самой
            # боевой ротации.
            repeat_count = raw_step.get("repeatCount", 1)
            try:
                repeat_count = int(repeat_count)
            except (TypeError, ValueError):
                logger.warning(
                    "Цепочка '%s': repeatCount шага слота '%s' не число "
                    "(%r), считаю за 1.",
                    chain_name, slot, repeat_count,
                )
                repeat_count = 1
            # Защита от опечатки/бага фронтенда (например, случайно вбитое
            # repeatCount=500) — цепочка не должна раздуваться до
            # неадекватной длины. 10 с запасом выше любого реального
            # игрового кейса.
            repeat_count = max(1, min(repeat_count, 10))

            # castTimeSeconds — необязательное поле: реальное время каста
            # ЭТОГО скилла в игре (например 5 сек у долгого канала). Если
            # задано — execute_combo() в InputManager подождёт ЕГО (±10%
            # джиттер) после этого шага вместо обычной короткой межскилльной
            # паузы, чтобы следующий скилл не срывал ещё не долетевший каст.
            # Нет поля / null / 0 — обычная короткая пауза, ничего не меняем
            # (обратная совместимость со старыми экспортами конфига).
            raw_cast_time = raw_step.get("castTimeSeconds")
            cast_time_s: float | None
            if raw_cast_time is None:
                cast_time_s = None
            else:
                try:
                    cast_time_s = float(raw_cast_time)
                except (TypeError, ValueError):
                    logger.warning(
                        "Цепочка '%s': castTimeSeconds шага слота '%s' не "
                        "число (%r), игнорирую — обычная короткая пауза.",
                        chain_name, slot, raw_cast_time,
                    )
                    cast_time_s = None
                else:
                    # 0 или отрицательное — то же самое, что "не задано":
                    # нулевая пауза после долгого каста бессмысленна, а не
                    # "специально быстро". Верхний потолок 30с — защита от
                    # опечатки (например 50 вместо 5.0), с запасом выше
                    # любого реального игрового каста.
                    if cast_time_s <= 0.0:
                        cast_time_s = None
                    else:
                        cast_time_s = min(cast_time_s, 30.0)

            # Явного значения нет (см. компромисс выше) — пробуем дефолт по
            # skillId из таблицы _DEFAULT_CAST_TIME_BY_SKILL_ID.
            if cast_time_s is None:
                skill_id = raw_step.get("skillId")
                default_cast_time = _DEFAULT_CAST_TIME_BY_SKILL_ID.get(skill_id)
                if default_cast_time is not None:
                    cast_time_s = default_cast_time
                    logger.info(
                        "Цепочка '%s': шаг слота '%s' (skillId=%s) без "
                        "castTimeSeconds в файле — подставляю дефолт %.1fс.",
                        chain_name, slot, skill_id, cast_time_s,
                    )

            for _ in range(repeat_count):
                sequence.append(slot)
                gamepad_steps.append(steps)
                cast_times.append(cast_time_s)

        if not gamepad_steps:
            logger.warning(
                "Цепочка '%s' пропущена целиком — нет ни одного валидного шага.",
                chain_name,
            )
            continue

        # Интерфейс теперь отдаёт ОДНО число ("cooldownSeconds") вместо
        # пары min/max — так пользователю проще заполнять форму. Но
        # кастовать цепочку СТРОГО через одинаковое время каждый раз —
        # тот самый статистически заметный "робо-ритм", с которым везде
        # по проекту борется рандомизация диапазоном (см. докстринг
        # InputManager._SKILL_CAST_GAP_BANDS_S). Поэтому бэкенд сам
        # разворачивает одно число в диапазон ±10% — пользователю не
        # нужно думать о рандомизации, а анти-детект разброс всё равно
        # сохраняется.
        raw_cooldown = raw_chain.get("cooldownSeconds")
        if raw_cooldown is not None:
            cooldown_seconds = float(raw_cooldown)
            _COOLDOWN_JITTER_FRACTION = 0.10
            cooldown_min = cooldown_seconds * (1.0 - _COOLDOWN_JITTER_FRACTION)
            cooldown_max = cooldown_seconds * (1.0 + _COOLDOWN_JITTER_FRACTION)
        else:
            # Обратная совместимость со старым форматом (пара
            # cooldownMinSeconds/cooldownMaxSeconds) — на случай, если
            # экспортированный конфиг ещё не пересобран под новый формат
            # интерфейса. Оставлено СОЗНАТЕЛЬНО, в отличие от общего
            # правила проекта "старый формат не поддерживаем": тут это не
            # старая архитектура целиком, а всего одно поле, и цена
            # поддержки обоих вариантов на переходный период — три строки.
            cooldown_min = float(raw_chain.get("cooldownMinSeconds", 0.0))
            cooldown_max = float(raw_chain.get("cooldownMaxSeconds", cooldown_min))
            if cooldown_max < cooldown_min:
                logger.warning(
                    "Цепочка '%s': cooldownMaxSeconds < cooldownMinSeconds, меняю местами.",
                    chain_name,
                )
                cooldown_min, cooldown_max = cooldown_max, cooldown_min

        chains.append({
            # id из приложения — по нему живая перезагрузка (bot.request_chains_reload)
            # переносит кулдаун цепочки, даже если её переименовали.
            "id": raw_chain.get("id") or chain_name,
            "name": chain_name,
            "sequence": sequence,
            "gamepad_steps": gamepad_steps,
            "cast_times": cast_times,
            "cooldown_min": cooldown_min,
            "cooldown_max": cooldown_max,
            "next_ready_at": 0.0,
        })

    logger.info(
        "Конфиг цепочек: загружено %d цепочек(и) из '%s'.", len(chains), path
    )
    return chains


if __name__ == "__main__":
    # Песочница: парсит конфиг БЕЗ геймпада и без игры — та же идея, что
    # была раньше, просто теперь печатает цепочки, а не отдельные скиллы.
    # os/sys уже импортированы вверху файла (нужны там для sys.path.insert).
    logging.basicConfig(level=logging.INFO)

    default_path = os.path.join(
        os.path.dirname(__file__), "..", "config", "skills_config.json"
    )
    path = sys.argv[1] if len(sys.argv) > 1 else default_path

    print(f"Проверяю '{path}'...")
    result = load_skills_config(path)
    for chain in result:
        print(
            f"  '{chain['name']}': {chain['sequence']} "
            f"(кулдаун {chain['cooldown_min']}-{chain['cooldown_max']}с)"
        )
    print(f"Итого валидных цепочек: {len(result)}")