"""Логика методологии v0.4 без интерфейса и без ИИ.

Все функции чистые: получают данные и библиотеку, возвращают результат.
ИИ подключается отдельно (ai_roles.py); здесь — правила и запасные варианты.
Ссылки в комментариях — на разделы методологии (07_Методология).
"""

import random
import re

SEED_RANK = {"редко": 0, "средне": 1, "часто": 2}
TOOL_TYPES = ["assumptions", "chain", "operator", "constraint", "contradiction", "crossing", "reframe"]
TOOL_NAMES = {
    "assumptions": "Допущения задачи",
    "chain": "Взлом причинной цепочки",
    "operator": "Оператор преобразования",
    "constraint": "Ограничение",
    "contradiction": "Противоречие",
    "crossing": "Скрещивание механизмов",
    "reframe": "Перевёрнутая задача",  # v0.5, C1 §4: цель за целью
}
DIAG_TOOL = {"Д0": "assumptions", "Д1": "operator", "Д2": "assumptions", "Д3": "chain",
             "Д4": "contradiction", "Д5": "crossing", "Д—": "constraint"}
DIAG_UI = {"Д0": "UI-049", "Д1": "UI-050", "Д2": "UI-052", "Д3": "UI-053",
           "Д4": "UI-054", "Д5": "UI-055", "Д—": "UI-056"}
ALTERNATIVES = {"assumptions": ["operator", "reframe"], "operator": ["constraint", "assumptions"],
                "chain": ["assumptions", "reframe"], "constraint": ["operator", "assumptions"],
                "contradiction": ["constraint", "operator"], "crossing": ["operator", "constraint"],
                "reframe": ["assumptions", "operator"]}
WHY_UI = {"assumptions": "UI-062", "chain": "UI-064", "operator": "UI-070", "contradiction": "UI-071",
          "crossing": "UI-072", "constraint": "UI-065", "reframe": "UI-175"}
CONSTRAINT_WHY = {"family_ban": "UI-066", "sub_ban": "UI-066", "time": "UI-067", "budget": "UI-067",
                  "scale": "UI-067", "antipode": "UI-068", "transfer": "UI-069"}

STOP = set("и в во на с со к о об от до по за из у не ни как что это для без при а но или ли же бы то так "
           "вы ваш ваша ваше ваши вас вам мы я он она они их его если чтобы когда где кто чем тем уже ещё еще "
           "только все всё всех один одна одно сам сама сами свой своя свои".split())


def stems(text):
    words = re.findall(r"[а-яёa-z0-9]+", (text or "").lower().replace("ё", "е"))
    return {w[:5] for w in words if len(w) >= 4 and w not in STOP}


# ---------- Классификация (A2, §2.2) ----------

def classify_by_dictionary(lib, text):
    code, _ = lib.classifier.classify(text)
    if not code:
        return None
    return {"family": code[:3], "subfamily": code if "." in code else None,
            "source": "dict", "confidence": "medium", "explanation": None}


# ---------- Облако очевидного (A2, §2.3) ----------

MIN_RESPONDENTS = 30
COMMON_SHARE = 0.25


def common_families(lib, baseline_families=(), data_shares=None, n_sessions=0):
    if data_shares is not None and n_sessions >= MIN_RESPONDENTS:
        return {f for f, share in data_shares.items() if share >= COMMON_SHARE}, "data"
    base = set(baseline_families)
    common = {c for c, f in lib.families.items()
              if f["seed"] == "часто" or (f["seed"] == "средне" and c in base)}
    return common, "seed"


# ---------- Конфликты с карточкой ресурсов (LIB-16, правила П1–П7) ----------

R06_FAMILIES = {
    "писать и звонить незнакомым": ["F05"],
    "появляться в видео и на публике": ["F01.3", "F02.3", "F12.1", "F12.3"],
    "вести соцсети регулярно": ["F01"],
    "давать скидки": ["F07"],
    "работать бесплатно даже в начале": ["F06"],
}
R12_FAMILIES = {
    "без скидок": ["F07"],
    "без платной рекламы": ["F02.1"],
    "без холодных контактов": ["F05.1"],
    "без публичности": ["F01", "F02.3", "F12"],
}
BAN_WORDS = {"скид": ["F07"], "акци": ["F07.1"], "реклам": ["F02.1"], "холодн": ["F05.1"],
             "публичн": ["F01", "F02.3", "F12"], "соцсет": ["F01"], "бесплатн": ["F06"]}
SKILL_OPTION = [("тексты", "писать тексты"), ("видео", "снимать и монтировать видео"),
                ("выступать", "выступать публично"), ("продавать", "продавать и вести переговоры"),
                ("программировать", "программировать"), ("организовывать", "организовывать события"),
                ("дизайн", "дизайн и визуал")]


def _matches(code, targets):
    return any(code == t or code.startswith(t + ".") or (len(t) == 3 and code[:3] == t) for t in targets)


def hard_banned(code, card, task_bans=""):
    """П5–П7 и запреты задачи: семейство нельзя предлагать (это запрет пользователя)."""
    reasons = []
    for option in card.get("R06") or []:
        if _matches(code, R06_FAMILIES.get(option, [])):
            reasons.append(("П5", f"вы не хотите: {option}"))
    for option in card.get("R12") or []:
        if _matches(code, R12_FAMILIES.get(option, [])):
            reasons.append(("П6", option))
    bans = (task_bans or "").lower()
    for word, targets in BAN_WORDS.items():
        if word in bans and ("без" in bans or "нельзя" in bans or "не " in bans) and _matches(code, targets):
            reasons.append(("П6", f"в задаче: {task_bans.strip()}"))
            break
    if card.get("R15") == "только онлайн" and _matches(code, ["F11", "F13.4", "F14.1"]):
        reasons.append(("П7", "вы работаете только онлайн"))
    return reasons


def resource_conflicts(lib, code, card):
    """П1–П4: семейство требует ресурса, которого нет. Материал для противоречия (Д4)."""
    fam = lib.families.get(code[:3]) if code else None
    if not fam:
        return []
    res, out = fam["res"], []
    if card.get("R01") in ("0 ₽", "до 5 тыс. ₽") and res["Д"] in ("С", "В"):
        out.append(("П1", "B01", "нужны деньги, а бюджета почти нет"))
    r02 = card.get("R02")
    if res["В"] == "В" and (r02 == "меньше 2 ч" or (r02 == "2–5 ч" and card.get("R03") == "за неделю")):
        out.append(("П2", "B02", "нужно много времени, а его мало"))
    if card.get("R04") == "только я" and res["К"] in ("С", "В"):
        out.append(("П3", "B03", "нужна команда, а вы один"))
    skills = card.get("R05")
    if skills:
        need = lib.skill_needs.get(code) or lib.skill_needs.get(code[:3])
        if need:
            needed = [opt for word, opt in SKILL_OPTION if word in need]
            if needed and not any(opt in skills for opt in needed):
                out.append(("П4", "B12", f"нужен навык: {need}"))
    return out


# ---------- Навигатор (A2, §2.4) ----------

def navigator(lib, used, card, common, history=(), task_bans="", k=4):
    used_f = {u[:3] for u in used if u}
    history_f = {h[:3] for h in history if h}
    candidates = [c for c in lib.family_order
                  if c not in used_f and not hard_banned(c, card, task_bans)]
    candidates.sort(key=lambda c: (c in common, c in history_f,
                                   SEED_RANK.get(lib.families[c]["seed"], 1), lib.family_order.index(c)))
    picked = []
    for c in candidates:
        if any(c in lib.families[p]["neighbors"] for p in picked):
            continue
        picked.append(c)
        if len(picked) == k:
            break
    for c in candidates:  # если соседство не дало набрать k
        if len(picked) == k:
            break
        if c not in picked:
            picked.append(c)
    return picked


# ---------- Профиль привычек (A2, §2.6) ----------

def favorite_family(session_families):
    """session_families: список множеств семейств первых идей по завершённым сессиям."""
    n = len(session_families)
    if n < 5:
        return None
    counts = {}
    for fams in session_families:
        for f in fams:
            counts[f] = counts.get(f, 0) + 1
    if not counts:
        return None
    fam, c = max(counts.items(), key=lambda kv: kv[1])
    return (fam, c, n) if c / n >= 0.4 else None


# ---------- Диагностика (B2, §4) ----------

def diagnose(ideas, dont_know=False, fav_conflict=False, level=1, completed=0):
    """ideas: [{family, common, is_favorite}]. Возвращает (код, признаки)."""
    n = len(ideas)
    rec = [i for i in ideas if i.get("family")]
    fams = [i["family"] for i in rec]
    fam_set = set(fams)
    n_common = sum(1 for i in rec if i.get("common"))
    fav = next((i for i in ideas if i.get("is_favorite")), None)
    fav_common = bool(fav and fav.get("family") and fav.get("common"))
    rare = [i for i in rec if not i.get("common")]
    rare_pair = len({i["family"] for i in rare}) >= 2
    feats = {"n_ideas": n, "n_recognized": len(rec), "n_families": len(fam_set), "n_common": n_common,
             "fav_common": fav_common, "fav_conflict": fav_conflict, "rare_pair": rare_pair}
    if n < 2 or (len(rec) < 2 and dont_know):
        return "Д0", feats
    repeated = any(fams.count(f) >= 2 for f in fam_set)
    if rec and (len(fam_set) <= 1 or (repeated and n_common == len(rec))):
        return "Д1", feats
    if fav_common and len(fam_set) >= 2:
        return "Д3", feats
    if len(fam_set) >= 2 and n_common >= 2:
        return "Д2", feats
    if n_common <= 1 and fav_conflict:
        return "Д4", feats
    if level >= 2 and completed >= 3 and rare_pair:
        return "Д5", feats
    return "Д—", feats


def choose_tool_type(diagnosis, history=()):
    """history: [(tool_type, shift)] последних сессий, новые в конце (персонализация, A2 §2.7)."""
    tool = DIAG_TOOL[diagnosis]
    last = list(history)[-2:]
    if len(last) == 2 and all(t == tool for t, _ in last) and not any(s for _, s in last):
        return ALTERNATIVES[tool][0], True
    return tool, False


# ---------- Дозировка (B3, §6) ----------

def start_strength(strengths, tool, diagnosis):
    if diagnosis == "Д0":
        return 1
    return max(1, min(3, int(strengths.get(tool, 2))))


def next_strengths(strengths, tool, softened=False, too_weak=False):
    new = dict(strengths)
    current = int(new.get(tool, 2))
    if softened:
        current = max(1, current - 1)
    elif too_weak:
        current = min(3, current + 1)
    new[tool] = current
    return new


# ---------- Выбор элементов библиотек (A2, §2.8) ----------

def _fits(code, targets):
    return any(code[:3] == t[:3] for t in targets)


def pick_assumptions(lib, families, in_domain, exclude, rng, k=4):
    def score(a):
        s = 0
        if a["applies_all"]:
            s += 1
        if any(_fits(f, a["applies"]) for f in families):
            s += 2
        if in_domain and not a["universal"]:
            s += 1
        return s
    pool = [a for a in lib.assumptions if a["id"] not in exclude] or list(lib.assumptions)
    pool.sort(key=lambda a: -score(a))
    best = score(pool[0])
    top = [a for a in pool if score(a) >= best]
    if len(top) < k:
        top = pool[: max(k * 5, k)]
    return rng.sample(top, min(k, len(top)))


def pick_chain(lib, fav_sub, fav_family, exclude):
    conditions = (lambda c: c["sub"] == fav_sub, lambda c: c["sub"][:3] == fav_family)
    for cond in conditions:
        for c in lib.chains:
            if cond(c) and c["id"] not in exclude:
                return c
    for cond in conditions:  # всё уже показывали — повторяем лучшее совпадение
        for c in lib.chains:
            if cond(c):
                return c
    return lib.chains[0]


def pick_operator(lib, target, strength, exclude, rng):
    def ok(o):
        return o["id"] not in exclude
    for cond in (lambda o: target and _fits(target, o["leads_to"]), lambda o: o["any"], lambda o: True):
        pool = [o for o in lib.operators if ok(o) and cond(o)]
        if pool:
            best = min(abs(o["strength"] - strength) for o in pool)
            return rng.choice([o for o in pool if abs(o["strength"] - strength) == best])
    return rng.choice(lib.operators)


def pick_constraint(lib, strength, rng, exclude, personal_family=None, personal_sub=None):
    if personal_family:
        fam = lib.families[personal_family]
        if strength <= 1 and personal_sub:
            items = [c for c in lib.constraints if c["type"] == "sub_ban" and personal_sub in c["families"]]
        elif strength >= 3:
            items = [c for c in lib.constraints if c["type"] == "family_ban" and len(c["families"]) == 2
                     and c["families"][0] == personal_family]
        else:
            items = [c for c in lib.constraints if c["type"] == "family_ban" and c["families"] == [personal_family]]
        if items:
            return dict(items[0], personal=True, family_name=fam["name"])
    kinds = ["behavior", "transfer", "antipode", "time", "scale", "budget", "channel", "resource"]
    pool = [c for c in lib.constraints if c["type"] in kinds and c["strength"] == strength
            and not c["not_for"] and c["id"] not in exclude]
    if not pool:
        pool = [c for c in lib.constraints if c["type"] in kinds and not c["not_for"]]
    return dict(rng.choice(pool), personal=False)


FAMILY_REQUIREMENTS = {
    "F01": ["A01", "A11", "A13"], "F02": ["A01", "A05"], "F03": ["A07", "A05"], "F04": ["A07", "A02"],
    "F05": ["A14", "A03"], "F06": ["A08", "A02"], "F07": ["A03", "A06"], "F08": ["A10", "A02"],
    "F09": ["A12", "A19"], "F10": ["A01", "A05"], "F11": ["A01", "A19"], "F12": ["A15", "A14"],
    "F13": ["A04", "A19"], "F14": ["A16", "A05"], "F15": ["A15", "A19"],
}


def pick_contradiction(lib, fav_family, conflicts, strength, exclude):
    a_codes = FAMILY_REQUIREMENTS.get(fav_family or "", [])
    b_codes = [b for _, b, _ in conflicts]
    pairs = [k for k in lib.contradictions if not k["general"] and k["id"] not in exclude]
    first = (next((k for k in pairs if k["a"] in a_codes and k["b"] in b_codes), None)
             or next((k for k in pairs if k["b"] in b_codes and k["b"].startswith("B")), None)
             or next((k for k in pairs if k["a"] in a_codes), None)
             or pairs[0])
    second = None
    if strength >= 3:
        second = next((k for k in pairs if k["a"] == first["a"] and k["b"] != first["b"]
                       and k["b"].startswith("B")), None)
    return first, second


def contradiction_text(lib, first, second=None, weakened=False):
    a = lib.side_text(first["a"]).lower()
    b = lib.side_text(first["b"]).lower()
    text = (f"Нужно: {a}. Но: {b}. Обычно это мешает друг другу, потому что "
            f"{first['conflict'][0].lower() + first['conflict'][1:]}.")
    if second:
        text += f" И при этом: {lib.side_text(second['b']).lower()}."
    text += " Найдите решение, в котором выполняются оба условия. Не жертвуйте ни одним."
    if weakened:
        text += f" Облегчение: {first['weaken'][0].lower() + first['weaken'][1:]}."
    return text


def pick_crossing(lib, idea_a, idea_b, exclude):
    tpl = next((x for x in lib.crossings if x["id"] not in exclude), lib.crossings[0])
    ma = lib.families[idea_a["family"]]["mechanism"].lower()
    mb = lib.families[idea_b["family"]]["mechanism"].lower()
    return tpl, tpl["template"].replace("[мА]", f"«{ma}»").replace("[мБ]", f"«{mb}»")


# ---------- Подстановка слотов без ИИ ----------

# Падеж слота [клиент] по предыдущему слову (проверено на всех шаблонах LIB-06 и LIB-08).
_GEN_PREP = {"у", "для", "от", "без", "до", "из", "около", "возле", "кроме", "за",
             "жизни", "действие", "пугает", "видит", "заставить", "убедить", "удивить"}
_DAT_PREP = {"к", "по", "заплатить", "помочь", "дать", "показать", "предложить"}
_INS_PREP = {"с", "со", "над", "перед"}


_CLIENT_FORMS = {"клиента": "вашего клиента", "клиенту": "вашему клиенту", "клиентом": "вашим клиентом",
                 "клиенте": "вашем клиенте", "клиенты": "ваши клиенты", "клиентов": "ваших клиентов",
                 "клиентам": "вашим клиентам", "клиентами": "вашими клиентами"}


def fill_slots(template, values):
    def client(m):
        if m.group(1) in _CLIENT_FORMS:
            return _CLIENT_FORMS[m.group(1)]
        before = template[: m.start()].split()
        prev = before[-1].lower().strip(",«»") if before else ""
        if prev in _GEN_PREP:
            return "вашего клиента"
        if prev in _DAT_PREP:
            return "вашему клиенту"
        if prev in _INS_PREP or (prev == "и" and "между" in [w.lower() for w in before[-4:]]):
            return "вашим клиентом"
        return "ваш клиент"
    text = re.sub(r"\[(клиент[а-я]*)\]", client, template)
    for key, value in values.items():
        if value:
            if key == "цель":  # «достичь [цель]» → «достичь цели «…»», иначе подставляем как есть
                text = re.sub(r"(достичь|добиться) \[цель\]", lambda m: f"{m.group(1)} цели «{value}»", text)
            text = text.replace(f"[{key}]", value)
    return text


def has_open_slots(text):
    return bool(re.search(r"\[[^\]]+\]", text))


# ---------- Подсказки (B4 §1, LIB-08) ----------

HINT_KINDS = ("question", "transfer", "fragment")
HINT_NAMES = {"question": "Вопрос", "transfer": "Перенос", "fragment": "Фрагмент"}
HINT_BUDGET = {1: 3, 2: 3, 3: 2}  # B5 §2.3: на уровне 3 бюджет 2
FRAGMENT_BY_OPERATOR = {"Участники": "Участники", "Время": "Моменты", "Среда": "Места и среды",
                        "Обмен": "Ресурсы и остатки", "Масштаб": "Ресурсы и остатки", "Смысл": "Мотивы и эмоции",
                        "Структура": "Моменты", "Обратная": "Участники"}
TRANSFER_KEYS = [("Возврат клиента", r"верн|повтор|постоян|снова"),
                 ("Рекомендация", r"рекоменд|сарафан|посовет"),
                 ("Доверие к новичку", r"довер|новичк|без опыта|начинающ"),
                 ("Сообщество", r"сообществ|клуб"),
                 ("Отличие от похожих", r"конкурент|отлич|выдел"),
                 ("Удержание внимания", r"внимани|заметн"),
                 ("Малый ресурс", r"бюджет|без денег|бесплатн"),
                 ("Повод прийти сейчас", r"срочн|сейчас|быстр"),
                 ("Первый визит без рекламы", r"нов|перв|найти|привлеч")]


def hint_budget(level):
    return HINT_BUDGET.get(level, 3)


def _group_key(group):
    return (group or "").split()[0] if group else ""


def hint_candidates(lib, kind, tool, task_text, rng):
    """Шаблоны LIB-08 в порядке предпочтения для данного инструмента (без учёта показанных)."""
    ttype = tool.get("type")
    op_group = ""
    if ttype == "operator" and tool.get("item_id"):
        op = next((o for o in lib.operators if o["id"] == tool["item_id"]), None)
        op_group = _group_key(op["group"]) if op else ""
    if kind == "question":
        own = [h for h in lib.hints["question"] if h["for"] == ttype
               and (ttype != "operator" or _group_key(h["group"]) == op_group)]
        if ttype == "operator" and not own:
            own = [h for h in lib.hints["question"] if h["for"] == "operator"]
        general = [h for h in lib.hints["question"] if h["for"] == "general"]
        rng.shuffle(own)
        rng.shuffle(general)
        return own + general
    if kind == "transfer":
        low = (task_text or "").lower()
        functions = [f for f, rx in TRANSFER_KEYS if re.search(rx, low)]
        first = [h for h in lib.hints["transfer"] if h["function"] in functions[:2]]
        rest = [h for h in lib.hints["transfer"] if h not in first]
        rng.shuffle(first)
        rng.shuffle(rest)
        return first + rest
    group = None
    if ttype == "operator":
        group = FRAGMENT_BY_OPERATOR.get(op_group)
    elif ttype == "constraint":
        group = "Ограничение как ресурс"
    first = [h for h in lib.hints["fragment"] if h["group"] == group]
    rest = [h for h in lib.hints["fragment"] if h["group"] != group]
    rng.shuffle(first)
    rng.shuffle(rest)
    return first + rest


def pick_hint(lib, kind, tool, task_text, slot_values, exclude, rng):
    """Первый шаблон, у которого все слоты заполнены; показанные раньше — в конец очереди.

    Возвращает (шаблон, текст с подставленными слотами).
    """
    candidates = hint_candidates(lib, kind, tool, task_text, rng)
    fresh = [h for h in candidates if h["id"] not in exclude]
    ban = slot_values.get("запрет")
    values = {k: v for k, v in slot_values.items() if k != "запрет"}
    for h in fresh + [h for h in candidates if h["id"] in exclude]:
        template = h["text"]
        if ban:  # без ИИ запрет грамматически встаёт только после «без»
            template = template.replace("без [запрет]", f"без {ban}")
        text = fill_slots(template, values)
        if not has_open_slots(text):
            return h, text
    return None, None


_LIST_LINE = re.compile(r"^\s*(?:[-–—•*]|\d+[.)])\s+")


def split_ideas(text):
    """Несколько идей в одном поле (B4 §1.12): нумерованный или маркированный список из 2+ пунктов."""
    lines = [l for l in (text or "").splitlines() if l.strip()]
    items = [_LIST_LINE.sub("", l).strip() for l in lines if _LIST_LINE.match(l)]
    if len(items) >= 2 and len(items) >= len(lines) - 1:
        return [i for i in items if i]
    return [text.strip()]


# ---------- Момент открытия (методология v0.5, C1) ----------

LENS_FALLBACK = {"system": "UI-170", "behavior": "UI-171", "value": "UI-172"}
MAX_VERSIONS = 4  # версий идеи за сессию (эволюция идеи, C1 §2)

# Лестница выхода из тупика (C1 §5), ступень 3: какую часть решения менять → группы операторов LIB-06
LADDER_OPERATIONS = {"участника": ("Участники",), "порядок шагов": ("Время", "Структура"),
                     "источник ценности": ("Обмен",), "условие задачи": ("Смысл", "Масштаб", "Среда")}


def fallback_shift(final_family, base_families):
    """Без ИИ детектор сдвига сравнивает только семейства: другое — «механизм», то же — «форма»."""
    s = shift(final_family, base_families)
    if s is None:
        return None
    return {"shift": "механизм" if s else "форма", "changed": ["механизм"] if s else [], "same_as": None,
            "explanation": "", "source": "rules"}


def novelty_level(shift_kind, rare):
    """Ось 1: новизна механизма относительно своих первых идей (+ редкость направления)."""
    if shift_kind == "механизм":
        return "высокая" if rare else "средняя"
    if shift_kind in ("форма", "нет"):
        return "низкая"
    return None


def feasibility_level(rubric):
    """Ось 2: реализуемость по рубрике (связь с целью, ресурсы, первый шаг)."""
    levels = [rubric[k]["level"] for k in ("RB1", "RB2", "RB3") if k in (rubric or {})]
    if not levels:
        return None
    if min(levels) == 1:
        return "низкая"
    return "высокая" if sum(levels) / len(levels) >= 2.67 else "средняя"


def idea_type(shift_kind, novelty, feasibility):
    """Тип идеи на двух шкалах (C1 §7) → (UI-текст, что доработать или None)."""
    if shift_kind == "форма":
        return "UI-198", "mechanism"
    if novelty in ("высокая", "средняя"):
        if feasibility == "низкая":
            return "UI-196", "feasibility"
        if feasibility in ("высокая", "средняя"):
            return "UI-195", None
        return None, None
    if novelty == "низкая" and feasibility != "низкая":
        return "UI-197", "mechanism"
    return None, "mechanism" if novelty == "низкая" else None


def pick_ladder_operator(lib, operation, exclude, rng):
    groups = LADDER_OPERATIONS.get(operation, ())
    pool = [o for o in lib.operators if o["group"].split()[0] in groups and o["id"] not in exclude] or \
           [o for o in lib.operators if o["group"].split()[0] in groups]
    return rng.choice(pool) if pool else None


def mechanism_profile(families, top=3):
    """Профиль привычных механизмов по первым идеям нескольких сессий (C1 §8): самые частые семейства."""
    counts = {}
    for f in families:
        if f:
            counts[f[:3]] = counts.get(f[:3], 0) + 1
    return sorted(counts.items(), key=lambda x: -x[1])[:top]


# ---------- Показатели (A4, §2) ----------

def shift(final_family, first_families):
    if not final_family or not any(first_families):
        return None
    return final_family not in {f for f in first_families if f}


def authorship(final_text, hint_texts):
    words = stems(final_text)
    if not words:
        return None
    from_hints = set()
    for h in hint_texts:
        from_hints |= stems(h)
    return round(1 - len(words & from_hints) / len(words), 2)


def checkpoint_metrics(classified, common):
    """classified: [{text, family}] в порядке написания. Индекс первой мысли (A4, §2.8)."""
    fams = [c["family"] for c in classified if c.get("family")]
    rare = {f for f in fams if f not in common}
    first_uncommon = next((i for i, c in enumerate(classified, 1)
                           if c.get("family") and c["family"] not in common), None)
    n = len(classified)
    return {"n_ideas": n, "flexibility": len(set(fams)), "rare_families": len(rare),
            "common_share": round(sum(1 for f in fams if f in common) / n, 2) if n else None,
            "first_uncommon_pos": first_uncommon, "n_unknown": n - len(fams)}


def trend(points):
    """points: список ИПМ по контрольным точкам. Вывод — только при 3+ точках (A4, §2.8)."""
    if len(points) < 3:
        return "few"
    later = sum(points[-2:]) / 2
    return "up" if later - points[0] >= 2 else "flat"


# ---------- Снятие опор (B5, §2) ----------

def next_level(level, sessions):
    """sessions: [(level, shift, hints)] завершённых сессий, новые в конце."""
    same = [s for s in sessions if s[0] == level]
    last4 = same[-4:]
    if level < 3 and len(last4) >= 3 and sum(1 for _, sh, h in last4 if sh and h <= 1) >= 3:
        return level + 1, "rule_up"
    last2 = same[-2:]
    if level > 1 and len(last2) == 2 and all(sh is False for _, sh, _ in last2):
        return level - 1, "rule_down"
    return level, None


# ---------- Пре-мортем без ИИ (B4, §3) ----------

CATEGORY_WORDS = {
    "К1": ["нужн", "интерес", "спрос", "не захот"], "К2": ["не увид", "не узна", "охват", "канал", "не найд"],
    "К3": ["довер", "не повер", "новичк"], "К4": ["не пойм", "непонят", "не замет"],
    "К5": ["врем", "денег", "деньг", "не хват", "ресурс"], "К6": ["не смогу", "не умею", "лень", "не успею сделать"],
    "К7": ["окуп", "дорог", "цен"], "К8": ["успе", "долго", "срок", "поздно"],
    "К9": ["партн", "площадк", "блогер", "зависит"], "К10": ["закон", "правил", "репутац", "штраф"],
}
RULE_CATEGORY = {"П1": "К5", "П2": "К5", "П3": "К5", "П4": "К6"}
FAMILY_CATEGORY = {"F01": "К2", "F02": "К9", "F03": "К9", "F04": "К3", "F05": "К3", "F06": "К7",
                   "F07": "К7", "F08": "К7", "F09": "К4", "F10": "К2", "F11": "К2", "F12": "К6",
                   "F13": "К6", "F14": "К9", "F15": "К4"}


def _user_covers(category, reasons):
    text = " ".join(reasons).lower()
    return any(w in text for w in CATEGORY_WORDS.get(category, []))


def _objection_score(item, idea_stems, card, wanted_fields):
    """Чем ближе возражение к словам идеи и к заполненной карточке, тем выше (LIB-09: «применимы к механизму»)."""
    score = 3 * len(idea_stems & stems(item["condition"] + " " + item["question"]))
    link = item["link"]
    if link in wanted_fields:
        score += 4
    elif link.startswith("R"):
        score += 1 if card.get(link) else -2  # привязка к полю, которого человек не заполнял, — слабая
    return score


def fallback_objections(lib, final_family, card, user_reasons, rng, k=3, idea_text=""):
    conflicts = resource_conflicts(lib, final_family, card) if final_family else []
    idea_stems = stems(idea_text)
    order = [RULE_CATEGORY[r] for r, _, _ in conflicts if r in RULE_CATEGORY]
    if final_family in FAMILY_CATEGORY:
        order.append(FAMILY_CATEGORY[final_family])
    order += ["К1", "К4", "К8", "К3", "К2", "К7", "К9", "К6", "К5", "К10"]
    chosen, cats = [], []
    conflict_fields = {"П1": "R01", "П2": "R02", "П3": "R04", "П4": "R05"}
    wanted_fields = {conflict_fields[r] for r, _, _ in conflicts if r in conflict_fields}
    for cat in order:
        if cat in cats or _user_covers(cat, user_reasons):
            continue
        items = [f for f in lib.failures if f["category"] == cat]
        scores = {f["id"]: _objection_score(f, idea_stems, card, wanted_fields) for f in items}
        best = max(scores.values())
        item = rng.choice([f for f in items if scores[f["id"]] == best])
        condition = item["condition"].replace("[канале]", "выбранном канале")
        chosen.append({
            "lib_id": item["id"], "category": cat, "category_name": item["category_name"],
            "card_field": item["link"] if item["link"].startswith("R") else None,
            "mechanism_link": None if item["link"].startswith("R") else "механизм идеи",
            "condition": f"Не сработает, если {condition}.",
            "check": next((c["action"] for c in lib.checks if c["id"] == item["check"]), ""),
            "check_id": item["check"], "question": item["question"],
            "severity": 3 if item["link"] in wanted_fields else 2,
        })
        cats.append(cat)
        if len(chosen) == k:
            break
    return chosen


INSISTENCE = ["точно", "уверен", "не проблема", "всё будет", "все будет", "сработает", "нет"]


def fallback_verdict(response_type, text):
    text = (text or "").strip()
    low = text.lower()
    if response_type == "agree":
        return {"verdict": "в силе", "basis": "нет", "reason": "Риск принят."}
    if response_type == "change":
        if len(text) >= 20:
            return {"verdict": "ослаблено", "basis": "изменение идеи",
                    "reason": "Идея изменена. Без ИИ мы не можем проверить, устранено ли условие полностью."}
        return {"verdict": "в силе", "basis": "нет", "reason": "Изменение описано слишком коротко."}
    if response_type == "fact":
        if len(text) >= 15:
            return {"verdict": "ослаблено", "basis": "факт", "reason": "Факт принят, но не проверен."}
        return {"verdict": "в силе", "basis": "нет", "reason": "Факт описан слишком коротко."}
    only_insist = len(text) < 40 or (len(stems(text)) <= 3 and any(w in low for w in INSISTENCE))
    if only_insist:
        return {"verdict": "в силе", "basis": "нет", "reason": "нового довода в ответе нет"}
    return {"verdict": "ослаблено", "basis": "аргумент", "reason": "Есть довод, но без ИИ он не проверен."}


def strongest_open(objections):
    open_ = [o for o in objections if o.get("verdict") == "в силе"] or \
            [o for o in objections if o.get("verdict") == "ослаблено"]
    if not open_:
        return None
    return max(open_, key=lambda o: (o.get("severity") or 2, o.get("about_me") == 1))


def check_for(lib, objection):
    if objection and objection.get("check_id"):
        c = next((c for c in lib.checks if c["id"] == objection["check_id"]), None)
        if c:
            return c
    cat = (objection or {}).get("category")
    return next((c for c in lib.checks if cat in c["categories"]), lib.checks[0])


def rng_for(*parts):
    return random.Random("|".join(str(p) for p in parts))
