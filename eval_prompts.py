"""Эталонная проверка промптов на настоящей модели. В unittest не входит: ходит в сеть и стоит денег
(прогон — десятки или сотни вызовов DeepSeek, центы).

    .venv/Scripts/python eval_prompts.py shift --split holdout --repeat 2
        детектор сдвига (Р12): v1 (прежний) против v2 (рабочий с 13.10) на отложенных случаях, каждый дважды
    .venv/Scripts/python eval_prompts.py shift --rescore eval/results/<файл>.json
        пересчитать сохранённый прогон по текущей разметке эталона — без запросов к модели
    .venv/Scripts/python eval_prompts.py lenses --split holdout --repeat 2
        три линзы (Р13) без первых идей и с ними: слепой судья, словарь LIB-02 и файл для ручной оценки командой

Ключ — из settings.env, как у сервиса. Все ответы целиком — в eval/results/ (в git не попадают).
Эталоны — eval/*.json: дополняйте случаями из пилота, где модель ошиблась."""

import argparse
import hashlib
import json
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import ai_roles
import engine
import library
import llm

HERE = Path(__file__).resolve().parent
SHIFT_CASES = HERE / "eval" / "shift_cases.json"
LENS_CASES = HERE / "eval" / "lens_cases.json"
RESULTS = HERE / "eval" / "results"
WORKERS = 4


class MeteredClient:
    """Свой клиент на каждый поток: счётчик токенов последнего ответа у клиента общий, и в параллели
    его перезаписал бы соседний вызов.

    Журнал вызова (EVAL_PLAN §4.3): роль, настройки генерации, отпечаток запроса, сырой ответ, ошибка, время,
    токены, провайдер. begin()/end() собирают вызовы одного случая — их кладут в строку результата."""

    available = True

    def __init__(self, settings, make=None):
        self.settings = settings
        self.make = make or llm.make_client
        self.local = threading.local()
        self.lock = threading.Lock()
        self.calls = []

    def begin(self):
        self.local.log = []

    def end(self):
        log, self.local.log = getattr(self.local, "log", None) or [], None
        return log

    def complete(self, system, user, max_tokens=600, temperature=0.3):
        import live_run  # разметка роли по тексту запроса — та же, что в живом прогоне
        client = getattr(self.local, "client", None)
        if client is None:
            client = self.local.client = self.make(self.settings)
        start, text, error = time.monotonic(), None, None
        try:
            text = client.complete(system, user, max_tokens=max_tokens, temperature=temperature)
            return text
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            meta = dict(getattr(client, "last_meta", None) or {})
            entry = {"role": live_run.detect_role(system, user), "seconds": round(time.monotonic() - start, 2),
                     "usage": dict(getattr(client, "last_usage", None) or {}) if error is None else {},
                     "temperature": temperature, "max_tokens": max_tokens,
                     "prompt_sha1": hashlib.sha1((system + "\n" + user).encode("utf-8")).hexdigest()[:12],
                     "provider": meta.get("provider"), "model": meta.get("model"), "attempts": meta.get("attempts"),
                     "error": error, "raw": text}
            with self.lock:
                self.calls.append(entry)
            log = getattr(self.local, "log", None)
            if log is not None:
                log.append(entry)


def timed(fn):
    start = time.monotonic()
    result = fn()
    return result, time.monotonic() - start


def parallel(fn, items):
    with ThreadPoolExecutor(WORKERS) as pool:
        return list(pool.map(fn, items))


def save(name, payload):
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"{datetime.now():%Y%m%d_%H%M%S}_{name}"
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, indent=1)
    out.write_text(text, encoding="utf-8")
    return out


def base_with_xray(ai, lib, sets):
    """Прежние идеи разбираем так же, как сервис после первых идей: рентгеном (Р11)."""
    jobs = [(name, text) for name, s in sets.items() for text in s["base"]]
    xrays = parallel(lambda j: ai_roles.xray(ai, lib, j[1], sets[j[0]]["task"]), jobs)
    base = {name: [] for name in sets}
    for (name, text), x in zip(jobs, xrays):
        base[name].append({"text": text, "xray": (x or {}).get("xray")})
    return base


# ---------- Р12. Детектор сдвига ----------

def run_shift(ai, lib, data, repeat, arm_names):
    sets = {name: s for name, s in data["sets"].items() if any(c["set"] == name for c in data["cases"])}
    base = base_with_xray(ai, lib, sets)
    arms = {
        # v1 — прежний рабочий (до 13.10): рентген новой идеи шёл параллельно и в детектор не попадал
        "v1": lambda c, b, s: timed(lambda: ai_roles.shift_check(ai, b, c["text"], None)),
        # v1 с рентгеном новой идеи (последовательно — почти вдвое дольше, выигрыша не дал)
        "v1_xray": lambda c, b, s: timed(lambda: ai_roles.shift_check(
            ai, b, c["text"], (ai_roles.xray(ai, lib, c["text"], s["task"]) or {}).get("xray"))),
        # v2 — рабочий с 13.10: механизм новой идеи восстанавливает сам детектор, знает задачу, цель, измерения
        "v2": lambda c, b, s: timed(lambda: ai_roles.shift_check_v2(ai, b, c["text"], s["task"], s["goal"])),
        # v3 — рабочий с 19.10: то же + способ воздействия новой идеи, цель, «что открывает», риск (тот же вызов)
        "v3": lambda c, b, s: timed(lambda: ai_roles.shift_check_v3(ai, b, c["text"], s["task"], s["goal"])),
    }
    arms = {name: fn for name, fn in arms.items() if name in arm_names}
    jobs = [(arm, case, r) for r in range(repeat) for case in data["cases"] for arm in arms]

    def work(job):
        arm, case, r = job
        result, seconds = arms[arm](case, base[case["set"]], sets[case["set"]])
        return {"arm": arm, "case": case["id"], "round": r, "seconds": round(seconds, 2), "result": result}

    return base, parallel(work, jobs)


def score_shift(data, rows):
    """Точность, precision и recall по сдвигам — отдельно по уверенным и спорным случаям; ошибки по классам.

    Главная ошибка детектора — ложный сдвиг, поэтому precision (доля объявленных сдвигов, которые
    подтверждает разметка) важнее общей точности."""
    cases = {c["id"]: c for c in data["cases"]}
    summary = {}
    for row in rows:
        case = cases.get(row["case"])
        if case is None:
            continue
        res = row["result"]
        s = summary.setdefault(row["arm"], {"n": 0, "seconds": 0.0, "failed": 0, "same_as_ok": 0, "same_as_n": 0,
                                            "sure": {"tp": 0, "fp": 0, "fn": 0, "tn": 0},
                                            "disputed": {"tp": 0, "fp": 0, "fn": 0, "tn": 0},
                                            "by_kind": {}, "errors": []})
        s["n"] += 1
        s["seconds"] += row["seconds"]
        if not res:
            s["failed"] += 1
            s["errors"].append(f"{case['id']}: нет ответа")
            continue
        got, want = res["shift"] == "механизм", case["expected"] == "shift"
        cell = ("tp" if want else "fp") if got else ("fn" if want else "tn")
        s["disputed" if case.get("borderline") else "sure"][cell] += 1
        k = s["by_kind"].setdefault(case.get("kind", "?"), {"n": 0, "wrong": 0})
        k["n"] += 1
        if got != want:
            k["wrong"] += 1
            what = "ложный сдвиг" if got else "пропущен сдвиг"
            mark = " [спорный]" if case.get("borderline") else ""
            s["errors"].append(f"{case['id']}{mark} {what} ({case.get('kind')}): «{case['text']}» — "
                               f"{res.get('explanation')}")
        if not want and not got:
            s["same_as_n"] += 1
            s["same_as_ok"] += res.get("same_as") in case.get("same_as", [])
    for arm, s in summary.items():
        answers = {}
        for row in rows:
            if row["arm"] == arm and row["result"]:
                answers.setdefault(row["case"], set()).add(row["result"]["shift"] == "механизм")
        s["unstable"] = sorted(c for c, a in answers.items() if len(a) > 1)
    return summary


def _ratio(a, b):
    return f"{a}/{b} ({100 * a / b:.0f}%)" if b else "—"


def print_shift(summary):
    print(f"{'вариант':<10} {'точность (увер.)':>18} {'precision':>16} {'recall':>16} {'спорные верно':>14} "
          f"{'нет ответа':>11} {'сек':>5}")
    for arm, s in summary.items():
        m, d = s["sure"], s["disputed"]
        print(f"{arm:<10} {_ratio(m['tp'] + m['tn'], sum(m.values())):>18} {_ratio(m['tp'], m['tp'] + m['fp']):>16} "
              f"{_ratio(m['tp'], m['tp'] + m['fn']):>16} {_ratio(d['tp'] + d['tn'], sum(d.values())):>14} "
              f"{s['failed']:>11} {s['seconds'] / max(s['n'], 1):>5.1f}")
    for arm, s in summary.items():
        kinds = ", ".join(f"{k}: {v['wrong']}/{v['n']}" for k, v in sorted(s["by_kind"].items()))
        print(f"{arm}: ошибки по классам — {kinds}")
        if s["unstable"]:
            print(f"{arm}: разные ответы между повторами — {', '.join(s['unstable'])}")
    for arm, s in summary.items():
        if s["errors"]:
            print(f"\n{arm}:")
            for e in s["errors"]:
                print("  " + e)


# ---------- Р13. Три линзы ----------

JUDGE_SYSTEM = (
    "Ты — строгий эксперт-методолог тренажёра нестандартного мышления. Тебе дают задачу, первые идеи человека и "
    "три «линзы» — наблюдения о главном препятствии с вопросом. Для КАЖДОЙ линзы ответь: "
    "addressed — касаются ли первые идеи человека этого препятствия: yes — хотя бы одна идея прямо направлена "
    "против него, partly — косвенно, no — ни одна идея его не касается; "
    "rut — возвращает ли вопрос линзы к механизму, который уже есть в первых идеях: yes или no; "
    "specific — относится ли наблюдение именно к этой задаче (yes), а не годится почти для любого бизнеса (no); "
    "solution — содержит ли формулировка готовое решение или совет: yes или no. "
    "Не оценивай, хороша ли линза в целом. Отвечай только JSON: "
    '{"system": {"addressed": "yes|partly|no", "rut": "yes|no", "specific": "yes|no", "solution": "yes|no"}, '
    '"behavior": {...}, "value": {...}}'
)


def judge_lenses(ai, task, ideas, lenses):
    def validate(d):
        out = {}
        for lens in ai_roles.LENSES:
            item = d.get(lens) or {}
            out[lens] = {"addressed": item.get("addressed") if item.get("addressed") in ("yes", "partly", "no")
                         else None}
            for key in ("rut", "specific", "solution"):
                out[lens][key] = item.get(key) if item.get(key) in ("yes", "no") else None
        return out
    listing = "\n".join(f"{i}. {t}" for i, t in enumerate(ideas, 1))
    lens_text = "\n".join(f"{name}: препятствие — {lenses[name]['barrier']}; вопрос — {lenses[name]['question']}"
                          for name in ai_roles.LENSES)
    user = f"Задача: {task}\n\nПервые идеи человека:\n{listing}\n\nЛинзы:\n{lens_text}"
    return ai.ask_json("judge_lenses", JUDGE_SYSTEM, user, validate, max_tokens=300, temperature=0)


def rut_by_dictionary(lib, ideas, lenses):
    """Без ИИ: сколько линз словарь LIB-02 относит к семейству, которое уже есть в первых идеях."""
    rut = {lib.classifier.classify(t)[0] for t in ideas} - {None}
    rut = {code[:3] for code in rut}
    hits = 0
    for name in ai_roles.LENSES:
        code, _ = lib.classifier.classify(f"{lenses[name]['barrier']} {lenses[name]['question']}")
        hits += bool(code and code[:3] in rut)
    return hits


def overlap(lenses):
    """Насколько три препятствия похожи друг на друга: максимальная доля общих основ слов в паре."""
    stems = [engine.stems(lenses[name]["barrier"]) for name in ai_roles.LENSES]
    best = 0.0
    for i in range(3):
        for j in range(i + 1, 3):
            union = stems[i] | stems[j]
            if union:
                best = max(best, len(stems[i] & stems[j]) / len(union))
    return best


def run_lenses(ai, lib, sets, cases, repeat):
    # current — рабочий промпт (без первых идей); with_ideas — кандидат: первые идеи и сначала «covered»
    arms = {"current": False, "with_ideas": True}
    jobs = [(arm, case, r) for r in range(repeat) for case in cases for arm in arms]

    def work(job):
        arm, case, r = job
        s = sets[case["set"]]
        lenses, seconds = timed(lambda: ai_roles.lenses(ai, lib, s["task"], s["goal"], case["card"],
                                                        s["base"] if arms[arm] else None))
        verdict = judge_lenses(ai, s["task"], s["base"], lenses) if lenses else None
        return {"arm": arm, "set": case["set"], "round": r, "seconds": round(seconds, 2), "lenses": lenses,
                "judge": verdict, "rut_dict": rut_by_dictionary(lib, s["base"], lenses) if lenses else None,
                "overlap": round(overlap(lenses), 2) if lenses else None}

    return parallel(work, jobs)


def score_lenses(rows):
    summary = {}
    for row in rows:
        s = summary.setdefault(row["arm"], {"runs": 0, "failed": 0, "lenses": 0, "not_addressed": 0, "partly": 0,
                                            "addressed": 0, "rut": 0, "specific": 0, "solution": 0, "rut_dict": 0,
                                            "overlap": 0.0, "seconds": 0.0, "judged": 0})
        s["runs"] += 1
        s["seconds"] += row["seconds"]
        if not row["lenses"]:
            s["failed"] += 1
            continue
        s["rut_dict"] += row["rut_dict"]
        s["overlap"] += row["overlap"]
        for name in ai_roles.LENSES:
            s["lenses"] += 1
            v = (row["judge"] or {}).get(name) or {}
            if v.get("addressed"):
                s["judged"] += 1
                s[{"no": "not_addressed", "partly": "partly", "yes": "addressed"}[v["addressed"]]] += 1
            s["rut"] += v.get("rut") == "yes"
            s["specific"] += v.get("specific") == "yes"
            s["solution"] += v.get("solution") == "yes"
    return summary


def print_lenses(summary):
    print(f"{'вариант':<11} {'идеи НЕ касаются':>17} {'частично':>10} {'уже видел':>10} {'в колею (судья)':>16} "
          f"{'в колею (словарь)':>18} {'конкретно':>10} {'готовое решение':>16} {'сбой':>5} {'сек':>5}")
    for arm, s in summary.items():
        n, j = s["lenses"], s["judged"]
        print(f"{arm:<11} {_ratio(s['not_addressed'], j):>17} {_ratio(s['partly'], j):>10} "
              f"{_ratio(s['addressed'], j):>10} {_ratio(s['rut'], n):>16} {_ratio(s['rut_dict'], n):>18} "
              f"{_ratio(s['specific'], n):>10} {_ratio(s['solution'], n):>16} {s['failed']:>5} "
              f"{s['seconds'] / max(s['runs'], 1):>5.1f}")
    for arm, s in summary.items():
        ok = s["runs"] - s["failed"]
        print(f"{arm}: максимальная похожесть двух препятствий в наборе — в среднем {s['overlap'] / max(ok, 1):.2f}")


def review_sheet(sets, rows):
    """Лист для ручной оценки командой: варианты перемешаны и названы A/B, ключ — отдельным файлом."""
    rng = random.Random(13)
    lines, key = ["# Линзы: слепая оценка", "",
                  "Для каждой линзы отметьте: 2 — показывает то, чего первые идеи не касаются, и помогает искать; "
                  "1 — частично; 0 — уже было в идеях, общее место или готовое решение.", ""], {}
    by_set = {}
    for row in rows:
        if row["round"] == 0 and row["lenses"]:
            by_set.setdefault(row["set"], []).append(row)
    for name, variants in by_set.items():
        rng.shuffle(variants)
        s = sets[name]
        lines += [f"## {name}", "", f"Задача: {s['task']}", f"Первые идеи: {'; '.join(s['base'])}", ""]
        for label, row in zip("AB", variants):
            key[f"{name}/{label}"] = row["arm"]
            lines.append(f"### Вариант {label}")
            for lens in ai_roles.LENSES:
                lines.append(f"- {ai_roles.LENS_NAMES[lens]}: {row['lenses'][lens]['barrier']} — "
                             f"*{row['lenses'][lens]['question']}* — оценка: __")
            lines.append("")
    return "\n".join(lines), key


# ---------- запуск ----------

def main():
    import eval_quality  # наборы качества LLM (docs/llm-quality): семейства, рубрика, облако, задержки, сравнение
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=["shift", "lenses", *eval_quality.ROLES])
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--split", choices=["all", "dev", "holdout"], default="all")
    parser.add_argument("--arms", default=None, help="детектор сдвига: v1, v1_xray, v2 (по умолчанию v1,v2); "
                                                     "семейства: dict, v1, v2; рубрика: v1, v2")
    parser.add_argument("--rescore", help="пересчитать сохранённый прогон детектора без запросов к модели")
    parser.add_argument("--suites", default="family,rubric,cloud,overlap,latency", help="для compare: какие наборы")
    parser.add_argument("--samples", type=int, default=0, help="cloud: сколько ответов чат-бота на задачу запросить")
    parser.add_argument("--prompt", default="v1", help="cloud: версия промпта чат-бота для корпуса (v1, v2)")
    parser.add_argument("--offline", action="store_true", help="cloud: только правила по сохранённому корпусу")
    parser.add_argument("--scale", type=float, default=0.25, help="latency: доля реального времени задержек")
    parser.add_argument("--out", help="review: куда положить таблицы для разметки")
    parser.add_argument("--score", help="review: заполненная таблица — посчитать согласие разметчиков")
    parser.add_argument("--no-head", dest="head", action="store_false",
                        help="latency: без замера исходной реализации (снимок git HEAD)")
    parser.add_argument("--think", type=float, default=120, help="latency: секунд человек пишет первые идеи")
    args = parser.parse_args()
    if args.role in eval_quality.ROLES:
        return eval_quality.cli(args)
    args.arms = args.arms or "v1,v2"
    shift_data = json.loads(SHIFT_CASES.read_text(encoding="utf-8"))
    if args.rescore:
        saved = json.loads(Path(args.rescore).read_text(encoding="utf-8"))
        print(f"Пересчёт {args.rescore} по текущей разметке эталона.\n")
        print_shift(score_shift(shift_data, saved["rows"]))
        return
    settings = llm.load_settings()
    if not settings.get("OPENROUTER_API_KEY"):
        sys.exit("Нет ключа в settings.env — прогон на настоящей модели невозможен.")
    meter = MeteredClient(settings)
    ai = llm.AI(meter)
    lib = library.Library()
    model = settings.get("OPENROUTER_MODEL") or llm.DEFAULT_MODEL
    started = time.monotonic()
    if args.role == "shift":
        if args.split != "all":
            shift_data["cases"] = [c for c in shift_data["cases"] if c.get("split") == args.split]
        base, rows = run_shift(ai, lib, shift_data, args.repeat, args.arms.split(","))
        summary = score_shift(shift_data, rows)
        payload = {"model": model, "base": base, "rows": rows, "summary": summary}
        title = f"Детектор сдвига, эталон: {args.split}, случаев {len(shift_data['cases'])}"
    else:
        cases = json.loads(LENS_CASES.read_text(encoding="utf-8"))["cases"]
        if args.split != "all":
            cases = [c for c in cases if c["split"] == args.split]
        rows = run_lenses(ai, lib, shift_data["sets"], cases, args.repeat)
        summary = score_lenses(rows)
        payload = {"model": model, "rows": rows, "summary": summary}
        title = f"Три линзы, сценарии: {args.split}, задач {len(cases)}"
    tokens = sum((c["usage"].get("total_tokens") or 0) for c in meter.calls)
    payload.update(calls=len(meter.calls), tokens=tokens)
    print(f"{title}, повторов {args.repeat}.")
    print(f"Модель: {model}. Вызовов: {len(meter.calls)}, токенов: {tokens}, "
          f"время прогона: {time.monotonic() - started:.0f} с\n")
    if args.role == "shift":
        print_shift(summary)
    else:
        print_lenses(summary)
        sheet, key = review_sheet(shift_data["sets"], rows)
        print(f"\nЛист для ручной оценки: {save(f'lenses_{args.split}_review.md', sheet)}")
        print(f"Ключ к нему: {save(f'lenses_{args.split}_key.json', key)}")
    print(f"\nОтветы целиком: {save(f'{args.role}_{args.split}.json', payload)}")


if __name__ == "__main__":
    main()
