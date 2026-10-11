"""Предварительная автоматическая оценка входного теста.

Идеи относятся к «семействам решений» по словарю ключевых слов для одной
стандартной задачи. Это грубая замена будущей классификации с помощью LLM:
она прозрачна, бесплатна и работает без сети, но ошибается на непривычных
формулировках. Нераспознанные идеи не считаются ни банальными, ни оригинальными.
"""

import json
import re
from pathlib import Path

MAX_IDEAS = 30
MAX_IDEA_LEN = 300
# С какого числа завершённых тестов «облако очевидного» считается по реальным
# данным, а не по стартовому предположению команды.
MIN_RESPONDENTS_FOR_DATA = 30
COMMON_SHARE = 0.25

_BULLET = re.compile(r"^\s*(?:[-–—•*·]+|\d+[.)]|\(\d+\))\s*")


def normalize(text):
    return text.lower().replace("ё", "е")


def load_task(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _keyword_pattern(keyword):
    """Слово с «$» в конце (и любое слово до двух букв) — целое слово, иначе — начало слова."""
    parts = []
    for token in normalize(keyword).split():
        whole = token.endswith("$") or len(token.rstrip("$")) <= 2
        token = token.rstrip("$")
        parts.append(re.escape(token) + (r"(?!\w)" if whole else r"\w*"))
    return re.compile(r"(?<!\w)" + r"\s+".join(parts))


class Classifier:
    def __init__(self, families):
        self.families = families
        self._patterns = [
            [(_keyword_pattern(kw), len(kw.rstrip("$"))) for kw in fam["keywords"]]
            for fam in families
        ]

    def classify(self, text):
        """Возвращает (основное семейство или None, все найденные семейства).

        Совпадение, целиком лежащее внутри более длинного, не считается
        (например, «бизнес-ланч» важнее «бизнес»). Из оставшихся основным
        становится семейство с наибольшим числом совпадений, при равенстве —
        стоящее раньше в списке.
        """
        norm = normalize(text)
        matches = []
        for index, patterns in enumerate(self._patterns):
            for pattern, key_len in patterns:
                for m in pattern.finditer(norm):
                    matches.append((index, m.start(), m.end(), key_len))
        # Совпадение отбрасывается, если другое накрывает его и длиннее —
        # по тексту или, при одинаковом охвате, по самому ключу
        # («подарочн» специфичнее, чем «подар», хотя оба покрывают слово целиком).
        kept = [
            (i, s, e)
            for (i, s, e, k) in matches
            if not any(
                s2 <= s and e <= e2 and ((e2 - s2) > (e - s) or ((e2 - s2) == (e - s) and k2 > k))
                for (_, s2, e2, k2) in matches
            )
        ]
        counts = {}
        for (i, _, _) in kept:
            counts[i] = counts.get(i, 0) + 1
        if not counts:
            return None, []
        # Иерархия «F01.2»: голоса подсемейств складываются в голос семейства
        # (часть id до точки). Для плоских id без точки это ничего не меняет.
        family_votes = {}
        for i, c in counts.items():
            fam = self.families[i]["id"].split(".")[0]
            votes, first = family_votes.get(fam, (0, i))
            family_votes[fam] = (votes + c, min(first, i))
        best_family = min(family_votes, key=lambda f: (-family_votes[f][0], family_votes[f][1]))
        members = [i for i in counts if self.families[i]["id"].split(".")[0] == best_family]
        primary = min(members, key=lambda i: (-counts[i], i))
        ids = [self.families[i]["id"] for i in sorted(counts)]
        return self.families[primary]["id"], ids


def parse_ideas(raw):
    ideas = []
    for line in (raw or "").splitlines():
        line = _BULLET.sub("", line).strip()
        if line:
            ideas.append(line[:MAX_IDEA_LEN])
        if len(ideas) >= MAX_IDEAS:
            break
    return ideas


def classify_ideas(classifier, ideas):
    result = []
    for text in ideas:
        family, all_families = classifier.classify(text)
        result.append({"text": text, "family": family, "all": all_families})
    return result


def family_frequencies(classified_attempts):
    """Доля участников, у которых встречалось каждое семейство.

    classified_attempts — список списков классифицированных идей
    (по одному списку на завершённый тест).
    """
    n = len(classified_attempts)
    counts = {}
    for ideas in classified_attempts:
        for fid in {idea["family"] for idea in ideas if idea["family"]}:
            counts[fid] = counts.get(fid, 0) + 1
    shares = {fid: c / n for fid, c in counts.items()} if n else {}
    avg_families = (
        sum(len({i["family"] for i in ideas if i["family"]}) for ideas in classified_attempts) / n
        if n
        else None
    )
    return n, shares, avg_families


def common_families(task, n_respondents, shares):
    if n_respondents >= MIN_RESPONDENTS_FOR_DATA:
        return {fid for fid, share in shares.items() if share >= COMMON_SHARE}, "data"
    return {f["id"] for f in task["families"] if f["seed_common"]}, "seed"


def build_result(task, classified, top2_indexes, n_respondents, shares, avg_families):
    families = {f["id"]: f for f in task["families"]}
    common, common_source = common_families(task, n_respondents, shares)

    ideas = []
    for idea in classified:
        fid = idea["family"]
        ideas.append(
            {
                "text": idea["text"],
                "family": fid,
                "family_name": families[fid]["name"] if fid else None,
                "common": bool(fid and fid in common),
            }
        )

    used = []
    for idea in ideas:
        if idea["family"] and idea["family"] not in used:
            used.append(idea["family"])

    n = len(ideas)
    common_count = sum(1 for i in ideas if i["common"])
    first_uncommon = next((pos for pos, i in enumerate(ideas, 1) if not i["common"]), None)

    halves = None
    if n >= 4:
        mid = n // 2
        first, second = ideas[:mid], ideas[mid:]
        halves = {
            "first_common_share": sum(i["common"] for i in first) / len(first),
            "second_common_share": sum(i["common"] for i in second) / len(second),
        }

    # «Незанятые пространства»: сначала редкие семейства, которых человек не коснулся.
    untouched = [f for f in task["families"] if f["id"] not in used]
    untouched.sort(key=lambda f: (f["id"] in common, shares.get(f["id"], 0)))

    family_map = []
    for f in task["families"]:
        family_map.append(
            {
                "id": f["id"],
                "name": f["name"],
                "count": sum(1 for i in ideas if i["family"] == f["id"]),
                "common": f["id"] in common,
                "share": shares.get(f["id"]) if common_source == "data" else None,
            }
        )

    return {
        "n_ideas": n,
        "n_families": len(used),
        "n_unknown": sum(1 for i in ideas if not i["family"]),
        "common_count": common_count,
        "first_is_common": ideas[0]["common"] if ideas else None,
        "first_uncommon_pos": first_uncommon,
        "halves": halves,
        "ideas": ideas,
        "top2": [ideas[i] for i in top2_indexes if 0 <= i < n],
        "untouched": [
            {"id": f["id"], "name": f["name"], "explain": f["explain"]} for f in untouched[:3]
        ],
        "family_map": family_map,
        "common_source": common_source,
        "n_respondents": n_respondents,
        "avg_families_others": avg_families if common_source == "data" else None,
    }


def default_task_path():
    return Path(__file__).parent / "data" / "task_coffee.json"
