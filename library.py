"""Загрузка библиотек содержания LIB-01…LIB-16.

Источник правды — markdown-файлы в папке проекта 08_Библиотеки (рядом с документами).
Сайт хранит их копию в site/libraries: она уходит в git-репозиторий сайта и на хостинг.
Копию обновляет sync_libraries.py; тест сверяет её с оригиналом.
Где читать: ACTKREATOR_LIBRARY_DIR, иначе оригинал (если он рядом), иначе копия.
"""

import glob
import os
import re
from pathlib import Path

import scoring

SITE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SITE_DIR.parent / "08_Библиотеки"   # оригинал — в документах проекта
BUNDLED_DIR = SITE_DIR / "libraries"               # копия внутри сайта


def default_dir():
    if os.environ.get("ACTKREATOR_LIBRARY_DIR"):
        return Path(os.environ["ACTKREATOR_LIBRARY_DIR"])
    return PROJECT_DIR if PROJECT_DIR.is_dir() else BUNDLED_DIR


DEFAULT_DIR = default_dir()
_SEPARATOR = re.compile(r"^\|[\s\-:|]+\|\s*$")


def _rows(path):
    """(заголовок h2, заголовок h3, ячейки) для каждой строки данных таблиц файла."""
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    h2 = h3 = ""
    for i, line in enumerate(lines):
        if line.startswith("## "):
            h2, h3 = line[3:].strip(), ""
            continue
        if line.startswith("### "):
            h3 = line[4:].strip()
            continue
        if not line.startswith("|") or _SEPARATOR.match(line):
            continue
        if i + 1 < len(lines) and _SEPARATOR.match(lines[i + 1]):
            continue  # строка заголовка таблицы
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        yield h2, h3, cells


def _files(directory, pattern):
    return sorted(glob.glob(str(Path(directory) / pattern)))


def _codes(text):
    return re.findall(r"F\d\d(?:\.\d)?", text or "")


_FAMILY = re.compile(r"^F\d\d$")
_SUB = re.compile(r"^F\d\d\.\d$")


class Library:
    def __init__(self, directory=DEFAULT_DIR):
        self.dir = Path(directory)
        self._load_families()
        self._load_keywords()
        self._load_assumptions()
        self._load_chains()
        self._load_constraints()
        self._load_operators()
        self._load_contradictions()
        self._load_hints()
        self._load_failures()
        self._load_checks()
        self._load_training()
        self._load_standard_tasks()
        self._load_ui()
        self._load_baselines()
        self._load_rubrics()
        self._load_card()
        self.classifier = scoring.Classifier(
            [{"id": code, "keywords": keys} for code, keys in self.keyword_rows]
        )

    # ---------- LIB-01 ----------
    def _load_families(self):
        self.families, self.subfamilies = {}, {}
        for h2, h3, c in _rows(self.dir / "LIB-01_Таксономия_семейств.md"):
            if _FAMILY.match(c[0]) and len(c) >= 10:
                self.families[c[0]] = {
                    "code": c[0], "name": c[1], "mechanism": c[2],
                    "res": {"Д": c[3], "В": c[4], "К": c[5], "Н": c[6]},
                    "seed": c[7], "neighbors": _codes(c[8]), "opposites": _codes(c[9]),
                }
            elif _SUB.match(c[0]) and len(c) >= 5:
                self.subfamilies[c[0]] = {
                    "code": c[0], "family": c[0][:3], "name": c[1], "mechanism": c[2],
                    "examples": c[3], "seed": c[4],
                }
        self.family_order = list(self.families)

    def family_of(self, code):
        return code[:3] if code else None

    def family_name(self, code):
        if not code:
            return None
        if code in self.subfamilies:
            return self.subfamilies[code]["name"]
        fam = self.families.get(code[:3])
        return fam["name"] if fam else code

    # ---------- LIB-02 ----------
    def _load_keywords(self):
        self.keyword_rows = []
        for h2, h3, c in _rows(self.dir / "LIB-02_Словари_ключевых_слов.md"):
            if re.match(r"^F\d\d(\.\d)?$", c[0]) and len(c) == 2:
                keys = [k.strip() for k in c[1].split(";") if k.strip()]
                self.keyword_rows.append((c[0], keys))

    # ---------- LIB-03 ----------
    def _load_assumptions(self):
        self.assumptions = []
        for path in _files(self.dir, "LIB-03_*.md"):
            for h2, h3, c in _rows(path):
                if re.match(r"^A\d{3}$", c[0]) and len(c) >= 5:
                    self.assumptions.append({
                        "id": c[0], "category": h2.split(".")[0], "text": c[1],
                        "challenge": c[2], "direction": c[3],
                        "universal": h2.startswith("U"), "applies": _codes(c[4]),
                        "applies_all": c[4].strip() == "все",
                    })

    # ---------- LIB-04 ----------
    def _load_chains(self):
        self.chains = []
        for path in _files(self.dir, "LIB-04_*.md"):
            for h2, h3, c in _rows(path):
                if re.match(r"^L\d{3}$", c[0]) and len(c) >= 7:
                    self.chains.append({
                        "id": c[0], "sub": c[1], "link": c[2], "assumption": c[3],
                        "q1": c[4], "q2": c[5], "q3": c[6],
                    })

    # ---------- LIB-05 ----------
    _C_TYPES = {"А": "channel", "Б": "family_ban", "В": "sub_ban", "Г": "resource",
                "Д": "time", "Е": "budget", "Ж": "scale", "З": "antipode",
                "И": "transfer", "К": "behavior"}

    def _load_constraints(self):
        self.constraints = []
        for path in _files(self.dir, "LIB-05_*.md"):
            for h2, h3, c in _rows(path):
                if not re.match(r"^C\d{3}$", c[0]):
                    continue
                kind = self._C_TYPES.get(h2[:1])
                item = {"id": c[0], "type": kind, "text": c[1], "strength": 2,
                        "not_for": "", "families": [], "note": ""}
                if kind == "sub_ban":
                    item["strength"], item["families"] = 1, [c[2]]
                elif kind == "transfer":
                    item["note"], item["strength"] = c[2], int(c[3])
                else:
                    item["strength"] = int(c[2])
                    if len(c) > 3:
                        item["not_for"] = "" if c[3] in ("—", "") else c[3]
                    if kind == "family_ban":
                        item["families"] = _codes(c[1])
                self.constraints.append(item)

    # ---------- LIB-06 ----------
    def _load_operators(self):
        self.operators, self.crossings = [], []
        path = self.dir / "LIB-06_Операторы_преобразования.md"
        for h2, h3, c in _rows(path):
            if re.match(r"^O\d\d$", c[0]) and len(c) >= 7:
                self.operators.append({
                    "id": c[0], "group": h2.split(".")[1].strip().split(" —")[0] if "." in h2 else h2,
                    "name": c[1], "template": c[2], "changes": c[3],
                    "leads_to": _codes(c[4]), "any": "любое" in c[4],
                    "strength": int(c[5]), "example": c[6],
                })
            elif re.match(r"^X\d\d$", c[0]):
                self.crossings.append({"id": c[0], "template": c[1]})

    # ---------- LIB-07 ----------
    def _load_contradictions(self):
        self.requirements, self.limits, self.contradictions = {}, {}, []
        for h2, h3, c in _rows(self.dir / "LIB-07_Противоречия.md"):
            if re.match(r"^A\d\d$", c[0]) and len(c) == 2:
                self.requirements[c[0]] = c[1]
            elif re.match(r"^B\d\d$", c[0]):
                self.limits[c[0]] = c[1]
                if len(c) >= 4 and re.match(r"^B\d\d$", c[2]):
                    self.limits[c[2]] = c[3]
            elif re.match(r"^K\d{3}$", c[0]):
                if len(c) >= 5 and re.match(r"^[AB]\d\d$", c[2]):
                    self.contradictions.append({"id": c[0], "a": c[1], "b": c[2],
                                                "conflict": c[3], "weaken": c[4], "general": False})
                elif len(c) >= 4:
                    self.contradictions.append({"id": c[0], "a": None, "b": None, "x": c[1], "y": c[2],
                                                "conflict": f"{c[1]} и одновременно {c[2]}",
                                                "weaken": c[3], "general": True})

    def side_text(self, code):
        return self.requirements.get(code) or self.limits.get(code) or code

    # ---------- LIB-08 ----------
    _Q_KIND = [("Общие", "general"), ("допущений", "assumptions"), ("взлома", "chain"),
               ("ограничений", "constraint"), ("операторов", "operator"),
               ("противоречий", "contradiction"), ("скрещивания", "crossing")]

    def _load_hints(self):
        self.hints = {"question": [], "transfer": [], "fragment": []}
        for h2, h3, c in _rows(self.dir / "LIB-08_Подсказки.md"):
            if re.match(r"^Q\d{3}$", c[0]):
                kind = next((k for word, k in self._Q_KIND if word in h3), "general")
                text = c[-1]
                group = c[1] if len(c) == 3 else None
                self.hints["question"].append({"id": c[0], "for": kind, "group": group, "text": text})
            elif re.match(r"^TR\d\d$", c[0]):
                self.hints["transfer"].append({"id": c[0], "function": c[1], "text": c[2]})
            elif re.match(r"^FR\d{3}$", c[0]):
                self.hints["fragment"].append({"id": c[0], "group": h3.split(" (")[0], "text": c[1]})

    # ---------- LIB-09 ----------
    def _load_failures(self):
        self.failures = []
        for h2, h3, c in _rows(self.dir / "LIB-09_Причины_провала.md"):
            if re.match(r"^P\d{3}$", c[0]) and len(c) >= 5:
                self.failures.append({
                    "id": c[0], "category": h2.split(".")[0], "category_name": h2.split(". ", 1)[-1],
                    "condition": c[1], "link": c[2], "check": c[3], "question": c[4],
                })

    # ---------- LIB-10 ----------
    def _load_checks(self):
        self.checks = []
        for h2, h3, c in _rows(self.dir / "LIB-10_Проверки_48_часов.md"):
            if re.match(r"^T\d\d$", c[0]) and len(c) >= 7:
                self.checks.append({
                    "id": c[0], "name": c[1], "categories": re.findall(r"К\d+", c[2]),
                    "action": c[3], "ok": c[4], "fail": c[5], "time": c[6],
                })

    # ---------- LIB-11 ----------
    def _load_training(self):
        self.training = []
        for path in _files(self.dir, "LIB-11_*.md"):
            for h2, h3, c in _rows(path):
                if re.match(r"^T\d{3}$", c[0]) and len(c) >= 5:
                    self.training.append({
                        "id": c[0], "goal": c[1], "text": c[2], "legend": parse_legend(c[3]),
                        "legend_text": c[3], "difficulty": int(c[4]),
                    })

    # ---------- LIB-12 ----------
    def _load_standard_tasks(self):
        self.standard_tasks = []
        for h2, h3, c in _rows(self.dir / "LIB-12_Стандартные_задачи.md"):
            if re.match(r"^S\d\d$", c[0]) and len(c) >= 3:
                text = re.sub(r"\s*\*\(.*?\)\*", "", c[2]).strip()
                self.standard_tasks.append({"id": c[0], "group": c[1], "text": text})

    # ---------- LIB-13 ----------
    def _load_ui(self):
        self.ui = {}
        for h2, h3, c in _rows(self.dir / "LIB-13_Тексты_интерфейса.md"):
            if re.match(r"^UI-\d{3}$", c[0]):
                self.ui[c[0]] = c[-1]

    def t(self, ui_id, **slots):
        text = self.ui.get(ui_id, ui_id)
        for key, value in slots.items():
            text = text.replace(f"[{key}]", str(value))
        return agree_numbers(text) if slots else text

    # ---------- LIB-14 ----------
    def _load_baselines(self):
        self.baselines = {}
        for h2, h3, c in _rows(self.dir / "LIB-14_Карта_банальности.md"):
            m = re.match(r"^(S\d\d)\b", c[0])
            if m and len(c) >= 3:
                answers = [{"text": t.strip(), "code": code}
                           for t, code in re.findall(r"([^;\[\]]+?)\s*\[(F\d\d(?:\.\d)?)\]", c[2])]
                self.baselines[m.group(1)] = {"cloud": _codes(c[1]), "answers": answers}

    # ---------- LIB-15 ----------
    def _load_rubrics(self):
        self.rubrics = {}
        for h2, h3, c in _rows(self.dir / "LIB-15_Рубрики_оценки.md"):
            m = re.match(r"^(RB\d)\. (.+)$", h2)
            if m and len(c) >= 4 and re.match(r"^\d", c[0]):
                r = self.rubrics.setdefault(m.group(1), {"id": m.group(1), "name": m.group(2), "levels": {}})
                r["levels"][int(c[0][0])] = {"label": c[0], "description": c[1], "anchors": [c[2], c[3]]}

    # ---------- LIB-16 ----------
    def _load_card(self):
        self.card_fields, self.skill_needs = [], {}
        for h2, h3, c in _rows(self.dir / "LIB-16_Карточка_ресурсов.md"):
            if re.match(r"^R\d\d$", c[0]) and len(c) >= 6:
                kind = c[2].lower()
                field_type = "text" if kind.startswith("текст") else ("multi" if kind.startswith("несколько") else "single")
                options = [o.strip() for o in c[3].split(";")] if field_type != "text" else []
                self.card_fields.append({
                    "id": c[0], "label": c[1], "type": field_type, "options": options,
                    "short": "Да" in c[4], "affects": c[5],
                    "with_text": "+ текст" in kind or "+ тип" in kind,
                })
            elif _codes(c[0]) and len(c) == 2 and h2.startswith("2."):
                for code in _codes(c[0]):
                    self.skill_needs[code] = c[1]


def _form(n, one, few, many):
    n = abs(n) % 100
    if 11 <= n <= 14:
        return many
    return {1: one, 2: few, 3: few, 4: few}.get(n % 10, many)


# Тексты LIB-13 написаны с родительным падежом множественного числа («[K] направлений»).
# После подстановки числа согласуем существительное: 1 направление, 2 направления, 5 направлений.
_AGREE = [
    (re.compile(r"\b(\d+) разных направлений"), ("разное направление", "разных направления", "разных направлений")),
    (re.compile(r"\b(\d+) направлений"), ("направление", "направления", "направлений")),
    (re.compile(r"\bиз (\d+) идей"), ("идеи", "идей", "идей")),
    (re.compile(r"(?<!из )\b(\d+) идей"), ("идея", "идеи", "идей")),
    (re.compile(r"\b(\d+) подходов"), ("подход", "подхода", "подходов")),
    (re.compile(r"\b(\d+) тренировок"), ("тренировка", "тренировки", "тренировок")),
    (re.compile(r"\b(\d+) сессий"), ("сессии", "сессий", "сессий")),
]


def agree_numbers(text):
    for rx, forms in _AGREE:
        def repl(m, forms=forms):
            head = m.group(0)[: m.start(1) - m.start(0)]
            n = int(m.group(1))
            return f"{head}{m.group(1)} {_form(n, *forms)}"
        text = rx.sub(repl, text)
    return text


_LEGEND = re.compile(r"(Б|В|К|П):\s*([^·]+)")


def parse_legend(text):
    """«Б: 0 ₽ · В: 5 ч · К: один · П: …» → карточка ресурсов для учебной задачи."""
    parts = {k: v.strip() for k, v in _LEGEND.findall(text)}
    budget = parts.get("Б", "")
    digits = re.sub(r"\D", "", budget)
    if digits in ("", "0"):
        r01 = "0 ₽"
    elif int(digits) <= 5000:
        r01 = "до 5 тыс. ₽"
    elif int(digits) <= 30000:
        r01 = "5–30 тыс. ₽"
    else:
        r01 = "30–100 тыс. ₽"
    hours = re.findall(r"\d+", parts.get("В", ""))
    h = int(hours[0]) if hours else 0
    r02 = "меньше 2 ч" if h < 2 else "2–5 ч" if h <= 5 else "5–10 ч" if h <= 10 else "больше 10 ч"
    team = parts.get("К", "")
    r04 = "только я" if re.search(r"\b(один|одна|мастер|владел|преподаватель|продавец|тренер|флорист|фотограф|администратор|швея|пекарь)\b", team) and "и" not in team.split() else "иногда помощник или друг"
    if re.search(r"(двое|трое|два|три|команда)", team):
        r04 = "команда 2–5 человек"
    return {"R01": r01, "R02": r02, "R04": r04, "R10": parts.get("П", ""), "team_text": team}
