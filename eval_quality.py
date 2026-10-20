"""Eval-контур качества LLM: наборы, варианты реализации («руки»), подсчёт и отчёт «до/после».

Методика — docs/llm-quality/EVAL_PLAN.md. Запуск — через eval_prompts.py (точка входа одна):

    .venv/Scripts/python eval_prompts.py compare --repeat 3          всё сразу: исходная (v1) против изменённой (v2)
    .venv/Scripts/python eval_prompts.py family --split dev --arms dict,v1,v1def,v2 --repeat 3
    .venv/Scripts/python eval_prompts.py rubric --arms v1,v2 --repeat 3
    .venv/Scripts/python eval_prompts.py cloud --offline             правила облака по сохранённому корпусу, без сети
    .venv/Scripts/python eval_prompts.py cloud --samples 12          пересобрать корпус ответов чат-бота (сеть)
    .venv/Scripts/python eval_prompts.py overlap --repeat 2          сверка с чат-ботом: детектор сдвига против Р15
    .venv/Scripts/python eval_prompts.py mirror --prompt v2          был ли механизм «сдвиговых» идей у чат-бота
    .venv/Scripts/python eval_prompts.py latency                     задержки без сети: снимок git HEAD, v1, v2
    .venv/Scripts/python eval_prompts.py review [--score файл]       таблицы для ручной разметки / согласие разметчиков

Каждая строка результата хранит вход, сырой ответ модели, разобранный результат, проверки, время, токены,
стоимость и провайдера (MeteredClient). Эталоны — eval/*.json; разметка в них — суждение разработчика, а не
истина: см. поле about каждого файла."""

import hashlib
import json
import random
import subprocess
import sys
import threading
import time
from collections import Counter
from datetime import datetime
from itertools import combinations
from pathlib import Path

import ai_roles
import engine
import eval_prompts as ep

HERE = Path(__file__).resolve().parent
FAMILY_CASES = HERE / "eval" / "family_cases.json"
RUBRIC_CASES = HERE / "eval" / "rubric_cases.json"
CLOUD_CORPUS = HERE / "eval" / "cloud_corpus.json"
EXPORTS = HERE / "exports"
FALLBACK_PRICING = (0.32e-6, 0.89e-6)  # deepseek/deepseek-chat на OpenRouter, 11.10.2026 (09_Живой_прогон)
VERSIONED_FILES = ("ai_roles.py", "engine.py", "training.py", "llm.py", "eval_prompts.py", "eval_quality.py")


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def code_version():
    """Версия кода прогона: коммит репозитория сайта, есть ли незакоммиченные правки и отпечаток файлов LLM-части."""
    h = hashlib.sha1()
    for name in VERSIONED_FILES:
        h.update((HERE / name).read_bytes())

    def git(*args):
        try:
            return subprocess.run(["git", *args], cwd=HERE, capture_output=True, text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""
    return {"git": git("rev-parse", "--short", "HEAD") or None,
            "dirty": bool(git("status", "--porcelain", "--", *VERSIONED_FILES)),
            "files_sha1": h.hexdigest()[:12], "prompts": dict(ai_roles.PROMPT_VERSIONS)}


def pricing(model):
    """Цена модели (USD за токен): публичный список OpenRouter, при сбое — цена из 09."""
    import live_run
    return live_run.model_pricing(model) or FALLBACK_PRICING


def cost_of(calls, price):
    tin = sum(c["usage"].get("prompt_tokens") or 0 for c in calls)
    tout = sum(c["usage"].get("completion_tokens") or 0 for c in calls)
    return {"calls": len(calls), "prompt_tokens": tin, "completion_tokens": tout,
            "usd": round(tin * price[0] + tout * price[1], 5),
            "errors": sum(1 for c in calls if c.get("error"))}


def run_jobs(meter, jobs, work):
    """work(job) → строка результата; вызовы модели одного случая попадают в строку целиком."""
    def wrapped(job):
        meter.begin() if meter else None
        start = time.monotonic()
        row = work(job)
        row["seconds"] = round(time.monotonic() - start, 2)
        row["calls"] = meter.end() if meter else []
        return row
    return ep.parallel(wrapped, jobs)


def card_line(lib, card):
    """Карточка одной строкой — как common.card_text, но без контекста Flask."""
    labels = {f["id"]: f["label"] for f in lib.card_fields}
    parts = []
    for key, value in card.items():
        if key in labels and value:
            parts.append(f"{labels[key]}: {', '.join(value) if isinstance(value, list) else value}")
    return "; ".join(parts) or "не заполнена"


def _pct(a, b):
    return round(100 * a / b) if b else None


def _fmt(a, b):
    return f"{a}/{b} ({100 * a / b:.0f}%)" if b else "—"


# ---------- Семейства (рентген Р11, словарь LIB-02) ----------

def family_arms(ai, lib):
    def dictionary(case, s):
        r = engine.classify_by_dictionary(lib, case["text"])
        return {"family": r["family"] if r else None, "subfamily": r["subfamily"] if r else None,
                "confidence": None, "xray": None}

    def via(fn):
        def run(case, s):
            r = fn(case, s)
            if not r:
                return {"family": None, "subfamily": None, "confidence": None, "xray": None, "failed": True}
            return {"family": r["family"], "subfamily": r.get("subfamily"), "confidence": r.get("confidence"),
                    "xray": r.get("xray"), "alt": r.get("alt"), "abstained": r.get("abstained", False)}
        return run

    arms = {"dict": dictionary,
            # v1 — рабочий рентген до 17.10: таксономия с названиями подсемейств, без цели задачи
            "v1": via(lambda c, s: ai_roles.xray(ai, lib, c["text"], s["task"]))}
    # v1+def — абляция: рабочий промпт v1, но таксономия с определениями подсемейств (что из v2 помогает/мешает)
    arms["v1def"] = via(lambda c, s: ai_roles.xray(ai, lib, c["text"], s["task"], full_taxonomy=True))
    if hasattr(ai_roles, "xray_v2"):
        # v2 — механизм прежде семейства: определения подсемейств, «легко спутать», право сказать «не определено»
        arms["v2"] = via(lambda c, s: ai_roles.xray_v2(ai, lib, c["text"], s["task"], s.get("goal")))
    return arms


DETERMINISTIC = {"dict"}


def run_family(ai, lib, meter, data, arm_names, repeat):
    arms = {k: v for k, v in family_arms(ai, lib).items() if k in arm_names}
    jobs = [(arm, case, r) for arm in arms for case in data["cases"]
            for r in range(1 if arm in DETERMINISTIC else repeat)]

    def work(job):
        arm, case, r = job
        out = arms[arm](case, data["sets"][case["set"]])
        return {"arm": arm, "case": case["id"], "round": r, "input": case["text"], **out}
    return run_jobs(meter if any(a not in DETERMINISTIC for a in arms) else None, jobs, work)


def score_family(data, rows):
    cases = {c["id"]: c for c in data["cases"]}
    out = {}
    for arm in sorted({r["arm"] for r in rows}, key=lambda a: ["dict", "v1", "v1def", "v2"].index(a) if a in
                      ("dict", "v1", "v1def", "v2") else 9):
        mine = [r for r in rows if r["arm"] == arm]
        s = {"rows": len(mine), "strict": 0, "lenient": 0, "sure_n": 0, "sure_strict": 0, "sure_lenient": 0,
             "border_n": 0, "border_lenient": 0, "abstain_n": 0, "abstain_ok": 0, "false_abstain": 0,
             "failed": 0, "by_kind": {}, "errors": []}
        for r in mine:
            c = cases[r["case"]]
            got = r["family"]
            strict, lenient = got == c["expected"], got in c["acceptable"]
            s["failed"] += bool(r.get("failed"))
            s["strict"] += strict
            s["lenient"] += lenient
            if c.get("borderline"):
                s["border_n"] += 1
                s["border_lenient"] += lenient
            else:
                s["sure_n"] += 1
                s["sure_strict"] += strict
                s["sure_lenient"] += lenient
            if c.get("abstain_ok"):
                s["abstain_n"] += 1
                s["abstain_ok"] += got is None or lenient
            elif got is None and not r.get("failed"):
                s["false_abstain"] += 1
            k = s["by_kind"].setdefault(c["kind"], {"n": 0, "lenient": 0})
            k["n"] += 1
            k["lenient"] += lenient
            if not lenient:
                s["errors"].append(f"{c['id']} [{c['split']}, {c['kind']}] «{c['text'][:70]}» → {got} "
                                   f"(ожидалось {c['expected']}; допустимо {c['acceptable']})")
        # стабильность: один и тот же ответ во всех повторах
        answers = {}
        for r in mine:
            answers.setdefault(r["case"], []).append(r["family"])
        multi = {k: v for k, v in answers.items() if len(v) > 1}
        s["unstable"] = sorted(k for k, v in multi.items() if len(set(v)) > 1)
        s["stable_n"], s["stable_of"] = len(multi) - len(s["unstable"]), len(multi)
        # перефразировки: внутри группы в одном повторе — один ответ
        groups = {}
        for r in mine:
            g = cases[r["case"]].get("group")
            if g:
                groups.setdefault((g, r["round"]), []).append(r["family"])
        s["groups_consistent"] = sum(1 for v in groups.values() if len(set(v)) == 1 and len(v) > 1)
        s["groups_n"] = sum(1 for v in groups.values() if len(v) > 1)
        # внешне похожие с разным механизмом: ответы должны различаться
        by_round = {(r["case"], r["round"]): r["family"] for r in mine}
        pairs, separated = 0, 0
        for c in data["cases"]:
            for other in c.get("contrast", []):
                if c["id"] < other:
                    for rnd in {r["round"] for r in mine}:
                        a, b = by_round.get((c["id"], rnd), "?"), by_round.get((other, rnd), "?")
                        if a == "?" or b == "?":
                            continue
                        pairs += 1
                        separated += a != b
        s["contrast_n"], s["contrast_separated"] = pairs, separated
        s["answers"] = answers
        out[arm] = s
    return out


def print_family(summary):
    print(f"{'вариант':<6} {'строго':>14} {'допустимо':>14} {'однозначные':>14} {'спорные':>10} "
          f"{'«не опр.» к месту':>17} {'лишний отказ':>12} {'стабильно':>12} {'перефраз.':>12} {'контраст':>12}")
    for arm, s in summary.items():
        print(f"{arm:<6} {_fmt(s['strict'], s['rows']):>14} {_fmt(s['lenient'], s['rows']):>14} "
              f"{_fmt(s['sure_lenient'], s['sure_n']):>14} {_fmt(s['border_lenient'], s['border_n']):>10} "
              f"{_fmt(s['abstain_ok'], s['abstain_n']):>17} {s['false_abstain']:>12} "
              f"{_fmt(s['stable_n'], s['stable_of']):>12} {_fmt(s['groups_consistent'], s['groups_n']):>12} "
              f"{_fmt(s['contrast_separated'], s['contrast_n']):>12}")
    for arm, s in summary.items():
        kinds = ", ".join(f"{k}: {v['lenient']}/{v['n']}" for k, v in sorted(s["by_kind"].items()))
        print(f"{arm}: верно по видам — {kinds}")
        if s["unstable"]:
            print(f"{arm}: разные ответы между повторами — {', '.join(s['unstable'])}")


# ---------- Рубрика (Р9) и выбор под ограничения ----------

RB = ("RB1", "RB2", "RB3", "RB4")
FEAS_RANK = {"низкая": 0, "средняя": 1, "высокая": 2, None: -1}


def rubric_arms(ai, lib):
    arms = {"v1": lambda idea, s, line, card: ai_roles.rubric(ai, lib, idea, s["task"], s["goal"], line)}
    if hasattr(ai_roles, "rubric_v2"):
        # v2: свидетельства и якоря LIB-15; RB2 — потолок по правилам engine.resource_fit против карточки
        arms["v2"] = lambda idea, s, line, card: ai_roles.rubric_v2(ai, lib, idea, s["task"], s["goal"], line, card)
    return arms


def run_rubric(ai, lib, meter, data, arm_names, repeat):
    arms = {k: v for k, v in rubric_arms(ai, lib).items() if k in arm_names}
    jobs = [(arm, case, r) for arm in arms for case in data["cases"] for r in range(repeat)]

    def work(job):
        arm, case, r = job
        s = data["sets"][case["set"]]
        card = data["cards_alt"][case["card"]] if case.get("card") else s["card"]
        res = arms[arm](case["text"], s, card_line(lib, card), card)
        levels = {k: (res or {}).get(k, {}).get("level") for k in RB}
        return {"arm": arm, "case": case["id"], "round": r, "input": case["text"], "card": card_line(lib, card),
                "levels": levels, "result": res, "failed": res is None}
    return run_jobs(meter, jobs, work)


def _total(levels):
    vals = [levels.get(k) for k in RB]
    return sum(v if v is not None else 2 for v in vals)  # пропущенный критерий — середина шкалы


def _feas(levels):
    return engine.feasibility_level({k: {"level": v} for k, v in levels.items() if v is not None})


def score_rubric(data, rows):
    cases = {c["id"]: c for c in data["cases"]}
    out = {}
    for arm in sorted({r["arm"] for r in rows}):
        mine = [r for r in rows if r["arm"] == arm]
        s = {"rows": len(mine), "failed": sum(r["failed"] for r in mine), "clear_n": 0, "clear_ok": 0,
             "within1_n": 0, "within1_ok": 0, "missing": 0, "bias_sum": 0, "bias_n": 0, "all3": 0,
             "all3_weak": 0, "weak_n": 0, "feas_ok": 0, "feas_n": 0, "by_criterion": {}, "errors": []}
        for r in mine:
            if r["failed"]:
                continue
            c = cases[r["case"]]
            got, exp = r["levels"], c["expected"]
            present = [k for k in RB if got.get(k) is not None]
            if present and all(got[k] == 3 for k in present):
                s["all3"] += 1
                if c["tier"] in ("weak", "off_goal", "original_infeasible"):
                    s["all3_weak"] += 1
            if c["tier"] in ("weak", "off_goal", "original_infeasible"):
                s["weak_n"] += 1
            for k in RB:
                crit = s["by_criterion"].setdefault(k, {"n": 0, "exact": 0, "missing": 0, "bias": 0})
                crit["n"] += 1
                if got.get(k) is None:
                    s["missing"] += 1
                    crit["missing"] += 1
                    continue
                crit["exact"] += got[k] == exp[k]
                crit["bias"] += got[k] - exp[k]
                s["bias_sum"] += got[k] - exp[k]
                s["bias_n"] += 1
                s["within1_n"] += 1
                s["within1_ok"] += abs(got[k] - exp[k]) <= 1
                if k in c.get("clear", []):
                    s["clear_n"] += 1
                    ok = got[k] == exp[k]
                    s["clear_ok"] += ok
                    if not ok:
                        s["errors"].append(f"{c['id']} {k}: {got[k]} вместо {exp[k]} — «{c['text'][:60]}»")
            s["feas_n"] += 1
            s["feas_ok"] += _feas(got) == _feas(exp)
        # пары: лучшая идея выше худшей по оси
        by = {}
        for r in mine:
            if not r["failed"]:
                by.setdefault((r["case"], r["round"]), r["levels"])
        pair_n = pair_ok = ties = 0
        pair_fail = []
        for p in data["pairs"]:
            for rnd in {r["round"] for r in mine}:
                a, b = by.get((p["better"], rnd)), by.get((p["worse"], rnd))
                if a is None or b is None:
                    continue
                if p["axis"] == "total":
                    va, vb = _total(a), _total(b)
                elif p["axis"] == "feasibility":
                    va, vb = FEAS_RANK[_feas(a)], FEAS_RANK[_feas(b)]
                else:
                    va, vb = a.get(p["axis"]) or 0, b.get(p["axis"]) or 0
                pair_n += 1
                pair_ok += va > vb
                ties += va == vb
                if va <= vb:
                    pair_fail.append(f"{p['better']}>{p['worse']} ({p['axis']}): {va} vs {vb}")
        s.update(pair_n=pair_n, pair_ok=pair_ok, pair_ties=ties, pair_fail=sorted(set(pair_fail)))
        # выбор под ограничения: из (сильная, привычная, необычная-нереализуемая) реализуемость ставит
        # нереализуемую строго ниже обеих
        choice_n = choice_ok = 0
        for set_name in data["sets"]:
            ids = {c["tier"]: c["id"] for c in data["cases"] if c["set"] == set_name and not c.get("card")}
            need = ("strong", "practical_typical", "original_infeasible")
            if not all(t in ids for t in need):
                continue
            for rnd in {r["round"] for r in mine}:
                lv = [by.get((ids[t], rnd)) for t in need]
                if any(x is None for x in lv):
                    continue
                f = [FEAS_RANK[_feas(x)] for x in lv]
                choice_n += 1
                choice_ok += f[2] < f[0] and f[2] < f[1]
        s.update(choice_n=choice_n, choice_ok=choice_ok)
        # стабильность уровней между повторами
        answers = {}
        for r in mine:
            if not r["failed"]:
                answers.setdefault(r["case"], []).append(tuple(r["levels"].get(k) for k in RB))
        multi = {k: v for k, v in answers.items() if len(v) > 1}
        s["unstable"] = sorted(k for k, v in multi.items() if len(set(v)) > 1)
        s["stable_n"], s["stable_of"] = len(multi) - len(s["unstable"]), len(multi)
        totals = [_total(r["levels"]) for r in mine if not r["failed"]]
        s["total_spread"] = round((max(totals) - min(totals)), 1) if totals else None
        s["total_sd"] = round(_sd(totals), 2) if totals else None
        s["answers"] = {k: [list(x) for x in v] for k, v in answers.items()}
        out[arm] = s
    return out


def _sd(xs):
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    return (sum((x - m) ** 2 for x in xs) / (len(xs) - 1)) ** 0.5


def print_rubric(summary):
    print(f"{'вариант':<6} {'однозначные':>14} {'±1':>14} {'пропуски':>9} {'сдвиг':>6} {'все 3':>12} "
          f"{'все 3 у слабых':>15} {'реализуемость':>14} {'пары':>13} {'ничьи':>6} {'выбор':>11} {'стабильно':>11}")
    for arm, s in summary.items():
        print(f"{arm:<6} {_fmt(s['clear_ok'], s['clear_n']):>14} {_fmt(s['within1_ok'], s['within1_n']):>14} "
              f"{s['missing']:>9} {s['bias_sum'] / max(s['bias_n'], 1):>+6.2f} {_fmt(s['all3'], s['rows']):>12} "
              f"{_fmt(s['all3_weak'], s['weak_n']):>15} {_fmt(s['feas_ok'], s['feas_n']):>14} "
              f"{_fmt(s['pair_ok'], s['pair_n']):>13} {s['pair_ties']:>6} {_fmt(s['choice_ok'], s['choice_n']):>11} "
              f"{_fmt(s['stable_n'], s['stable_of']):>11}")
    for arm, s in summary.items():
        crit = ", ".join(f"{k}: точно {v['exact']}/{v['n'] - v['missing']}, сдвиг {v['bias'] / max(v['n'] - v['missing'], 1):+.2f}"
                         for k, v in s["by_criterion"].items())
        print(f"{arm}: {crit}; разброс суммы {s['total_spread']}, sd {s['total_sd']}; сбоев {s['failed']}")


# ---------- Облако очевидного: корпус ответов чат-бота и правила частотности ----------

def baseline_arms(ai):
    arms = {"v1": lambda s: ai_roles.baseline(ai, s["task"])}
    if hasattr(ai_roles, "baseline_v2"):
        arms["v2"] = lambda s: ai_roles.baseline_v2(ai, s["task"], s.get("goal"), s.get("bans"))
    return arms


def build_cloud_corpus(ai, lib, meter, sets, samples, prompt):
    """samples независимых ответов «обычного чат-бота» на каждую задачу; каждый ответ размечен, как в сервисе:
    словарь LIB-02, а что словарь не узнал — классификатор Р1."""
    ask = baseline_arms(ai)[prompt]
    jobs = [(name, k) for name in sets for k in range(samples)]

    def work(job):
        name, k = job
        s = sets[name]
        answers = ask(s) or []
        labelled = []
        for text in answers:
            d = engine.classify_by_dictionary(lib, text)
            if d:
                labelled.append({"text": text, "family": d["family"], "source": "dict"})
            else:
                r = ai_roles.classify(ai, lib, text, s["task"])
                labelled.append({"text": text, "family": r["family"] if r else None, "source": "llm" if r else "none"})
        return {"set": name, "k": k, "prompt": prompt, "answers": labelled, "failed": not answers}
    return run_jobs(meter, jobs, work)


def cloud_rules(lib):
    """Правила «частое / редкое / не определено» по выборке ответов чат-бота. Каждое — функция от списка
    выборок (каждая — множество семейств одного ответа чат-бота) → {семейство: метка}."""
    fams = list(lib.families)

    def v1(samples):  # рабочее до 17.10 (engine.common_families): seed «часто» ∪ «средне», встреченные у чат-бота
        common, _ = engine.common_families(lib, samples[0])
        return {f: "common" if f in common else "rare" for f in fams}

    rules = {"v1": (1, v1),
             "seed_only": (1, lambda samples: {f: {"часто": "common", "редко": "rare"}.get(lib.families[f]["seed"],
                                                                                         "uncertain") for f in fams})}
    if hasattr(engine, "cloud_labels"):
        rules["v2"] = (engine.CLOUD_SAMPLES, lambda samples: engine.cloud_labels(lib, samples)[0])
    return rules


def score_cloud(lib, corpus, partitions=30, seed=17):
    """Надёжность меток облака на корпусе. Эталон частотности для задачи — доля независимых ответов чат-бота,
    где семейство встретилось (по всему корпусу). Метки правила считаются на непересекающихся выборках
    нужного размера, много раз, и сравниваются между собой (повторяемость) и с эталоном."""
    rng = random.Random(seed)
    by_set = {}
    for row in corpus["samples"]:
        if not row.get("failed"):
            by_set.setdefault(row["set"], []).append({a["family"] for a in row["answers"] if a["family"]})
    rules = cloud_rules(lib)
    out = {name: {"agree": 0, "agree_n": 0, "size": [], "false_rare": 0, "typical_n": 0, "false_common": 0,
                  "atypical_n": 0, "uncertain": 0, "labels_n": 0, "hard_flips": 0, "rare_claims": 0,
                  "rare_repeated": 0, "flips": Counter()} for name in rules}
    reference = {}
    for set_name, samples in by_set.items():
        n = len(samples)
        share = {f: sum(f in s for s in samples) / n for f in lib.families}
        reference[set_name] = {f: round(v, 2) for f, v in share.items() if v}
        for name, (k, rule) in rules.items():
            o = out[name]
            for _ in range(partitions):
                order = samples[:]
                rng.shuffle(order)
                draws = [order[i:i + k] for i in range(0, n - k + 1, k)]
                labels = [rule(d) for d in draws]
                for lab in labels:
                    o["size"].append(sum(v == "common" for v in lab.values()))
                    for f, v in lab.items():
                        o["labels_n"] += 1
                        o["uncertain"] += v == "uncertain"
                        if share[f] >= 0.5:
                            o["typical_n"] += 1
                            o["false_rare"] += v == "rare"
                        if share[f] <= 1 / n + 1e-9 and lib.families[f]["seed"] != "часто":
                            o["atypical_n"] += 1
                            o["false_common"] += v == "common"
                for a, b in combinations(labels, 2):
                    for f in lib.families:
                        o["agree_n"] += 1
                        o["agree"] += a[f] == b[f]
                        # самое вредное: в одном запуске «частое», в другом «редкое» — человек видит противоположное
                        o["hard_flips"] += {a[f], b[f]} == {"common", "rare"}
                        for x, y in ((a[f], b[f]), (b[f], a[f])):
                            if x == "rare":  # надёжность утверждения «редкое»: повторится ли оно в другом запуске
                                o["rare_claims"] += 1
                                o["rare_repeated"] += y == "rare"
                        if a[f] != b[f]:
                            o["flips"][f"{set_name}:{f}"] += 1
    for name, o in out.items():
        o["size_mean"] = round(sum(o["size"]) / max(len(o["size"]), 1), 1)
        o["size"] = None
        o["top_flips"] = [f for f, _ in o["flips"].most_common(6)]
        o["flips"] = None
    return out, reference


def print_cloud(summary):
    print(f"{'правило':<10} {'повторяемость':>14} {'частое↔редкое':>14} {'«редкое» повторилось':>21} "
          f"{'«частых» в ср.':>15} {'ложно «редкое»':>17} {'ложно «частое»':>15} {'«не опр.»':>10}")
    for name, o in summary.items():
        print(f"{name:<10} {_pct(o['agree'], o['agree_n']):>13}% {_pct(o['hard_flips'], o['agree_n']):>13}% "
              f"{_fmt(o['rare_repeated'], o['rare_claims']):>21} {o['size_mean']:>15} "
              f"{_fmt(o['false_rare'], o['typical_n']):>17} {_fmt(o['false_common'], o['atypical_n']):>15} "
              f"{_pct(o['uncertain'], o['labels_n']):>9}%")
    for name, o in summary.items():
        if o["top_flips"]:
            print(f"{name}: чаще всего меняют метку — {', '.join(o['top_flips'])}")


# ---------- Честность зеркала: предложил бы обычный чат-бот тот же механизм? ----------

def run_chatbot_overlap(ai, meter, corpus, shift_data, per_set):
    """Для «сдвиговых» идей эталона детектора (expected = shift — механизм, которого не было в первых идеях)
    проверяем: есть ли такой же механизм среди ответов обычного чат-бота на ту же задачу. Сравнивает детектор
    сдвига v2 — ответы чат-бота подаются как «прежние идеи». «форма» или «нет» — чат-бот механизм уже предлагал."""
    ideas = [c for c in shift_data["cases"] if c["expected"] == "shift"]
    by_set = {}
    for row in corpus["samples"]:
        if not row.get("failed"):
            by_set.setdefault(row["set"], []).append(row)
    jobs = [(idea, sample) for idea in ideas for sample in by_set.get(idea["set"], [])[:per_set]]

    def work(job):
        idea, sample = job
        s = shift_data["sets"][idea["set"]]
        base = [{"text": a["text"], "xray": None} for a in sample["answers"]]
        r = ai_roles.shift_check_v2(ai, base, idea["text"], s["task"], s["goal"])
        same = base[r["same_as"] - 1]["text"] if r and r.get("same_as") else None
        return {"case": idea["id"], "set": idea["set"], "sample": sample["k"], "prompt": sample["prompt"],
                "input": idea["text"], "chatbot": [a["text"] for a in sample["answers"]], "result": r,
                "covered": bool(r and r["shift"] in ("форма", "нет")), "same_as_text": same, "failed": r is None}
    return run_jobs(meter, jobs, work)


def score_overlap(rows):
    per = {}
    for r in rows:
        if not r["failed"]:
            per.setdefault(r["case"], []).append(r["covered"])
    shares = {c: sum(v) / len(v) for c, v in per.items()}
    return {"ideas": len(shares), "rows": len(rows), "failed": sum(r["failed"] for r in rows),
            "covered_rows": sum(r["covered"] for r in rows if not r["failed"]),
            "ideas_covered_majority": sorted(c for c, v in shares.items() if v > 0.5),
            "ideas_covered_ever": sorted(c for c, v in shares.items() if v > 0),
            "mean_share": round(sum(shares.values()) / max(len(shares), 1), 2), "shares": shares}


MIRROR_CASES = HERE / "eval" / "mirror_cases.json"
PATTERN_CASES = HERE / "eval" / "pattern_cases.json"


OUTCOME_CASES = HERE / "eval" / "outcome_cases.json"


def run_pattern(ai, meter, data, repeat, arms=("v1", "v2")):
    """Р16 «что общего у первых идей»: вердикт и решение «колея → задание смены механизма» против разметки.
    v1 — прежний запрос (группы и вердикт от модели) и прежнее правило колеи (уверенность high или medium);
    v2 — способы воздействия у каждой идеи, группы и уверенность считает код, колея — training.mechanism_rut."""
    import training
    fns = {"v1": ai_roles.pattern_v1, "v2": ai_roles.pattern_v2}
    jobs = [(c, r, a) for c in data["cases"] for r in range(repeat) for a in arms]

    def work(job):
        c, r, arm = job
        res = fns[arm](ai, c["task"], c["goal"], c["ideas"])
        if arm == "v1":
            rut = bool(res and res["verdict"] == "one" and res["confidence"] in ("high", "medium") and res["groups"])
        else:
            rut = bool(res) and training.mechanism_rut({"pattern": res})
        return {"case": c["id"], "round": r, "arm": arm, "input": c["ideas"], "result": res, "failed": res is None,
                "verdict": (res or {}).get("verdict"), "confidence": (res or {}).get("confidence"), "rut": rut}
    return run_jobs(meter, jobs, work)


def score_pattern(data, rows):
    """По каждой версии запроса отдельно: доля допустимых вердиктов и верных решений о колее, ошибки."""
    cases = {c["id"]: c for c in data["cases"]}
    out = {}
    for arm in sorted({r.get("arm", "v1") for r in rows}):
        ok_v = ok_r = n = failed = false_rut = missed_rut = 0
        errors, answers = [], {}
        for r in (x for x in rows if x.get("arm", "v1") == arm):
            c = cases[r["case"]]
            if r["failed"]:
                failed += 1
                errors.append(f"{c['id']}: нет ответа")
                continue
            n += 1
            ok_v += r["verdict"] in c["verdict"]
            ok_r += r["rut"] == c["rut"]
            false_rut += r["rut"] and not c["rut"]
            missed_rut += c["rut"] and not r["rut"]
            answers.setdefault(c["id"], []).append(r["verdict"])
            if r["verdict"] not in c["verdict"] or r["rut"] != c["rut"]:
                res = r["result"] or {}
                errors.append(f"{c['id']} [{c['scenario']}]: вердикт {r['verdict']}/{r.get('confidence')} "
                              f"(допустимо {c['verdict']}), колея {r['rut']} (ожидалось {c['rut']}) — "
                              f"{res.get('headline') or res.get('insight')}")
        out[arm] = {"n": n, "failed": failed, "verdict_ok": ok_v, "rut_ok": ok_r, "false_rut": false_rut,
                    "missed_rut": missed_rut, "errors": errors, "answers": answers,
                    "unstable": sorted(k for k, v in answers.items() if len(set(v)) > 1)}
    return out


def run_outcome(ai, lib, meter, data, repeat):
    """Проверка результата упражнения целиком, как в сервисе: Р16 v2 по первым идеям → рентген первых идей → Р12 v3
    по новой идее (с рентгеном и способами первых идей) → training.outcome_verdict."""
    import training
    jobs = [(c, r) for c in data["cases"] for r in range(repeat)]

    def work(job):
        c, r = job
        pat = ai_roles.pattern_v2(ai, c["task"], c["goal"], c["first"])
        levers = {p["n"]: p["lever"] for p in (pat or {}).get("ideas") or []}
        base = []
        for i, text in enumerate(c["first"], 1):
            x = ai_roles.xray(ai, lib, text, c["task"])
            base.append({"text": text, "xray": (x or {}).get("xray"), "lever": levers.get(i)})
        vf = ai_roles.shift_check_v3(ai, base, c["new"], c["task"], c["goal"])
        old = {lv for lv in levers.values() if lv}
        verdict, basis = training.outcome_verdict(dict(vf, source="ai") if vf else None, old, True, c["new"])
        return {"case": c["id"], "round": r, "verdict": verdict, "basis": basis, "old": sorted(old),
                "new": (vf or {}).get("levers"), "shift": (vf or {}).get("shift"),
                "confidence": (vf or {}).get("confidence"), "failed": vf is None}
    return run_jobs(meter, jobs, work)


def score_outcome(data, rows):
    cases = {c["id"]: c for c in data["cases"]}
    ok = n = false_success = 0
    errors = []
    for r in rows:
        c = cases[r["case"]]
        n += 1
        ok += r["verdict"] in c["outcome"]
        false_success += r["verdict"] == "changed" and "changed" not in c["outcome"]
        if r["verdict"] not in c["outcome"]:
            errors.append(f"{c['id']} [{c['scenario']}]: {r['verdict']} (допустимо {c['outcome']}); способы "
                          f"{r['old']} → {r['new']}, детектор {r['shift']}/{r['confidence']}")
    return {"n": n, "ok": ok, "false_success": false_success, "errors": errors}


def overlap_arms(ai):
    def detector(c, s):  # как в run_chatbot_overlap: ответы чат-бота — «прежние идеи» детектора сдвига v2
        r = ai_roles.shift_check_v2(ai, [{"text": a, "xray": None} for a in c["chatbot"]], c["idea"], s["task"],
                                    s["goal"])
        return None if r is None else {"same": r["same_as"] if r["shift"] in ("форма", "нет") else None,
                                       "covered": r["shift"] in ("форма", "нет"), "explanation": r["explanation"]}

    def role(c, s):  # Р15: отдельная роль «есть ли у чат-бота тот же механизм»
        r = ai_roles.chatbot_overlap(ai, c["idea"], c["chatbot"], s["task"], s["goal"])
        return None if r is None else dict(r, covered=r["same"] is not None)
    return {"detector": detector, "v1": role}


def run_overlap(ai, meter, data, sets, arm_names, repeat):
    arms = {k: v for k, v in overlap_arms(ai).items() if k in arm_names}
    jobs = [(arm, c, r) for arm in arms for c in data["cases"] for r in range(repeat)]

    def work(job):
        arm, c, r = job
        res = arms[arm](c, sets[c["set"]])
        return {"arm": arm, "case": c["id"], "round": r, "input": c["idea"], "result": res, "failed": res is None,
                "covered": bool(res and res["covered"]), "same": (res or {}).get("same")}
    return run_jobs(meter, jobs, work)


def score_overlap_cases(data, rows):
    cases = {c["id"]: c for c in data["cases"]}
    out = {}
    for arm in sorted({r["arm"] for r in rows}):
        mine = [r for r in rows if r["arm"] == arm]
        s = {"rows": len(mine), "failed": sum(r["failed"] for r in mine), "errors": []}
        for part in ("sure", "border"):
            s[part] = {"tp": 0, "fp": 0, "fn": 0, "tn": 0}
        same_ok = same_n = 0
        for r in mine:
            if r["failed"]:
                continue
            c = cases[r["case"]]
            cell = ("tp" if c["covered"] else "fp") if r["covered"] else ("fn" if c["covered"] else "tn")
            s["border" if c.get("borderline") else "sure"][cell] += 1
            if r["covered"] and c["covered"]:
                same_n += 1
                same_ok += r["same"] == c["expected_same"]
            if r["covered"] != c["covered"] and not c.get("borderline"):
                s["errors"].append(f"{c['id']} {'ложное совпадение' if r['covered'] else 'пропуск'}: «{c['idea'][:60]}»")
        s["same_ok"], s["same_n"] = same_ok, same_n
        answers = {}
        for r in mine:
            if not r["failed"]:
                answers.setdefault(r["case"], []).append(r["covered"])
        s["answers"] = answers
        s["unstable"] = sorted(k for k, v in answers.items() if len(set(v)) > 1)
        out[arm] = s
    return out


def print_overlap(summary):
    print(f"{'вариант':<9} {'точность':>14} {'precision':>14} {'recall':>14} {'спорные «как в разметке»':>25} "
          f"{'тот же ответ':>13} {'сбоев':>6}")
    for arm, s in summary.items():
        m, b = s["sure"], s["border"]
        print(f"{arm:<9} {_fmt(m['tp'] + m['tn'], sum(m.values())):>14} {_fmt(m['tp'], m['tp'] + m['fp']):>14} "
              f"{_fmt(m['tp'], m['tp'] + m['fn']):>14} {_fmt(b['tp'] + b['tn'], sum(b.values())):>25} "
              f"{_fmt(s['same_ok'], s['same_n']):>13} {s['failed']:>6}")
    for arm, s in summary.items():
        if s["unstable"]:
            print(f"{arm}: разные ответы между повторами — {', '.join(s['unstable'])}")


# ---------- Ручная проверка: критические случаи для разметки людьми ----------
# Только из сохранённых прогонов — без новых вызовов модели. Метки эталонов не меняются: люди размечают вслепую
# (файл без ответа системы и текущей метки), затем сверка по ключу. docs/product-value/review/INSTRUCTIONS.md

RESULTS_DIR = HERE / "eval" / "results"
REVIEW_QUESTIONS = {
    "chatbot": ("Есть ли среди ответов чат-бота идея с ТЕМ ЖЕ механизмом, что у идеи пользователя: тот же ответ на "
                "«почему клиент придёт или купит» и тот же снимаемый барьер? Детали и канал механизм не меняют.",
                "номер ответа чат-бота (1–6) / нет / не могу решить"),
    "rb2": ("Укладывается ли идея в карточку ресурсов? 3 — укладывается; 2 — почти весь ресурс или не хватает одного "
            "(навык, помощник, немного времени); 1 — требует того, чего нет, или того, что человек запретил.",
            "3 / 2 / 1 + чего не хватает"),
    "family": ("К какому семейству LIB-01 относится идея — по ответу на «почему клиент придёт»? Если подходят два — "
               "основное и допустимое. Если механизма привлечения нет — «нет механизма».",
               "код F01–F15 (+ второй допустимый) / нет механизма"),
    "shift": ("Работает ли новая идея за счёт другого побуждения или снимает другой барьер, чем ВСЕ первые идеи?",
              "сдвиг / та же ветка (номер похожей первой идеи) / не могу решить"),
}


def _latest(pattern):
    files = sorted(RESULTS_DIR.glob(pattern))
    return files[-1] if files else None


def review_rows():
    """Критические случаи по четырём проверкам; каждый — вход, ответ системы, текущая метка, тип ошибки и почему
    это важно человеку. Ошибка — гипотеза разметчика-разработчика, её и проверяют люди."""
    rows = []

    def add(kind, cid, task, inp, system, label, error, why):
        rows.append({"id": f"{kind}:{cid}", "kind": kind, "task": task, "input": inp, "system": system,
                     "label": label, "error": error, "why": why})
    # 1. Сверка с чат-ботом (Р15): ложные «у чат-бота было» по разметке, спорные расхождения и три контрольных
    sets = load(ep.SHIFT_CASES)["sets"]
    mirror = {c["id"]: c for c in load(MIRROR_CASES)["cases"]}
    found = {}
    for pattern in ("*_overlap_dev.json", "*_overlap_holdout.json"):
        path = _latest(pattern)
        for r in (load(path)["overlap"]["rows"] if path else []):
            if r["arm"] == "v1" and not r["failed"]:
                found.setdefault(r["case"], []).append(r)
    for cid, rs in sorted(found.items()):
        c = mirror[cid]
        said = [r["covered"] for r in rs]
        control = cid in ("c11_s1", "h05_s0", "h11_s0")
        if not (control or (any(said) and not c["covered"]) or (c["borderline"] and any(said) != c["covered"])):
            continue
        same = next((r["same"] for r in rs if r["same"]), None)
        listing = " | ".join(f"{i}. {a}" for i, a in enumerate(c["chatbot"], 1))
        system = (f"у чат-бота был тот же механизм (ответ {same}) в {sum(said)} из {len(said)} повторов: "
                  + ((next(r for r in rs if r["same"])["result"] or {}).get("explanation") or "")
                  if any(said) else f"тот же механизм не найден ({len(said)} повтора)")
        label = (f"да, ответ {c['expected_same']}" if c["covered"] else "нет") + (" (спорный)" if c["borderline"] else "")
        error = ("контроль: система и разметка согласны" if control else
                 "спорный: система и разметка расходятся" if c["borderline"] else "ложное «у чат-бота было» (по разметке)")
        add("chatbot", cid, sets[c["set"]]["task"], f"Идея пользователя: {c['idea']}\nОтветы чат-бота: {listing}",
            system, label, error,
            "Человек увидит «похожий механизм предлагал и обычный чат-бот», новизна станет «средней». Ложное "
            "утверждение обесценивает его идею, пропуск — выдаёт доступный каждому ход за открытие.")
    # 2. Рубрика v2, RB2 — расхождения с разметкой по ресурсам (в т. ч. потолки по правилам)
    rub = load(RUBRIC_CASES)
    rcases = {c["id"]: c for c in rub["cases"]}
    v2rows = [r for p in ("*_rubric_dev.json", "*_rubric_holdout.json") if _latest(p)
              for r in load(_latest(p))["rubric"]["rows"] if r["arm"] == "v2" and not r["failed"]]
    by = {}
    for r in v2rows:
        by.setdefault(r["case"], []).append(r)
    for cid, rs in sorted(by.items()):
        c = rcases[cid]
        levels = [r["levels"]["RB2"] for r in rs]
        if "RB2" not in c.get("clear", []) or all(lv == c["expected"]["RB2"] for lv in levels):
            continue  # только там, где уровень ресурсов в разметке однозначен
        s = rub["sets"][c["set"]]
        card = rub["cards_alt"][c["card"]] if c.get("card") else s["card"]
        reasons = "; ".join(dict.fromkeys((r["result"]["RB2"].get("reason") or "") for r in rs))
        rule = any(r["result"]["RB2"].get("rule") for r in rs)
        add("rb2", cid, f"{s['task']} Цель: {s['goal']}",
            f"Идея: {c['text']}\nКарточка: {json.dumps(card, ensure_ascii=False)}",
            f"RB2 по повторам: {levels}; причина: {reasons}", f"RB2 = {c['expected']['RB2']} ({c['why']})",
            ("потолок по правилу против карточки" if rule else "оценка модели") + " расходится с разметкой",
            "Строка «Ресурсы» решает, назовёт ли сервис идею «рискованной»: зря заниженный уровень отговаривает от "
            "выполнимого решения, завышенный — пропускает то, на что нет денег, времени или навыка.")
    # 3. Семейства, рентген v1: ошибки по разметке и неустойчивые между повторами
    fam = load(FAMILY_CASES)
    fpath = _latest("*_family_all.json")
    if fpath:
        answers = score_family(fam, [r for r in load(fpath)["family"]["rows"] if r["arm"] == "v1"])["v1"]["answers"]
        for c in fam["cases"]:
            got = answers.get(c["id"], [])
            if got and (len(set(got)) > 1 or any(g not in c["acceptable"] for g in got)):
                add("family", c["id"], fam["sets"][c["set"]]["task"], f"Идея: {c['text']}", f"семейство по повторам: {got}",
                    f"{c['expected']} (допустимо {c['acceptable']}; {c['why']})",
                    "неустойчиво между повторами" if len(set(got)) > 1 else "ошибка по разметке",
                    "От семейства зависят метка «частое/редкое» и новизна в оценке: другое семейство при повторе — "
                    "другой вывод для того же человека.")
    # 4. Детектор сдвига: спорные случаи эталона 13.10 (ответы — из сохранённых прогонов 13.10)
    shift = load(ep.SHIFT_CASES)
    said = {}
    for pattern in ("*_shift.json", "*_shift_holdout.json"):
        for path in RESULTS_DIR.glob(pattern):
            for r in load(path)["rows"]:
                if r["arm"] == "v2" and r["result"]:
                    said.setdefault(r["case"], []).append(r["result"]["shift"])
    for c in shift["cases"]:
        if c.get("borderline"):
            s = shift["sets"][c["set"]]
            got = said.get(c["id"]) or (["механизм (живая проверка 13.10, ответ не сохранён)"] if c["id"] == "h26" else [])
            add("shift", c["id"], s["task"], f"Первые идеи: {'; '.join(s['base'])}\nНовая идея: {c['text']}",
                f"детектор v2: {got}", f"{'сдвиг' if c['expected'] == 'shift' else 'та же ветка'} ({c['why']})",
                "спорная метка эталона",
                "«Сдвиг механизма» — главный сигнал сессии: от него зависят новизна и «вау»-вопрос.")
    return rows


def write_review(rows, out_dir):
    """Два файла: для разметчиков — вслепую (без ответа системы и метки), ключ — для сверки после разметки.
    CSV с BOM и «;» — открывается в Excel и Google Таблицах как есть."""
    import csv
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    blind, key = out_dir / "critical_cases_blind.csv", out_dir / "critical_cases_key.csv"
    with open(blind, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["id", "проверка", "задача", "что оценить", "вопрос", "варианты ответа", "разметчик 1",
                    "разметчик 2", "итог после обсуждения", "комментарий"])
        for r in rows:
            q, options = REVIEW_QUESTIONS[r["kind"]]
            w.writerow([r["id"], r["kind"], r["task"], r["input"], q, options, "", "", "", ""])
    with open(key, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["id", "ответ системы", "текущая метка эталона", "тип расхождения (гипотеза разработчика)",
                    "почему важно человеку"])
        for r in rows:
            w.writerow([r["id"], r["system"], r["label"], r["error"], r["why"]])
    return blind, key


def score_review(path):
    """Согласие разметчиков по заполненной слепой таблице: доля совпадений и каппа Коэна по каждой проверке."""
    import csv

    def norm(v):
        return " ".join((v or "").lower().replace("ё", "е").split())
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f, delimiter=";"))
    out = {}
    for kind in REVIEW_QUESTIONS:
        pairs = [(norm(r["разметчик 1"]), norm(r["разметчик 2"])) for r in rows
                 if r["проверка"] == kind and r["разметчик 1"].strip() and r["разметчик 2"].strip()]
        if not pairs:
            continue
        agree = sum(a == b for a, b in pairs) / len(pairs)
        cats = Counter([a for a, _ in pairs] + [b for _, b in pairs])
        chance = sum((n / (2 * len(pairs))) ** 2 for n in cats.values())
        kappa = (agree - chance) / (1 - chance) if chance < 1 else 1.0
        out[kind] = {"n": len(pairs), "agree": round(agree, 2), "kappa": round(kappa, 2),
                     "disagree": [r["id"] for r in rows if r["проверка"] == kind and r["разметчик 1"].strip()
                                  and norm(r["разметчик 1"]) != norm(r["разметчик 2"])]}
    return out


# ---------- Задержки: критический путь без сети ----------

# Роли, которых не было в прогонах 12.10: медианы из прогонов 17–18.10 (eval_prompts.py overlap / pattern,
# live_run_20261018_1145: план проверки). Для исходной реализации они не нужны — их там нет.
NEW_ROLE_DELAYS = {"сверка с чат-ботом (Р15)": 2.7, "что общего у идей (Р16)": 13.3, "план проверки (Р5 v2)": 8.0}


def measured_delays():
    """Медиана времени каждой роли по живым прогонам v0.5 (exports/live_run_20261012_*.json) + новые роли."""
    per = {}
    for f in sorted(EXPORTS.glob("live_run_20261012_*.json")):
        for sc in load(f)["scenarios"]:
            for c in sc["calls"]:
                per.setdefault(c["role"], []).append(c["seconds"])
    return dict({role: sorted(v)[len(v) // 2] for role, v in per.items()}, **NEW_ROLE_DELAYS)


class DelayClient:
    """FakeClient с задержкой роли: ответы по контрактам из тестов, время — медиана живых прогонов × scale."""

    available = True

    def __init__(self, responder, delays, scale):
        import live_run
        self.detect, self.responder, self.delays, self.scale = live_run.detect_role, responder, delays, scale
        self.calls = []
        self.lock = threading.Lock()

    def complete(self, system, user, max_tokens=600, temperature=0.3):
        role = self.detect(system, user)
        time.sleep(self.delays.get(role, 3.0) * self.scale)
        with self.lock:
            self.calls.append({"role": role, "seconds": self.delays.get(role, 3.0), "prompt_tokens": 0,
                               "completion_tokens": 0, "ok": True, "error": None, "output": ""})
        return self.responder(system, user)


def run_latency(configs, scale=0.05, think=120, read=20, look=30):
    """Сценарий А живого прогона на FakeClient с задержками: сколько человек ждёт на каждом шаге.

    configs — {имя: функция, включающая вариант реализации}; think — сколько секунд человек пишет первые идеи
    (предвыборка ответов чат-бота успевает за это время, если она есть)."""
    import tempfile
    sys.path.insert(0, str(HERE / "tests"))
    import live_run
    import test_e2e
    import training
    delays = measured_delays()
    out = {}
    for name, enable in configs.items():
        restore = enable(training)
        try:
            client = DelayClient(test_e2e.contract_ai, delays, scale)
            with tempfile.TemporaryDirectory() as tmp:
                runner = live_run.Runner(client, str(Path(tmp) / "lat.db"))
                runner.think = think * scale
                runner.read = read * scale
                runner.look = look * scale
                runner.run(live_run.SCENARIOS[0], "lat")
                steps = [{"step": s["step"], "seconds": round(s["seconds"] / scale, 1)} for s in runner.steps]
        finally:
            restore()
        waits = [s["seconds"] for s in steps]
        out[name] = {"steps": steps, "total_wait": round(sum(waits), 1),
                     "first_ideas": next(s["seconds"] for s in steps if s["step"].startswith("С2")),
                     "max_wait": max(waits)}
    return out, delays


def run_latency_head(scale, think):
    """Исходная реализация для замера задержек — снимок git HEAD сайта во временной папке. Туда же кладутся
    текущие скрипты замера и медианы живых прогонов; запуск — отдельным процессом (модули с теми же именами)."""
    import io
    import shutil
    import tarfile
    import tempfile
    archive = subprocess.run(["git", "archive", "HEAD"], cwd=HERE, capture_output=True, timeout=60).stdout
    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(tmp, filter="data")
        for name in ("eval_quality.py", "eval_prompts.py", "live_run.py"):
            shutil.copy2(HERE / name, Path(tmp) / name)
        (Path(tmp) / "exports").mkdir(exist_ok=True)
        for f in EXPORTS.glob("live_run_20261012_*.json"):
            shutil.copy2(f, Path(tmp) / "exports" / f.name)
        out = subprocess.run([sys.executable, "eval_prompts.py", "latency", "--scale", str(scale), "--think",
                              str(think), "--no-head"], cwd=tmp, capture_output=True, text=True, encoding="utf-8",
                             timeout=1800, env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"})
        path = next((line.split(": ", 1)[1].strip() for line in out.stdout.splitlines()
                     if line.startswith("Ответы целиком: ")), None)
        if not path:
            raise RuntimeError(f"замер HEAD не удался: {out.stderr[-500:]}")
        return load(path)["latency"]["runs"]["v1"]


def print_latency(summary):
    """Шаги разных путей (полный и короткий) различаются: строка на каждый шаг, «—» — шага в этом пути нет.
    Повторяющиеся названия (две подсказки, две версии) различаются по порядку."""
    names = list(summary)

    def keyed(steps):
        seen, out = Counter(), {}
        for s in steps:
            seen[s["step"]] += 1
            out[(s["step"], seen[s["step"]])] = s["seconds"]
        return out
    per = {n: keyed(summary[n]["steps"]) for n in names}
    order = []
    for n in names:
        for k in per[n]:
            if k not in order:
                order.append(k)
    print(f"{'шаг':<52}" + "".join(f"{n:>12}" for n in names))
    for k in order:
        print(f"{k[0]:<52}" + "".join(f"{per[n][k]:>11.1f}с" if k in per[n] else f"{'—':>12}" for n in names))
    print(f"{'ВСЕГО ожидания за сессию':<52}" + "".join(f"{summary[n]['total_wait']:>11.1f}с" for n in names))


# ---------- Сравнение «до/после» и отчёт ----------

def case_diffs(cases, summary, key_ok):
    """Случаи, где v2 лучше/хуже v1 по большинству повторов, и неустойчивые."""
    if not ("v1" in summary and "v2" in summary):
        return {}
    better, worse = [], []
    for cid, c in cases.items():
        a, b = summary["v1"]["answers"].get(cid), summary["v2"]["answers"].get(cid)
        if not a or not b:
            continue
        ra = sum(key_ok(c, x) for x in a) / len(a)
        rb = sum(key_ok(c, x) for x in b) / len(b)
        if rb > ra:
            better.append((cid, a, b))
        elif rb < ra:
            worse.append((cid, a, b))
    return {"better": better, "worse": worse,
            "unstable_v1": summary["v1"]["unstable"], "unstable_v2": summary["v2"]["unstable"]}


def write_report(title, sections, payload, name):
    """Markdown-отчёт рядом с JSON: условия прогона, сводки, конкретные улучшения и ухудшения."""
    lines = [f"# {title}", "", f"Дата: {datetime.now():%Y-%m-%d %H:%M}. Код: `{json.dumps(payload['code'])}`.",
             f"Модель: {payload.get('model')}. Провайдеры: {payload.get('providers')}. "
             f"Стоимость прогона: ${payload.get('cost', {}).get('usd')} "
             f"({payload.get('cost', {}).get('calls')} вызовов, ошибок {payload.get('cost', {}).get('errors')}).", ""]
    for head, body in sections:
        lines += [f"## {head}", "", body, ""]
    md = ep.save(f"{name}.md", "\n".join(lines))
    js = ep.save(f"{name}.json", payload)
    return md, js


def providers(calls):
    return dict(Counter(c.get("provider") or "?" for c in calls))


# ---------- командная строка ----------

ROLES = ("family", "rubric", "cloud", "mirror", "overlap", "pattern", "outcome", "latency", "compare", "review")


def _setup():
    import library
    import llm
    settings = llm.load_settings()
    if not settings.get("OPENROUTER_API_KEY"):
        sys.exit("Нет ключа в settings.env — прогон на настоящей модели невозможен.")
    meter = ep.MeteredClient(settings)
    model = settings.get("OPENROUTER_MODEL") or llm.DEFAULT_MODEL
    return llm.AI(meter), library.Library(), meter, model, pricing(model)


def _split(cases, split):
    return [c for c in cases if split == "all" or c.get("split") == split]


def _diff_text(diffs, show):
    if not diffs:
        return "Сравнения нет: в прогоне один вариант."
    out = []
    for label, key in (("Лучше в v2", "better"), ("Хуже в v2 (регрессии)", "worse")):
        out.append(f"**{label}: {len(diffs[key])}**")
        out += [f"- {cid}: v1 {show(a)} → v2 {show(b)}" for cid, a, b in diffs[key]]
    out.append(f"Неустойчивы между повторами: v1 — {', '.join(diffs['unstable_v1']) or 'нет'}; "
               f"v2 — {', '.join(diffs['unstable_v2']) or 'нет'}.")
    return "\n".join(out)


def _capture(fn, *args):
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(*args)
    return buf.getvalue()


def latency_configs():
    """Варианты реализации для замера задержек: исходная (v1) и изменённая (v2) — переключатели в training."""
    import training

    def setter(**values):
        def enable(module):
            old = {k: getattr(module, k) for k in values}
            for k, v in values.items():
                setattr(module, k, v)
            return lambda: [setattr(module, k, v) for k, v in old.items()]
        return enable
    if not hasattr(training, "PREFETCH"):
        return {"v1": setter()}
    return {"v1": setter(PREFETCH=False), "v2": setter(PREFETCH=True)}


def cli(args):
    started = time.monotonic()
    if args.role == "review":  # таблица для ручной разметки или подсчёт согласия по заполненной — без сети
        if args.score:
            print(json.dumps(score_review(args.score), ensure_ascii=False, indent=1))
            return
        rows = review_rows()
        blind, key = write_review(rows, args.out or HERE.parent / "docs" / "product-value" / "review")
        print(f"Случаев: {len(rows)} ({dict(Counter(r['kind'] for r in rows))}).\nДля разметчиков: {blind}\nКлюч: {key}")
        return
    suites = args.suites.split(",") if args.role == "compare" else [args.role]
    sections, payload, lat_text = [], {}, None
    if "latency" in suites:
        lat = {"HEAD": run_latency_head(args.scale, args.think)} if args.head else {}
        runs, delays = run_latency(latency_configs(), scale=args.scale, think=args.think)
        lat.update(runs)
        lat_text = _capture(print_latency, lat)
        print(lat_text)
        payload["latency"] = {"runs": lat, "delays": delays, "scale": args.scale, "think": args.think}
        sections.append(("Задержки (FakeClient, медианы ролей живых прогонов 12.10)", f"```\n{lat_text}```"))
        if args.role == "latency":
            print(f"Медианы ролей: {json.dumps(delays, ensure_ascii=False)}")
            print(f"Ответы целиком: {ep.save('latency.json', payload)}")
            return
    if args.role == "cloud" and args.offline:
        import library
        summary, reference = score_cloud(library.Library(), load(CLOUD_CORPUS))
        print_cloud(summary)
        print(f"\nЭталон (доля ответов чат-бота с семейством): {json.dumps(reference, ensure_ascii=False)}")
        return
    ai, lib, meter, model, price = _setup()
    arms = args.arms.split(",") if args.arms else None
    if "family" in suites:
        data = load(FAMILY_CASES)
        data["cases"] = _split(data["cases"], args.split)
        rows = run_family(ai, lib, meter, data, arms or ["dict", "v1", "v2"], args.repeat)
        summary = score_family(data, rows)
        diffs = case_diffs({c["id"]: c for c in data["cases"]}, summary, lambda c, got: got in c["acceptable"])
        text = _capture(print_family, summary)
        print(f"Семейства: случаев {len(data['cases'])}, повторов {args.repeat}\n{text}")
        sections.append((f"Семейства ({args.split}, {len(data['cases'])} случаев × {args.repeat})",
                         f"```\n{text}```\n\n" + _diff_text(diffs, lambda xs: "/".join(str(x) for x in xs))))
        payload["family"] = {"rows": rows, "summary": summary}
    if "rubric" in suites:
        data = load(RUBRIC_CASES)
        data["cases"] = _split(data["cases"], args.split)
        keep = {c["id"] for c in data["cases"]}
        data["pairs"] = [p for p in data["pairs"] if p["better"] in keep and p["worse"] in keep]
        rows = run_rubric(ai, lib, meter, data, arms or ["v1", "v2"], args.repeat)
        summary = score_rubric(data, rows)

        def clear_ok(c, levels):  # однозначные критерии совпали с разметкой
            return all(levels[RB.index(k)] == c["expected"][k] for k in c.get("clear", []))
        diffs = case_diffs({c["id"]: c for c in data["cases"]}, summary, clear_ok)
        text = _capture(print_rubric, summary)
        print(f"Рубрика: идей {len(data['cases'])}, повторов {args.repeat}\n{text}")
        sections.append((f"Рубрика ({args.split}, {len(data['cases'])} идей × {args.repeat})",
                         f"```\n{text}```\n\n" + _diff_text(diffs, lambda xs: " | ".join(
                             "".join(str(v or "-") for v in x) for x in xs))))
        payload["rubric"] = {"rows": rows, "summary": summary}
    if "cloud" in suites:
        sets = load(FAMILY_CASES)["sets"]
        if args.samples:
            rows = build_cloud_corpus(ai, lib, meter, sets, args.samples, args.prompt)
            corpus = {"about": "Ответы «обычного чат-бота» (Р3) на задачи eval/family_cases.json — настоящие вызовы "
                               "модели. Разметка ответов — как в сервисе: словарь LIB-02, затем классификатор Р1. "
                               "Это корпус для проверки правил облака, а не ответы людей.",
                      "provenance": {"model": model, "date": f"{datetime.now():%Y-%m-%d}", "prompt": args.prompt,
                                     "code": code_version(), "providers": providers(meter.calls),
                                     "cost": cost_of(meter.calls, price)},
                      "samples": rows}
            target = CLOUD_CORPUS if args.prompt == "v1" else CLOUD_CORPUS.with_name(f"cloud_corpus_{args.prompt}.json")
            target.write_text(json.dumps(corpus, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"Корпус сохранён: {target} ({len(rows)} ответов чат-бота)")
        summary, reference = score_cloud(lib, load(CLOUD_CORPUS))
        text = _capture(print_cloud, summary)
        print(text)
        sections.append(("Облако очевидного", f"```\n{text}```\nЭталон частотности — доля независимых ответов "
                                               f"чат-бота, где встретилось семейство (корпус `{CLOUD_CORPUS.name}`)."))
        payload["cloud"] = {"summary": summary, "reference": reference}
    if "pattern" in suites:
        data = load(PATTERN_CASES)
        arms = arms or ["v1", "v2"]
        rows = run_pattern(ai, meter, data, args.repeat, arms)
        summary = score_pattern(data, rows)
        lines = [f"Что общего у первых идей (Р16): случаев {len(data['cases'])} × {args.repeat}."]
        for arm, s in summary.items():
            lines.append(f"  {arm}: ответов {s['n']}, сбоев {s['failed']}. Вердикт допустим: "
                         f"{_fmt(s['verdict_ok'], s['n'])}; решение «колея → смена механизма» как в разметке: "
                         f"{_fmt(s['rut_ok'], s['n'])} (ложная колея: {s['false_rut']}, пропущенная: "
                         f"{s['missed_rut']}). Неустойчивы: {', '.join(s['unstable']) or 'нет'}."
                         + "".join(f"\n    {e}" for e in s["errors"]))
        text = "\n".join(lines)
        print(text)
        sections.append(("Что общего у первых идей (Р16)", text))
        payload["pattern"] = {"rows": rows, "summary": summary}
    if "outcome" in suites:
        data = load(OUTCOME_CASES)
        rows = run_outcome(ai, lib, meter, data, args.repeat)
        s = score_outcome(data, rows)
        text = (f"Результат упражнения (Р16 v2 → Р12 v3 → outcome_verdict): случаев {len(data['cases'])} × "
                f"{args.repeat}. Вывод допустим: {_fmt(s['ok'], s['n'])}; ложный «механизм изменился»: "
                f"{s['false_success']}." + "".join(f"\n  {e}" for e in s["errors"]))
        print(text)
        sections.append(("Результат упражнения «смена механизма»", text))
        payload["outcome"] = {"rows": rows, "summary": s}
    if "overlap" in suites:
        data = load(MIRROR_CASES)
        data["cases"] = _split(data["cases"], args.split)
        rows = run_overlap(ai, meter, data, load(ep.SHIFT_CASES)["sets"],
                           arms if args.role == "overlap" and arms else ["detector", "v1"], args.repeat)
        summary = score_overlap_cases(data, rows)
        text = _capture(print_overlap, summary)
        print(f"Сверка с чат-ботом: сравнений {len(data['cases'])}, повторов {args.repeat}\n{text}")
        diffs = None
        if "detector" in summary and "v1" in summary:  # «до» — детектор сдвига в роли сверки, «после» — Р15
            diffs = case_diffs({c["id"]: c for c in data["cases"]},
                               {"v1": summary["detector"], "v2": summary["v1"]}, lambda c, got: got == c["covered"])
        sections.append((f"Сверка с чат-ботом ({args.split}, {len(data['cases'])} сравнений × {args.repeat})",
                         f"```\n{text}```\n\n" + _diff_text(diffs, lambda xs: "/".join("да" if x else "нет"
                                                                                       for x in xs))))
        payload["overlap"] = {"rows": rows, "summary": summary}
    if "mirror" in suites:
        path = CLOUD_CORPUS if args.prompt == "v1" else CLOUD_CORPUS.with_name(f"cloud_corpus_{args.prompt}.json")
        rows = run_chatbot_overlap(ai, meter, load(path), load(ep.SHIFT_CASES), args.samples or 4)
        summary = score_overlap(rows)
        text = (f"Чат-бот (промпт {args.prompt}, корпус {path.name}) против «сдвиговых» идей эталона детектора: "
                f"идей {summary['ideas']}, сравнений {summary['rows']}, сбоев {summary['failed']}.\n"
                f"Механизм уже был у чат-бота: {summary['covered_rows']} сравнений из {summary['rows']}; "
                f"средняя доля по идее {summary['mean_share']}.\n"
                f"Идеи, чей механизм чат-бот предлагал больше чем в половине ответов: "
                f"{', '.join(summary['ideas_covered_majority']) or 'нет'}.\n"
                f"Хоть раз: {', '.join(summary['ideas_covered_ever']) or 'нет'}.")
        print(text)
        sections.append(("Честность зеркала: предлагал ли чат-бот тот же механизм", text))
        payload["mirror"] = {"rows": rows, "summary": summary}
    payload = {"model": model, "code": code_version(), "providers": providers(meter.calls),
               "cost": cost_of(meter.calls, price), "pricing_usd_per_token": price, **payload}
    md, js = write_report(f"Eval: {args.role}", sections, payload, f"{args.role}_{args.split}")
    print(f"\nМодель {model}; вызовов {payload['cost']['calls']}, ошибок {payload['cost']['errors']}, "
          f"токенов {payload['cost']['prompt_tokens']}+{payload['cost']['completion_tokens']}, "
          f"стоимость ${payload['cost']['usd']}; провайдеры {payload['providers']}; "
          f"время {time.monotonic() - started:.0f} с")
    print(f"Отчёт: {md}\nОтветы целиком: {js}")
