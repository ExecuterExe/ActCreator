"""Тренировочная сессия: от выбора режима до проверки за 48 часов (B1 §4, B2–B4).

Состояние сессии хранится в sessions.state; на каждый шаг пускаем только если
сессия находится на нём (перескочить нельзя). Брошенная больше 24 часов назад
сессия помечается «abandoned», её идеи попадают в банк как черновики.
"""

import os
import re
import sqlite3
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, url_for
from flask import session as flask_session

import ai_roles
import engine
import llm
import research
from common import (METHODOLOGY_VERSION, access_status, ai, card_text, dumps, get_state, lib, loads,
                    log_event, login_required, mark_shown, now, parse_time, recently_shown, update_state)
from db import get_db, now_iso

bp = Blueprint("train", __name__)

STEPS = ["task", "first", "mirror", "tool", "idea", "eval", "premortem", "route", "check", "done"]
# Сокращённый сценарий C (эксперимент, docs/product-value/SHORT_FLOW.md): задача → механизм первых идей → один
# вызов → сравнение вариантов под ограничения → шаг за 48 часов. Включается флагом сессии flow = «short».
SHORT_STEPS = ["task", "first", "mirror", "tool", "idea", "choose", "check", "done"]
ALL_STEPS = STEPS + ["choose"]
STEP_TITLES = {"task": "Задача", "first": "Первые идеи", "mirror": "Зеркало", "tool": "Инструмент",
               "idea": "Своя идея", "eval": "Оценка", "premortem": "Проверка на прочность",
               "route": "Маршрут мысли", "check": "Проверка за 48 часов", "done": "Готово",
               "choose": "Выбор решения"}
ABANDON_AFTER = timedelta(hours=24)
HANDLERS = {}


def step_handler(name):
    def deco(fn):
        HANDLERS[name] = fn
        return fn
    return deco


# ---------- сессия ----------

def load_session(sid):
    db = get_db()
    s = db.execute("SELECT * FROM sessions WHERE id = ? AND user_id = ?", (sid, g.user["id"])).fetchone()
    if s is None:
        abort(404)
    updated = parse_time(s["updated_at"])
    if s["state"] not in ("done", "abandoned") and updated and now() - updated > ABANDON_AFTER:
        db.execute("UPDATE sessions SET state = 'abandoned' WHERE id = ?", (sid,))
        db.execute("UPDATE ideas SET status = 'черновик' WHERE session_id = ? AND status IS NULL", (sid,))
        db.commit()
        log_event("session_abandoned", {"session": sid, "at_step": s["state"]})
        s = db.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
    return s


def save(sid, **fields):
    fields["updated_at"] = now_iso()
    cols = ", ".join(f"{k} = ?" for k in fields)
    db = get_db()
    db.execute(f"UPDATE sessions SET {cols} WHERE id = ?", (*fields.values(), sid))
    db.commit()


def go(sid, state):
    return redirect(url_for("train.step", sid=sid, step=state))


def ctx(s):
    """Общие данные сессии для шаблонов."""
    return {"s": s, "sid": s["id"], "task_data": loads(s["task_json"], {}), "card": loads(s["card_json"], {}),
            "flags": loads(s["flags_json"], {}), "steps": flow_steps(s)[:-1],
            "titles": STEP_TITLES,
            "current": s["state"]}


def flow_steps(s):
    if not is_short(s):
        return STEPS
    if loads(s["flags_json"], {}).get("pm_opt"):  # человек сам выбрал проверку на прочность
        return SHORT_STEPS[:SHORT_STEPS.index("check")] + ["premortem"] + SHORT_STEPS[SHORT_STEPS.index("check"):]
    return SHORT_STEPS


def is_short(s):
    return loads(s["flags_json"], {}).get("flow") == "short"


def default_flow():
    """Путь новой тренировки. С 18.10 по умолчанию короткий: задача → колея первых идей → одно задание → своя
    идея → выбор под ограничения → проверка на 48 часов; пре-мортем — по желанию на шаге выбора. Полный путь
    (оценка, пре-мортем, маршрут) — DEFAULT_FLOW=full; сессии, начатые раньше, идут прежним путём."""
    flow = current_app.config.get("DEFAULT_FLOW") or os.environ.get("ACTKREATOR_FLOW") or "short"
    return "full" if flow == "full" else "short"


def flags_update(s, **values):
    flags = loads(s["flags_json"], {})
    flags.update(values)
    save(s["id"], flags_json=dumps(flags))
    return flags


def ideas_of(sid, stage=None):
    db = get_db()
    if stage:
        return db.execute("SELECT * FROM ideas WHERE session_id = ? AND stage = ? ORDER BY ord, id",
                          (sid, stage)).fetchall()
    return db.execute("SELECT * FROM ideas WHERE session_id = ? ORDER BY id", (sid,)).fetchall()


@bp.get("/s/<int:sid>")
@login_required
def session_home(sid):
    s = load_session(sid)
    if s["state"] == "abandoned":
        flash("Сессия была брошена больше суток назад. Идеи сохранены в банке идей как черновики.")
        return redirect(url_for("train.start"))
    return go(sid, s["state"])


@bp.route("/s/<int:sid>/<step>", methods=["GET", "POST"])
@login_required
def step(sid, step):
    if step not in ALL_STEPS:
        abort(404)
    s = load_session(sid)
    if s["state"] == "abandoned":
        return redirect(url_for("train.session_home", sid=sid))
    if s["state"] != step:
        return go(sid, s["state"])
    handler = HANDLERS.get(step)
    if handler is None:
        abort(501)
    return handler(s)


# ---------- старт, карточка, оплата ----------

@bp.get("/train")
@login_required
def start():
    uid = g.user["id"]
    state = get_state(uid)
    if not state["card_json"]:
        return redirect(url_for("train.card", next=url_for("train.start")))
    access = access_status(uid)
    db = get_db()
    open_session = db.execute(
        "SELECT id, state FROM sessions WHERE user_id = ? AND state NOT IN ('done','abandoned') ORDER BY id DESC LIMIT 1",
        (uid,)).fetchone()
    return render_template("train/start.html", state=state, access=access, open_session=open_session,
                           card=loads(state["card_json"], {}), card_summary=card_text(loads(state["card_json"], {})))


@bp.post("/train/start")
@login_required
def start_session():
    uid = g.user["id"]
    mode = request.form.get("mode")
    if mode not in ("task", "practice"):
        abort(400)
    src = request.form.get("src") if request.form.get("src") in ("self", "reminder", "test") else "self"
    if flask_session.pop("after_outcome", None):
        src = "outcome_link"  # вход после ответа на вопрос об исходе (B5 §1.4)
    state = get_state(uid)
    if not state["card_json"]:
        return redirect(url_for("train.card", next=url_for("train.start")))
    access = access_status(uid)
    if not access["allowed"]:
        log_event("paywall_shown", {"reason": "trial_over"})
        return redirect(url_for("train.paywall"))
    state = get_state(uid)
    if not state["trial_started_at"]:
        update_state(uid, trial_started_at=now_iso())
    card = loads(state["card_json"], {})
    task = {}
    if mode == "practice":
        used = recently_shown(uid, "LIB-11", last_sessions=1000)
        max_diff = {1: 2, 2: 3, 3: 3}.get(state["level"], 3)
        pool = [t for t in lib().training if t["id"] not in used and t["difficulty"] <= max_diff] or lib().training
        rng = engine.rng_for(uid, len(used))
        t = rng.choice(pool)
        question = re.findall(r"[^.?!]*\?", t["text"])
        task = {"text": t["text"], "goal": (question[-1].strip() if question else t["text"]),
                "bans": "", "in_domain": True, "practice_id": t["id"], "legend": t["legend_text"]}
        card = t["legend"]
    db = get_db()
    n = db.execute("SELECT COUNT(*) FROM sessions WHERE user_id = ?", (uid,)).fetchone()[0] + 1
    ts = now_iso()
    cur = db.execute(
        "INSERT INTO sessions (user_id, mode, state, entry_source, level, methodology_version, task_json, card_json, "
        "flags_json, started_at, updated_at) VALUES (?, ?, 'task', ?, ?, ?, ?, ?, ?, ?, ?)",
        (uid, mode, src, state["level"], METHODOLOGY_VERSION, dumps(task), dumps(card),
         dumps({"flow": default_flow()}), ts, ts))
    db.commit()
    sid = cur.lastrowid
    if mode == "practice":
        mark_shown(uid, "LIB-11", task["practice_id"], sid)
    log_event("session_started", {"session": sid, "mode": mode, "source": src, "n": n})
    prev = request.form.get("prev", type=int)
    if mode == "task" and prev:  # «продолжить работу над задачей» из банка идей (B5 §1.5)
        return redirect(url_for("train.step", sid=sid, step="task", prev=prev))
    return go(sid, "task")


def _card_fields(kind):
    fields = lib().card_fields
    if kind == "short":  # R10 и R11 показываются одним полем (B1, §4)
        out = [f for f in fields if f["short"] and f["id"] != "R11"]
        return [dict(f, label=lib().t("UI-009") if f["id"] == "R10" else f["label"]) for f in out]
    return list(fields)


@bp.route("/train/card", methods=["GET", "POST"])
@login_required
def card():
    uid = g.user["id"]
    state = get_state(uid)
    current = loads(state["card_json"], {})
    kind = request.values.get("kind") or ("full" if current else "short")
    if kind not in ("short", "full"):
        kind = "short"
    fields = _card_fields(kind)
    need_notice = ai().available and not state["ai_notice_at"]
    errors = {}
    if request.method == "POST":
        new = dict(current)
        for f in fields:
            if f["type"] == "multi":
                values = [v for v in request.form.getlist(f["id"]) if v in f["options"]]
                new[f["id"]] = values
            elif f["type"] == "single":
                value = request.form.get(f["id"])
                new[f["id"]] = value if value in f["options"] else None
            else:
                new[f["id"]] = (request.form.get(f["id"]) or "").strip()[:500]
            if f.get("with_text"):
                new[f["id"] + "_text"] = (request.form.get(f["id"] + "_text") or "").strip()[:300]
        for required in ("R01", "R02"):
            if not new.get(required):
                errors[required] = "Выберите вариант."
        if need_notice and not request.form.get("ai_notice"):
            errors["ai_notice"] = "Подтвердите, что прочитали предупреждение."
        if not errors:
            new["_kind"] = "full" if kind == "full" or current.get("_kind") == "full" else "short"
            fields_upd = {"card_json": dumps(new), "card_kind": new["_kind"], "card_updated_at": now_iso()}
            if need_notice:
                fields_upd["ai_notice_at"] = now_iso()
            update_state(uid, **fields_upd)
            log_event("resource_card_saved", {"kind": kind})
            target = request.args.get("next") or url_for("train.start")
            if not target.startswith("/") or target.startswith("//"):
                target = url_for("train.start")
            return redirect(target)
        current = new
    return render_template("train/card.html", fields=fields, values=current, kind=kind,
                           need_notice=need_notice, errors=errors)


@bp.route("/paywall", methods=["GET", "POST"])
@login_required
def paywall():
    uid = g.user["id"]
    clicked = False
    if request.method == "POST":
        log_event("paywall_click", {})
        clicked = True
    db = get_db()
    ideas = db.execute("SELECT COUNT(*) FROM ideas i JOIN sessions s ON s.id = i.session_id "
                       "WHERE s.user_id = ? AND i.stage = 'final'", (uid,)).fetchone()[0]
    return render_template("train/paywall.html", access=access_status(uid), ideas=ideas, clicked=clicked)


# ---------- С1. Постановка задачи (B2, §1) ----------

GOAL_HINT = re.compile(r"\d|недел|месяц|день|дня|дней|срок|квартал|год|до конца|к концу")
GENERATION_REQUEST = re.compile(
    r"\b(дай|дайте|придумай|придумайте|предложи|предложите|напиши|напишите|подскажи|подскажите)\s+"
    r"(мне\s+|за\s+меня\s+|сам\s+)?(готов|иде|ответ|вариант|решени)|\bпридумай(те)?\s+за\s+меня", re.I)


@step_handler("task")
def task_step(s):
    c = ctx(s)
    td = c["task_data"]
    if s["mode"] == "practice":
        if request.method == "POST":
            save(s["id"], state="first")
            log_event("task_set", {"session": s["id"], "practice": td.get("practice_id")})
            start_prefetch(s, td)
            return go(s["id"], "first")
        return render_template("train/task_practice.html", **c)

    uid = g.user["id"]
    db = get_db()
    previous = db.execute(
        "SELECT id, task_json FROM sessions WHERE user_id = ? AND mode = 'task' AND id != ? "
        "AND state IN ('done','abandoned') ORDER BY id DESC LIMIT 3", (uid, s["id"])).fetchall()
    previous = [(p["id"], loads(p["task_json"], {})) for p in previous]
    values = dict(td)
    prev_id = request.args.get("prev", type=int)
    if prev_id and request.method == "GET":
        for pid, pt in previous:
            if pid == prev_id:
                values = {"text": pt.get("text"), "goal": pt.get("goal"), "bans": pt.get("bans"),
                          "in_domain": pt.get("in_domain"), "prev_session": pid}
    errors, warning = {}, None
    if request.method == "POST":
        values = {
            "text": (request.form.get("text") or "").strip(),
            "goal": (request.form.get("goal") or "").strip(),
            "bans": (request.form.get("bans") or "").strip()[:300],
            "in_domain": request.form.get("in_domain") == "yes",
            "example": bool(request.form.get("example")),
            "prev_session": request.form.get("prev_session", type=int),
        }
        if not 20 <= len(values["text"]) <= 600:
            errors["text"] = "Опишите задачу в 1–3 предложениях (от 20 до 600 символов)."
        if not 10 <= len(values["goal"]) <= 300:
            errors["goal"] = "Напишите, что должно получиться и к какому сроку."
        if request.form.get("in_domain") not in ("yes", "no"):
            errors["in_domain"] = "Ответьте на вопрос."
        confirmed = bool(request.form.get("confirm"))
        if not errors:
            check = ai_roles.check_task(ai(), values["text"], values["goal"]) if ai().available else None
            if check and check["problem"] == "unethical":
                errors["text"] = lib().t("UI-023")
            elif not confirmed:
                if check and check["problem"] == "not_ideation":
                    warning = lib().t("UI-019")
                elif (check and check["problem"] == "too_general") or (not check and not GOAL_HINT.search(values["goal"])):
                    warning = lib().t("UI-018")
        if not errors and not warning:
            if not values["in_domain"]:
                temp = ai_roles.temp_taxonomy(ai(), values["text"]) if ai().available else None
                if temp:
                    values["temp_taxonomy"] = temp
                else:
                    values["low_precision"] = True
            save(s["id"], task_json=dumps(values), state="first")
            log_event("task_set", {"session": s["id"], "in_domain": values["in_domain"],
                                   "temp_taxonomy": bool(values.get("temp_taxonomy"))})
            start_prefetch(s, values)
            return go(s["id"], "first")
    return render_template("train/task.html", values=values, errors=errors, warning=warning,
                           previous=previous, **c)


# ---------- Таксономия сессии: сферы или временная (A2, §2.1) ----------

class Taxonomy:
    def __init__(self, task_data):
        self.temp = task_data.get("temp_taxonomy")
        self._by = {f["id"]: f for f in self.temp} if self.temp else None

    def codes(self):
        return [f["id"] for f in self.temp] if self.temp else list(lib().family_order)

    def name(self, code):
        if not code:
            return None
        if self.temp:
            return self._by.get(code, {}).get("name", code)
        return lib().family_name(code)

    def mechanism(self, code):
        if self.temp:
            return self._by.get(code, {}).get("mechanism", "")
        return lib().families[code[:3]]["mechanism"]

    def fam(self, code):
        """Код семейства: для сферы — первые 3 символа, для временной — как есть."""
        if not code:
            return None
        return code if self.temp else code[:3]


CLASSIFY_WORKERS = 4  # больше одновременных вызовов — чаще отказ 429 у провайдера (живой прогон 2026-10-12)
NO_CLASS = {"family": None, "subfamily": None, "source": "none", "confidence": None, "explanation": None}


def classify_text(task_data, text):
    return classify_many(task_data, [text])[0]


def classify_many(task_data, texts):
    """Словарь LIB-02 — сразу; что словарь не узнал — в ИИ параллельно.

    Последовательно 6–10 вызовов модели — это 20–40 секунд ожидания на зеркале (замер живого прогона).
    """
    temp = task_data.get("temp_taxonomy")
    results = [None if temp else engine.classify_by_dictionary(lib(), t) for t in texts]
    pending = [i for i, r in enumerate(results) if not r]
    if pending and ai().available:
        library, task_text = lib(), task_data.get("text")
        found = run_ai_parallel([lambda w, t=texts[i]: ai_roles.classify(w, library, t, task_text, temp)
                                 for i in pending])
        for i, r in zip(pending, found):
            results[i] = r
    return [r or dict(NO_CLASS) for r in results]


def run_ai_parallel(jobs):
    """jobs — функции job(worker_ai). Вызовы модели идут параллельно; события пишем в основном потоке,
    потому что у рабочих потоков нет контекста приложения."""
    if not jobs:
        return []
    events = []
    worker = llm.AI(ai().client, on_event=lambda name, props: events.append((name, props)))
    with ThreadPoolExecutor(max_workers=min(CLASSIFY_WORKERS, len(jobs))) as pool:
        results = [f.result() for f in [pool.submit(job, worker) for job in jobs]]
    for name, props in events:
        log_event(name, props)
    return results


# ---------- Предвыборка: материал задачи готовится, пока человек пишет первые идеи ----------
# Линзы и ответы «обычного чат-бота» зависят только от задачи. В живых прогонах 12.10 ответ чат-бота шёл 10–17 с
# и стоял в очереди за рентгеном на шаге «Первые идеи» (ожидание 12–54 с). Теперь их запрос уходит сразу после
# шага «Задача» — у модели есть те 2 минуты, что человек пишет идеи (09 §4; docs/llm-quality/IMPLEMENTATION_PLAN.md).

PREFETCH = True  # False — прежний путь: всё после отправки первых идей (для замера «до/после»)
CLOUD_VERSION = "v2"  # облако очевидного: v1 — engine.common_families по одному ответу чат-бота, v2 — cloud_labels
CHATBOT_OVERLAP = True  # сверка итоговой идеи с ответами обычного чат-бота (Р15) — честность «новизны»
MIRROR_PATTERN = True  # «что общего у ваших идей» в зеркале (Р16) и задание «смена механизма» по нему
PREFETCH_WAIT = 75  # секунд ждать недосчитанную предвыборку, потом — прежний путь
PREFETCH_KEEP = 200  # незабранных предвыборок в памяти (брошенные сессии)
_PREFETCH_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="prefetch")
_PREFETCHED = OrderedDict()  # ключ → (проверочное значение, Future); только память процесса
_PREFETCH_LOCK = threading.Lock()


def run_in_background(key, check, fn):
    """fn(worker) считается в фоне; забрать — take_background(key, check). События ИИ пишутся при выдаче:
    у фонового потока нет контекста приложения. check — что должно совпасть (текст задачи, текст версии)."""
    client = ai().client

    def job():
        events = []
        worker = llm.AI(client, on_event=lambda name, props: events.append((name, props)))
        return fn(worker), events
    with _PREFETCH_LOCK:
        _PREFETCHED[key] = (check, _PREFETCH_POOL.submit(job))
        while len(_PREFETCHED) > PREFETCH_KEEP:
            _PREFETCHED.popitem(last=False)


def take_background(key, check):
    """(True, результат) — фоновый расчёт был и дождались его; (False, None) — считаем прежним путём."""
    with _PREFETCH_LOCK:
        entry = _PREFETCHED.pop(key, None)
    if entry is None or entry[0] != check:
        return False, None
    try:
        result, events = entry[1].result(timeout=PREFETCH_WAIT)
    except Exception:  # сбой или слишком долгое ожидание
        return False, None
    for name, props in events:
        log_event(name, props)
    return True, result


def ask_baseline(worker, task_data):
    if ai_roles.PROMPT_VERSIONS.get("baseline") == "v2":
        return ai_roles.baseline_v2(worker, task_data.get("text", ""), task_data.get("goal"), task_data.get("bans"))
    return ai_roles.baseline(worker, task_data.get("text", ""))


def label_answers(worker, library, texts, task_text, temp):
    """Разметка ответов чат-бота — как classify_many, но без контекста приложения: словарь, затем Р1."""
    results = [None if temp else engine.classify_by_dictionary(library, t) for t in texts]
    pending = [i for i, r in enumerate(results) if not r]
    if pending:
        with ThreadPoolExecutor(max_workers=min(CLASSIFY_WORKERS, len(pending))) as pool:
            found = list(pool.map(lambda i: ai_roles.classify(worker, library, texts[i], task_text, temp), pending))
        for i, r in zip(pending, found):
            results[i] = r
    return [r or dict(NO_CLASS) for r in results]


def task_material(worker, library, task_data, card_line, samples):
    """Линзы и `samples` независимых ответов чат-бота с разметкой. Для облака v2 нужно несколько ответов:
    один ответ с temperature 0.7 — случайная выборка (docs/llm-quality/AUDIT.md, облако)."""
    text, goal, temp = task_data.get("text", ""), task_data.get("goal", ""), task_data.get("temp_taxonomy")
    with ThreadPoolExecutor(max_workers=1 + samples) as pool:
        lens_job = pool.submit(ai_roles.lenses, worker, library, text, goal, card_line)
        answer_jobs = [pool.submit(ask_baseline, worker, task_data) for _ in range(samples)]
        lenses, answer_sets = lens_job.result(), [j.result() or [] for j in answer_jobs]
    flat = [a for answers in answer_sets for a in answers]
    labels = label_answers(worker, library, flat, text, temp) if flat else []
    labelled, pos = [], 0
    for answers in answer_sets:
        labelled.append(labels[pos:pos + len(answers)])
        pos += len(answers)
    first = next((i for i, a in enumerate(answer_sets) if a), None)
    return {"task": text, "lenses": lenses,
            "answers": answer_sets[first] if first is not None else [],
            "classes": [{"family": c["family"], "source": c["source"]} for c in labelled[first]]
            if first is not None else [],
            "samples": [[c["family"] for c in lab if c["family"]] for a, lab in zip(answer_sets, labelled) if a]}


def peek_background(key, check, timeout):
    """Как take_background, но не забирает недосчитанное: «done» и результат, «pending» или «missing»."""
    with _PREFETCH_LOCK:
        entry = _PREFETCHED.get(key)
    if entry is None or entry[0] != check:
        return "missing", None
    try:
        result, events = entry[1].result(timeout=timeout)
    except TimeoutError:
        return "pending", None
    except Exception:
        result, events = None, []
    with _PREFETCH_LOCK:
        _PREFETCHED.pop(key, None)
    for name, props in events:
        log_event(name, props)
    return "done", result


def resolve_pattern(s, wait):
    """Разбор «что общего» (Р16) из фона → флаги сессии и зеркало. (готов ли, разбор ИИ или None).
    Не готов за wait секунд — (False, None): зеркало пока показывает грубый разбор по направлениям."""
    flags = loads(s["flags_json"], {})
    if not flags.get("pattern_pending"):
        return True, flags.get("pattern")
    status, result = peek_background(("pattern", s["id"]), flags["pattern_pending"], wait)
    if status == "pending":
        return False, None
    fresh = load_session(s["id"])
    flags_update(fresh, pattern=result, pattern_pending=None)
    mirror = loads(fresh["mirror_json"], None)
    if mirror is not None and result:
        mirror["pattern"] = result
        save(s["id"], mirror_json=dumps(mirror))
    return True, result


def start_prefetch(s, task_data):
    if not (PREFETCH and ai().available):
        return
    library, line = lib(), card_text(loads(s["card_json"], {}))
    samples = engine.CLOUD_SAMPLES if CLOUD_VERSION == "v2" else 1
    run_in_background(s["id"], task_data.get("text", ""),
                      lambda w: task_material(w, library, task_data, line, samples))


def take_prefetch(s, task_data):
    """Готовый материал задачи (подождём, если ещё считается) или None — тогда прежний путь."""
    found, material = take_background(s["id"], task_data.get("text", ""))
    return material if found else None


def xray_many(task_data, texts):
    """Рентген идей пользователя (C1 §1): с ИИ — разбор по механизму, без ИИ — словарь LIB-02.

    Словарь ищет слова, поэтому для идей человека ИИ идёт первым: в живом прогоне «обложка-образец…
    портфолио» словарь отнёс к «Своему контенту» по слову «портфолио», хотя механизм другой.
    """
    if not ai().available:
        return classify_many(task_data, texts)
    return finish_xray(task_data, texts, run_ai_parallel(xray_jobs(task_data, texts)))


def xray_jobs(task_data, texts):
    """Задания рентгена для run_ai_parallel — чтобы запускать их вместе с другими вызовами модели."""
    library, task_text, temp = lib(), task_data.get("text"), task_data.get("temp_taxonomy")
    return [lambda w, t=t: ai_roles.xray(w, library, t, task_text, temp) for t in texts]


def finish_xray(task_data, texts, results):
    temp = task_data.get("temp_taxonomy")
    library = lib()
    results = list(results)
    missing = [i for i, r in enumerate(results) if not r]
    if missing:  # ИИ не ответил — словарь, чтобы не остаться без направления
        backup = [None if temp else engine.classify_by_dictionary(library, texts[i]) for i in missing]
        for i, r in zip(missing, backup):
            results[i] = r
    return [r or dict(NO_CLASS) for r in results]


def store_idea(sid, stage, ord_, text, task_data, favorite=False, cls=None, version=None, change=None):
    c = cls or xray_many(task_data, [text])[0]
    db = get_db()
    cur = db.execute(
        "INSERT INTO ideas (session_id, stage, ord, text, is_favorite, family, subfamily, class_source, confidence, "
        "explanation, xray_json, version, change_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (sid, stage, ord_, text, int(favorite), c["family"], c["subfamily"], c["source"], c["confidence"],
         c["explanation"], dumps(c["xray"]) if c.get("xray") else None, version,
         dumps(change) if change else None, now_iso()))
    db.commit()
    return cur.lastrowid, c


# ---------- С2. Выгрузка очевидного (B2, §2) ----------

@step_handler("first")
def first_step(s):
    c = ctx(s)
    errors, values = {}, {}
    if request.method == "POST":
        texts = [(request.form.get(f"idea{i}") or "").strip()[:300] for i in range(1, 5)]
        filled = [(i, t) for i, t in enumerate(texts, 1) if t]
        fav = request.form.get("favorite", type=int)
        values = {"texts": texts, "favorite": fav, "why": (request.form.get("why") or "").strip()[:200],
                  "dont_know": bool(request.form.get("dont_know"))}
        if not filled:
            errors["ideas"] = "Запишите хотя бы одну идею — можно коротко."
        elif len(filled) == 1:
            fav = filled[0][0]
        elif fav not in [i for i, _ in filled]:
            errors["favorite"] = lib().t("UI-028")
        if not errors:
            td, texts = c["task_data"], [text for _, text in filled]
            material = take_prefetch(s, td) if (ai().available and PREFETCH) else None
            # что общего у первых идей (Р16): ответ 9–19 с — дольше рентгена, поэтому в фоне; зеркало открывается
            # после рентгена, вывод подгружается по готовности (mirror_pattern), к выбору задания он уже учтён
            pattern_key = " | ".join(texts)
            if ai().available and MIRROR_PATTERN and len(texts) >= 2:
                run_in_background(("pattern", s["id"]), pattern_key,
                                  lambda w: ai_roles.pattern(w, td.get("text", ""), td.get("goal", ""), texts))
            else:
                pattern_key = None
            if material is not None:  # линзы и ответы чат-бота готовы с шага «Задача» — ждём только рентген
                results = run_ai_parallel(xray_jobs(td, texts))
                classes = finish_xray(td, texts, results[:len(texts)])
            elif ai().available:
                # рентген идей, три линзы и ответы чат-бота не зависят друг от друга — один параллельный заход,
                # зеркало потом берёт готовое (в живом прогоне последовательно выходило 20+ секунд)
                library, card_line = lib(), card_text(loads(s["card_json"], {}))
                results = run_ai_parallel(xray_jobs(td, texts) + [
                    lambda w: ai_roles.lenses(w, library, td.get("text", ""), td.get("goal", ""), card_line),
                    lambda w: ask_baseline(w, td)])
                classes = finish_xray(td, texts, results[:len(texts)])
                material = {"lenses": results[-2], "answers": results[-1] or []}
            else:
                classes = xray_many(td, texts)
            for n, ((i, text), cls) in enumerate(zip(filled, classes), 1):
                store_idea(s["id"], "first", n, text, c["task_data"], favorite=(i == fav), cls=cls)
            # одной записью: s — снимок строки до шага, вторая запись флагов затёрла бы первую. Так и было до
            # 17.10: готовые линзы и ответы чат-бота терялись, зеркало запрашивало их заново (+2 вызова, +10 с;
            # живые прогоны 12.10, docs/llm-quality/AUDIT.md)
            extra = {"prefetch": material} if material is not None else {}
            flags_update(s, why=values["why"], dont_know=values["dont_know"], pattern_pending=pattern_key, **extra)
            save(s["id"], state="mirror")
            log_event("first_ideas_submitted", {"session": s["id"], "n": len(filled)})
            return go(s["id"], "mirror")
    elif c["flags"].get("lab_first"):  # эксперимент: те же первые идеи, что человек записал до обеих сессий
        lf = c["flags"]["lab_first"]
        values = {"texts": (lf.get("texts") or []) + [""] * 4, "favorite": lf.get("favorite"), "why": lf.get("why")}
    return render_template("train/first.html", errors=errors, values=values, **c)


# ---------- С3. Зеркало (B2, §3) ----------

def data_shares():
    """Частоты семейств по завершённым сессиям сферы (≥30 — расчёт по данным)."""
    db = get_db()
    rows = db.execute("SELECT s.id, s.task_json, i.family FROM sessions s JOIN ideas i ON i.session_id = s.id "
                      "WHERE s.state = 'done' AND i.stage = 'first' AND i.family IS NOT NULL").fetchall()
    per = {}
    for r in rows:
        td = loads(r["task_json"], {})
        if td.get("in_domain") and not td.get("temp_taxonomy"):
            per.setdefault(r["id"], set()).add(r["family"][:3])
    n = len(per)
    counts = {}
    for fams in per.values():
        for f in fams:
            counts[f] = counts.get(f, 0) + 1
    return ({f: c / n for f, c in counts.items()} if n else {}), n


def user_first_families(uid, exclude_sid=None):
    db = get_db()
    rows = db.execute("SELECT s.id, s.task_json, i.family FROM sessions s JOIN ideas i ON i.session_id = s.id "
                      "WHERE s.user_id = ? AND s.state = 'done' AND i.stage = 'first' AND i.family IS NOT NULL "
                      "ORDER BY s.id", (uid,)).fetchall()
    per = {}
    for r in rows:
        if r["id"] != exclude_sid and not loads(r["task_json"], {}).get("temp_taxonomy"):
            per.setdefault(r["id"], set()).add(r["family"][:3])
    return list(per.values())


def build_baseline(s, task_data, answers=None, classes=None):
    """answers — уже полученные ответы чат-бота ([] — ИИ пробовали, не вышло; None — ещё не спрашивали);
    classes — их разметка, если она сделана при предвыборке."""
    if answers is None and ai().available:
        answers = ask_baseline(ai(), task_data)
    if ai().available:
        if answers:
            if not classes or len(classes) != len(answers):
                classes = classify_many(task_data, answers)
            return [{"text": a, "family": cl["family"]} for a, cl in zip(answers, classes)], "ai"
    if task_data.get("temp_taxonomy"):
        return [], "none"
    rng = engine.rng_for("baseline", s["id"])
    similar = similar_baseline(task_data.get("text", ""))
    if similar:  # ответы на самую похожую эталонную задачу LIB-14, сначала из частых семейств
        answers = list(lib().baselines[similar]["answers"])
        rng.shuffle(answers)
        answers.sort(key=lambda a: lib().families[a["code"][:3]]["seed"] != "часто")
        picked = answers[:3]
    else:
        pool = [a for b in lib().baselines.values() for a in b["answers"]
                if lib().families[a["code"][:3]]["seed"] == "часто"]
        picked = rng.sample(pool, 3)
    return [{"text": a["text"], "family": a["code"][:3]} for a in picked], "library"


def similar_baseline(task_text):
    """Эталонная задача LIB-14 с наибольшим числом общих основ слов с задачей пользователя (или None)."""
    words = engine.stems(task_text)
    texts = {t["id"]: t["text"] for t in lib().standard_tasks}
    scored = [(len(words & engine.stems(texts.get(bid, ""))), bid) for bid in lib().baselines]
    best = max(scored, key=lambda x: (x[0], -int(x[1][1:]))) if scored else (0, None)
    return best[1] if best[0] >= 2 else None


def compute_mirror(s, keep_baseline=None, keep_lenses=None):
    td = loads(s["task_json"], {})
    tax = Taxonomy(td)
    card = loads(s["card_json"], {})
    ideas = ideas_of(s["id"], "first")
    lenses = keep_lenses
    prefetch = loads(s["flags_json"], {}).get("prefetch")
    if keep_baseline is not None:
        baseline, b_source = keep_baseline
    elif prefetch is not None and ai().available:  # линзы и ответы чат-бота получены заранее или вместе с рентгеном
        lenses = prefetch.get("lenses")
        baseline, b_source = build_baseline(s, td, answers=prefetch.get("answers") or [],
                                            classes=prefetch.get("classes"))
    elif ai().available:  # ответы чат-бота и три линзы зависят только от задачи — спрашиваем параллельно
        # всё, что требует контекста приложения (lib, card_text), — до запуска потоков
        library, text, goal, card_line = lib(), td.get("text", ""), td.get("goal", ""), card_text(card)
        lenses, answers = run_ai_parallel([
            lambda w: ai_roles.lenses(w, library, text, goal, card_line),
            lambda w: ask_baseline(w, td)])
        baseline, b_source = build_baseline(s, td, answers=answers or [])
    else:
        baseline, b_source = build_baseline(s, td)
    base_f = [tax.fam(b["family"]) for b in baseline if b.get("family")]
    # выборки ответов чат-бота для облака: несколько — из предвыборки, иначе одна — показанная
    samples = [[tax.fam(f) for f in fams] for fams in (prefetch or {}).get("samples") or []] \
        if b_source == "ai" and (prefetch or {}).get("samples") else [base_f]
    rare = None
    if CLOUD_VERSION == "v2":
        if tax.temp:
            labels, c_source, n_basis = engine.cloud_labels_temp(tax.codes(), samples), "baseline", 0
        else:
            shares, n_basis = data_shares()
            labels, c_source = engine.cloud_labels(lib(), samples, shares, n_basis)
        common = {f for f, v in labels.items() if v == "common"}
        rare = sorted(f for f, v in labels.items() if v == "rare")
    elif tax.temp:
        common, c_source, n_basis = set(base_f), "baseline", 0
    else:
        shares, n_basis = data_shares()
        common, c_source = engine.common_families(lib(), base_f, shares, n_basis)
    used = [i["family"] for i in ideas if i["family"]]
    prev = td.get("prev_session")
    if prev:
        used += [r["family"] for r in ideas_of(prev) if r["family"]]
    history = [f for fams in user_first_families(g.user["id"]) for f in fams]
    if tax.temp:
        free = [c for c in tax.codes() if c not in used]  # сначала редкие: частое направление «незанятым» не считаем
        untouched = ([c for c in free if c not in common] + [c for c in free if c in common])[:4]
    else:
        untouched = engine.navigator(lib(), used, card, common, history, td.get("bans", ""))
    habit = None if tax.temp else engine.favorite_family(user_first_families(g.user["id"], exclude_sid=s["id"]))
    rec = [tax.fam(i["family"]) for i in ideas if i["family"]]
    # что общего у первых идей: разбор ИИ (Р16) или, без него, грубо по направлениям
    pattern = loads(s["flags_json"], {}).get("pattern") or engine.pattern_by_families(
        [tax.fam(i["family"]) for i in ideas], tax.name, tax.mechanism)
    return {
        "pattern": pattern,
        "baseline": baseline, "baseline_source": b_source,
        "common": sorted(common), "common_source": c_source, "n_basis": n_basis,
        # облако v2: «редкое» — только явно редкое; не попавшее ни в common, ни в rare — «встречается иногда»
        "rare": rare, "cloud": CLOUD_VERSION, "n_samples": len(samples) if b_source == "ai" else 0,
        "untouched": untouched, "habit": list(habit) if habit else None,
        "low_precision": bool(td.get("low_precision")),
        "n_ideas": len(ideas), "n_families": len(set(rec)),
        "n_common": sum(1 for f in rec if f in common),
        "overlap": sorted(set(rec) & set(base_f)),
        "lenses": lenses, "lens_source": "ai" if lenses else "template",
    }


def family_frequency(family, mirror):
    """«common», «rare» или «uncertain» по облаку зеркала; None — семейство не определено.
    Зеркала до облака v2 (без списка rare): всё, что не в облаке, считалось редким."""
    if not family:
        return None
    if family in set(mirror.get("common") or []):
        return "common"
    rare = mirror.get("rare")
    if rare is None:
        return "rare"
    return "rare" if family in set(rare) else "uncertain"


PATTERN_USER = {"one": "Да, это один механизм", "diverse": "Нет, механизмы разные",
                "unclear": "По описанию не определить"}


def pattern_of(s, mirror=None):
    """Разбор «что общего» вместе с поправкой человека (флаг pattern_user): его вывод сильнее вывода модели."""
    mirror = mirror if mirror is not None else (loads(s["mirror_json"], {}) or {})
    p = dict(mirror.get("pattern") or {})
    user = loads(s["flags_json"], {}).get("pattern_user")
    if user in PATTERN_USER:
        p["user_verdict"] = user
    return p


def mechanism_rut(mirror):
    """Колея, под которую даём задание «смена механизма». Решает поправка человека, если она есть. Без неё — только
    разбор ИИ: все первые идеи держатся на одном способе и уверенность «high» после проверок кода (ai_roles.
    pattern_v2 понижает её при неясных идеях и группе из двух). «medium» — возможная колея: зеркало просит
    подтвердить, а без ответа обязательного задания нет. По одним направлениям (без ИИ) колею так не называем."""
    p = (mirror or {}).get("pattern") or {}
    if not MIRROR_PATTERN:
        return False
    if p.get("user_verdict"):
        return p["user_verdict"] == "one"
    return bool(p.get("source") == "ai" and p.get("verdict") == "one" and p.get("confidence") == "high"
                and p.get("groups"))


def rut_lever(p):
    """Способ воздействия, по которому идёт колея: самая большая группа, иначе (человек сам подтвердил колею, а
    группы нет) — способ идеи-фаворита по разбору. None — способ неизвестен (разбор v1 или без ИИ)."""
    if p.get("groups") and p["groups"][0].get("lever"):
        return p["groups"][0]["lever"]
    clear = [i for i in p.get("ideas") or [] if isinstance(i, dict) and i.get("lever")]
    return clear[0]["lever"] if clear else None


def needs_confirmation(p):
    """Колея возможна, но не установлена (все идеи в одной группе, уверенность ниже «high»): зеркало прямо
    спрашивает человека. В остальных случаях поправка доступна, но свёрнута — лишних вопросов не задаём."""
    return bool(p.get("source") == "ai" and not p.get("user_verdict") and p.get("verdict") == "one"
                and p.get("confidence") != "high")


def done_sessions(uid):
    db = get_db()
    return db.execute("SELECT * FROM sessions WHERE user_id = ? AND state = 'done' AND mode IN ('task','practice') "
                      "ORDER BY id", (uid,)).fetchall()


def diagnose_session(s, mirror):
    td = loads(s["task_json"], {})
    tax = Taxonomy(td)
    card = loads(s["card_json"], {})
    common = set(mirror["common"])
    ideas = ideas_of(s["id"], "first")
    items = []
    for i in ideas:
        f = tax.fam(i["family"])
        items.append({"family": f, "common": bool(f and f in common), "is_favorite": bool(i["is_favorite"])})
    fav = next((i for i in ideas if i["is_favorite"]), None)
    conflicts = []
    if fav and fav["family"] and not tax.temp:
        conflicts = engine.resource_conflicts(lib(), fav["subfamily"] or fav["family"], card)
    done = done_sessions(g.user["id"])
    flags = loads(s["flags_json"], {})
    code, feats = engine.diagnose(items, dont_know=flags.get("dont_know"), fav_conflict=bool(conflicts),
                                  level=s["level"], completed=len(done))
    history = [(loads(d["tool_json"], {}).get("type"), loads(d["eval_json"], {}).get("shift")) for d in done]
    p = pattern_of(s, mirror)
    rut = mechanism_rut(dict(mirror, pattern=p))
    rejected = p.get("user_verdict") in ("diverse", "unclear") or (
        not p.get("user_verdict") and p.get("source") == "ai" and p.get("verdict") != "one")
    if code == "Д1" and not rut and rejected:
        # Д1 («все идеи — одно направление») считается по словарю направлений. Если разбор механизмов или сам
        # человек колею не подтвердили, задание не должно говорить «все ваши идеи работают через одно и то же»
        code = "Д2" if feats.get("n_common") == feats.get("n_ideas") else "Д—"
    if len(done) >= 5:  # персонализация с 5 сессий (A2, §2.7)
        tool, switched = engine.choose_tool_type(code, history)
    else:
        tool, switched = engine.DIAG_TOOL[code], False
    if rut:  # все первые идеи — один механизм (Р16 или поправка человека): задание бьёт ровно в него
        tool, switched = "switch", False
    return {"code": code, "feats": feats, "tool_type": tool, "switched": switched,
            "conflicts": [list(x) for x in conflicts],
            "pattern": {"verdict": p.get("verdict"), "confidence": p.get("confidence"),
                        "user": p.get("user_verdict"), "source": p.get("source")}}


PATTERN_WAIT_PAGE = 1.5  # секунд подождать разбор «что общего» при открытии зеркала — дальше он подгрузится сам
PATTERN_WAIT_CHOICE = 25  # перед выбором задания разбор нужен: колея решает, какое задание дать


@step_handler("mirror")
def mirror_step(s):
    action = request.form.get("action") if request.method == "POST" else None
    ready, _ = resolve_pattern(s, PATTERN_WAIT_CHOICE if action == "continue" else
                               (PATTERN_WAIT_PAGE if request.method == "GET" else 0))
    s = load_session(s["id"])
    c = ctx(s)
    tax = Taxonomy(c["task_data"])
    mirror = loads(s["mirror_json"], None)
    if mirror is None:
        mirror = compute_mirror(s)
        save(s["id"], mirror_json=dumps(mirror))
        log_event("mirror_shown", {"session": s["id"], "n_common": mirror["n_common"],
                                   "source": mirror["common_source"], "baseline": mirror["baseline_source"],
                                   "lens_source": mirror.get("lens_source"),
                                   "lens_prompt": ai_roles.PROMPT_VERSIONS["lenses"], "cloud": mirror.get("cloud"),
                                   "n_samples": mirror.get("n_samples"),
                                   "baseline_prompt": ai_roles.PROMPT_VERSIONS.get("baseline")})
    if request.method == "POST":
        action = request.form.get("action")
        if action == "dispute":
            idea_id = request.form.get("idea_id", type=int)
            code = request.form.get("family")
            valid = set(tax.codes()) | (set() if tax.temp else set(lib().subfamilies))
            db = get_db()
            idea = db.execute("SELECT * FROM ideas WHERE id = ? AND session_id = ?", (idea_id, s["id"])).fetchone()
            if idea and (code in valid or code == "none"):
                fam = None if code == "none" else tax.fam(code)
                sub = code if (code != "none" and not tax.temp and "." in code) else None
                db.execute("UPDATE ideas SET family = ?, subfamily = ?, class_source = 'user', disputed = 1 "
                           "WHERE id = ?", (fam, sub, idea_id))
                db.commit()
                log_event("classification_disputed", {"session": s["id"], "from": idea["family"], "to": fam})
                s = load_session(s["id"])
                mirror = compute_mirror(s, keep_baseline=(mirror["baseline"], mirror["baseline_source"]),
                                        keep_lenses=mirror.get("lenses"))
                save(s["id"], mirror_json=dumps(mirror))
            return go(s["id"], "mirror")
        if action == "fix_mechanism":  # «Мы поняли идею не так?» — человек поправляет разбор (C1 §1)
            idea_id = request.form.get("idea_id", type=int)
            text = (request.form.get("driver") or "").strip()[:200]
            db = get_db()
            idea = db.execute("SELECT * FROM ideas WHERE id = ? AND session_id = ?", (idea_id, s["id"])).fetchone()
            if idea and text:
                x = loads(idea["xray_json"], {}) or {}
                x["driver_user"] = text
                db.execute("UPDATE ideas SET xray_json = ?, disputed = 1 WHERE id = ?", (dumps(x), idea_id))
                db.commit()
                log_event("xray_corrected", {"session": s["id"], "idea": idea_id})
            return go(s["id"], "mirror")
        if action == "pattern_verdict":  # человек поправляет вывод «что общего»: он решает, нужна ли смена механизма
            value = request.form.get("verdict")
            if value not in PATTERN_USER and value != "reset":
                abort(400)
            p = pattern_of(s, mirror)
            flags_update(s, pattern_user=None if value == "reset" else value)
            log_event("pattern_corrected", {"session": s["id"], "to": value, "where": "mirror",
                                            "verdict": p.get("verdict"), "confidence": p.get("confidence"),
                                            "source": p.get("source")})
            return go(s["id"], "mirror")
        if action == "continue":
            lens = request.form.get("lens")
            own = (request.form.get("own_barrier") or "").strip()[:200]
            lenses = mirror.get("lenses") or {}
            barrier = None
            if own:
                barrier = {"lens": lens if lens in ai_roles.LENSES else "own", "text": own, "source": "user"}
            elif lens in lenses:
                barrier = {"lens": lens, "text": lenses[lens]["barrier"], "question": lenses[lens]["question"],
                           "source": "ai"}
            elif lens in ai_roles.LENSES:  # без ИИ: человек выбрал вопрос-линзу, но препятствие не сформулировал
                barrier = {"lens": lens, "text": None, "question": lib().t(engine.LENS_FALLBACK[lens]),
                           "source": "template"}
            if barrier:
                flags_update(s, barrier=barrier)
                s = load_session(s["id"])
            diag = diagnose_session(s, mirror)
            save(s["id"], diagnosis_json=dumps(diag), state="tool")
            start_fav_rubric(s)  # к заданию: сильна ли уже первая идея — тогда смену механизма не навязываем
            log_event("diagnosis_made", {"session": s["id"], "code": diag["code"], "tool": diag["tool_type"],
                                         "lens": barrier["lens"] if barrier else None,
                                         "pattern": diag["pattern"]["user"] or diag["pattern"]["verdict"],
                                         "pattern_confidence": diag["pattern"]["confidence"]})
            return go(s["id"], "tool")
        abort(400)
    ideas = ideas_of(s["id"], "first")
    common = set(mirror["common"])
    rows = []
    for i in ideas:
        f = tax.fam(i["family"])
        x = loads(i["xray_json"], None)
        rows.append({"id": i["id"], "text": i["text"], "family": f, "name": tax.name(i["subfamily"] or i["family"]),
                     "common": bool(f and f in common), "freq": family_frequency(f, mirror),
                     "favorite": bool(i["is_favorite"]),
                     "explanation": None if x else i["explanation"], "disputed": bool(i["disputed"]),
                     "xray": lib().t("UI-167", механизм=x.get("driver_user") or x["driver"], барьер=x["barrier"])
                     if x else None,
                     "xray_full": [(ai_roles.XRAY_NAMES[k], (x.get("driver_user") if k == "driver" else None) or x[k])
                                   for k in ai_roles.XRAY_FIELDS if x.get(k)] if x else []})
    options = [(code, tax.name(code)) for code in tax.codes()]
    lens_rows = [{"key": k, "name": ai_roles.LENS_NAMES[k],
                  "barrier": (mirror.get("lenses") or {}).get(k, {}).get("barrier"),
                  "question": (mirror.get("lenses") or {}).get(k, {}).get("question")
                  or lib().t(engine.LENS_FALLBACK[k])} for k in ai_roles.LENSES]
    p = pattern_of(s, mirror)
    return render_template("train/mirror.html", m=mirror, rows=rows, options=options, tax=tax, lens_rows=lens_rows,
                           pattern_pending=not ready, p=p, ask=needs_confirmation(p), user_options=PATTERN_USER,
                           **c)


@bp.get("/s/<int:sid>/mirror/pattern")
@login_required
def mirror_pattern(sid):
    """Разбор «что общего у ваших идей», когда он готов: страница зеркала спрашивает сама. 204 — ещё считается."""
    s = load_session(sid)
    ready, _ = resolve_pattern(s, 15)
    if not ready:
        return "", 204
    s = load_session(sid)
    p = pattern_of(s)
    return render_template("train/_pattern.html", p=p, pending=False, sid=sid, ask=needs_confirmation(p),
                           user_options=PATTERN_USER, editable=s["state"] == "mirror")


# ---------- С4–С5. Диагноз и инструмент (B2 §4, B3) ----------

SELF_DIAG = {"Одна колея": "Д1", "Всё привычное": "Д2", "Держусь за любимую идею": "Д3",
             "Упираюсь в ресурсы": "Д4", "Не знаю": None}
WHEN_UI = {"assumptions": "UI-140", "chain": "UI-141", "operator": "UI-142", "contradiction": "UI-143",
           "constraint": "UI-144", "crossing": "UI-072", "reframe": "UI-175"}
SLOT_NOTE = "Слова в [квадратных скобках] замените на то, что относится к вашей задаче."


def favorite_idea(sid):
    return next((i for i in ideas_of(sid, "first") if i["is_favorite"]), None)


def session_barrier(s):
    """Препятствие, которое человек выбрал на зеркале через линзы (C1 §3), или {}."""
    return loads(s["flags_json"], {}).get("barrier") or {}


def adapt(td, template, kind, barrier=None):
    goal = td.get("goal") or td.get("text", "")
    if barrier:  # задание адаптируется вокруг выбранного препятствия (текст может быть от человека — как данные)
        kind += f"; решение ищем вокруг препятствия: <<<{barrier}>>>"
    if ai().available:
        text = ai_roles.adapt_tool(ai(), td.get("text", ""), goal, template, kind)
        if text:
            return text, "ai"
    text = engine.fill_slots(template, {"цель": goal})
    return text, "template"


def _operator_into(tool, s, target, exclude=()):
    td, tax = loads(s["task_json"], {}), Taxonomy(loads(s["task_json"], {}))
    rng = engine.rng_for(s["id"], "op", tool["strength"], len(exclude))
    excl = recently_shown(g.user["id"], "LIB-06") | set(exclude)
    op = engine.pick_operator(lib(), None if tax.temp else target, tool["strength"], excl, rng)
    text, src = adapt(td, op["template"], f"оператор «{op['name']}»", session_barrier(s).get("text"))
    mech = tax.mechanism(target).rstrip(". ") if target else ""
    tool.update({"phase": "ready", "target": target, "item_id": op["id"], "source": src,
                 "header": (f"Направление, которого вы не касались: «{tax.name(target)}»"
                            + (f" — {mech[:1].lower() + mech[1:]}." if mech else ".") if target else ""),
                 "task_text": f"«{op['name']}»: {text[:1].lower() + text[1:]}", "lib_ids": [op["id"]]})
    mark_shown(g.user["id"], "LIB-06", op["id"], s["id"])


def _constraint_into(tool, s, mirror, exclude=()):
    td = loads(s["task_json"], {})
    tax = Taxonomy(td)
    first = ideas_of(s["id"], "first")
    habit = mirror.get("habit")
    personal, personal_sub = None, None
    if habit and not tax.temp and any(tax.fam(i["family"]) == habit[0] for i in first):
        personal = habit[0]
        subs = [i["subfamily"] for i in first if i["subfamily"] and i["subfamily"][:3] == personal]
        personal_sub = max(set(subs), key=subs.count) if subs else None
    rng = engine.rng_for(s["id"], "c", tool["strength"], len(exclude))
    excl = recently_shown(g.user["id"], "LIB-05") | set(exclude)
    item = engine.pick_constraint(lib(), tool["strength"], rng, excl, personal, personal_sub)
    kind = item["type"]
    body = re.sub(r"\s*\((?:F\d\d(?:\.\d)?(?:,\s*)?)+\)", "", item["text"]).strip().rstrip(".")
    body = body[0].lower() + body[1:]
    if kind == "antipode":
        text = f"Решите задачу для {body}. {lib().t('UI-073')}"
    elif kind == "transfer":
        text = f"Как эту задачу решала бы сфера «{item['text']}»? Найдите принцип и перенесите его, не копируя внешний вид."
    elif kind in ("family_ban", "sub_ban"):
        text = f"Ограничение: {body}. Цель та же: {td.get('goal') or td.get('text', '')}."
    else:
        text = f"Условие: {body}. Цель та же: {td.get('goal') or td.get('text', '')}."
    why_ui = engine.CONSTRAINT_WHY.get(kind, "UI-065")
    why = lib().t(why_ui, семейство=tax.name(personal) if personal else "")
    if item.get("personal") and habit:
        why = lib().t("UI-066", семейство=tax.name(personal))
    tool.update({"phase": "ready", "item_id": item["id"], "constraint_type": kind, "personal": bool(item.get("personal")),
                 "ban": body if kind in ("family_ban", "sub_ban", "channel") else None, "note": item.get("note") or None,
                 "task_text": text, "why": why, "lib_ids": [item["id"]], "source": "library"})
    mark_shown(g.user["id"], "LIB-05", item["id"], s["id"])


def _chain_into(tool, s, why):
    td = loads(s["task_json"], {})
    goal = td.get("goal") or td.get("text", "")
    fav = favorite_idea(s["id"])
    extracted = ai_roles.extract_assumption(ai(), fav["text"], why, goal) if (ai().available and fav) else None
    if extracted:
        tool.update({"assumption": extracted["assumption"], "task_text": extracted["task"], "source": "ai"})
    else:
        ch = engine.pick_chain(lib(), fav["subfamily"] if fav else None, fav["family"] if fav else None,
                               recently_shown(g.user["id"], "LIB-04"))
        tool.update({"assumption": ch["assumption"], "task_text": engine.fill_slots(ch["q1"], {"цель": goal}),
                     "source": "library", "item_id": ch["id"], "lib_ids": [ch["id"]],
                     "q2": engine.fill_slots(ch["q2"], {"цель": goal}), "q3": engine.fill_slots(ch["q3"], {"цель": goal})})
        mark_shown(g.user["id"], "LIB-04", ch["id"], s["id"])
    tool["header"] = f"Ваша идея: «{fav['text'] if fav else ''}». Она работает, если верно: «{tool['assumption']}». Цель: {goal}."
    tool["phase"] = "ready"


def build_tool(s, ttype, strength=None):
    uid = g.user["id"]
    td = loads(s["task_json"], {})
    tax = Taxonomy(td)
    diag = loads(s["diagnosis_json"], {})
    mirror = loads(s["mirror_json"], {})
    strengths = loads(get_state(uid)["strengths_json"], {})
    st = strength or engine.start_strength(strengths, ttype, diag.get("code"))
    goal = td.get("goal") or td.get("text", "")
    tool = {"type": ttype, "name": engine.TOOL_NAMES[ttype], "strength": st, "phase": "ready",
            "why": lib().t(engine.WHY_UI[ttype]), "lib_ids": []}
    first = ideas_of(s["id"], "first")
    if ttype == "assumptions":
        fams = [i["family"] for i in first if i["family"] and not tax.temp]
        rng = engine.rng_for(s["id"], "a")
        items = engine.pick_assumptions(lib(), fams, bool(td.get("in_domain")) and not tax.temp,
                                        recently_shown(uid, "LIB-03"), rng, k=4)
        tool.update({"phase": "select", "items": [{k: a[k] for k in ("id", "text", "challenge", "direction")}
                                                  for a in items], "lib_ids": [a["id"] for a in items]})
        for a in items:
            mark_shown(uid, "LIB-03", a["id"], s["id"])
    elif ttype == "chain":
        why = loads(s["flags_json"], {}).get("why")
        if why:
            _chain_into(tool, s, why)
        else:
            tool["phase"] = "why"
    elif ttype == "operator":
        untouched = mirror.get("untouched") or []
        if s["level"] >= 2 and untouched:
            tool.update({"phase": "target", "targets": untouched})
        else:
            _operator_into(tool, s, untouched[0] if untouched else None)
    elif ttype == "constraint":
        _constraint_into(tool, s, mirror)
    elif ttype == "contradiction":
        fav = favorite_idea(s["id"])
        conflicts = [tuple(x) for x in diag.get("conflicts", [])]
        excl = recently_shown(uid, "LIB-07")
        first_k, second_k = engine.pick_contradiction(lib(), tax.fam(fav["family"]) if fav else None, conflicts, st, excl)
        tool.update({"task_text": engine.contradiction_text(lib(), first_k, second_k if st >= 3 else None, weakened=st <= 1),
                     "item_id": first_k["id"], "lib_ids": [k["id"] for k in (first_k, second_k) if k],
                     "source": "library"})
        for k in tool["lib_ids"]:
            mark_shown(uid, "LIB-07", k, s["id"])
    elif ttype == "crossing":
        common = set(mirror.get("common", []))
        rare, seen = [], set()
        for i in first:
            f = tax.fam(i["family"])
            if f and f not in common and f not in seen and not tax.temp:
                rare.append({"family": f, "text": i["text"]})
                seen.add(f)
        principles = False
        if len(rare) < 2 and not tax.temp:
            # C1 §6: у человека нет двух редких идей — скрещиваем два незанятых принципа из навигатора
            for f in mirror.get("untouched", []):
                if f not in seen and f in lib().families and len(rare) < 2:
                    rare.append({"family": f, "text": lib().families[f]["name"]})
                    seen.add(f)
                    principles = True
        if len(rare) < 2:
            return build_tool(s, "operator", strength)
        tpl, text = engine.pick_crossing(lib(), rare[0], rare[1], recently_shown(uid, "LIB-06X"))
        if principles:
            header = " ".join(f"Принцип {n}: «{lib().families[r['family']]['name']}» — "
                              f"{lib().families[r['family']]['mechanism'].rstrip('. ').lower()}."
                              for n, r in zip("AB", rare[:2]))
        else:
            header = f"Идея A: «{rare[0]['text']}». Идея B: «{rare[1]['text']}»."
        tool.update({"task_text": text, "item_id": tpl["id"], "lib_ids": [tpl["id"]], "source": "library",
                     "header": header, "principles": principles,
                     "after_question": "Что вы взяли из A, что из B и что появилось нового?"})
        mark_shown(uid, "LIB-06X", tpl["id"], s["id"])
    elif ttype == "switch":  # 18.10: смена механизма — задание от колеи, найденной в зеркале (Р16)
        if not _switch_into(tool, s, mirror, goal):
            return build_tool(s, "operator", strength)
    elif ttype == "reframe":  # C1 §4: цель за целью
        fav = favorite_idea(s["id"])
        r = (ai_roles.reframe(ai(), lib(), td.get("text", ""), goal, fav["text"] if fav else None)
             if ai().available else None)
        if r:
            behind = r["goal_behind"]
            tool.update({"phase": "reframe_confirm", "goal_behind": behind[:1].lower() + behind[1:], "source": "ai"})
        else:
            tool.update({"phase": "reframe_why", "source": "template"})
    if tool.get("task_text") and engine.has_open_slots(tool["task_text"]):
        tool["slot_note"] = SLOT_NOTE
    tool["goal"] = goal
    barrier = session_barrier(s)
    if barrier.get("text"):
        tool["barrier"] = barrier["text"]
        tool["barrier_line"] = lib().t("UI-174", барьер=barrier["text"].rstrip(". "))
    elif barrier.get("question"):
        tool["barrier_line"] = barrier["question"]
    return tool


def resources_line(card, bans=""):
    """Ограничения одной строкой: бюджет, время, запреты и то, чего человек не хочет делать."""
    parts = [f"бюджет — {card['R01']}" if card.get("R01") else None,
             f"время — {card['R02']} в неделю" if card.get("R02") else None,
             f"нельзя: {bans}" if bans else None,
             ("не хотите: " + ", ".join(card["R06"])) if card.get("R06") else None]
    return "; ".join(p for p in parts if p)


# Направления поиска для «смены механизма»: другие способы воздействия, от ближних к цели «первый шаг клиента»
SWITCH_DIRECTIONS = ("trial", "risk", "proof", "referral", "contact", "ease", "experience", "belonging", "habit",
                     "reach", "price")


def _switch_into(tool, s, mirror, goal):
    """Одно точное задание вместо случайного оператора (19.10). Называет способ воздействия, на котором держатся
    первые идеи, и показывает его на них самих («клиент сделает…, потому что…»); говорит, чем этот путь ограничен,
    если это можно обосновать; запрещает все средства этого способа — заменить скидку на бонус или купон нельзя;
    сохраняет цель, ресурсы и запреты; даёт 2–3 других способа как вопросы-направления, а не готовые ответы.
    Без способа (разбор v1) — прежнее задание по общей фразе группы. Без колеи — False (прежний инструмент)."""
    p = pattern_of(s, mirror)
    td = loads(s["task_json"], {})
    constraints = resources_line(loads(s["card_json"], {}), td.get("bans", ""))
    lever = rut_lever(p)
    if lever in ai_roles.LEVERS:
        lv = ai_roles.LEVERS[lever]
        group = next((g for g in p.get("groups") or [] if g.get("lever") == lever), None)
        evidence = (group["evidence"] if group else
                    [i for i in p.get("ideas") or [] if isinstance(i, dict) and i.get("lever") == lever])
        used = {i.get("lever") for i in p.get("ideas") or [] if isinstance(i, dict) and i.get("lever")}
        directions = [{"code": c, "name": ai_roles.LEVERS[c]["name"], "mechanism": ai_roles.LEVERS[c]["question"]}
                      for c in SWITCH_DIRECTIONS if c not in used and c != lever][:3]
        tool.update({"phase": "ready", "source": "pattern", "rut_lever": lever, "shared": lv["name"],
                     "header": f"Ваши первые идеи держатся на одном способе — {lv['name']}: {lv['why']}.",
                     "evidence": [{"text": e["text"], "because": e.get("because") or ""} for e in evidence[:4]],
                     "limit": p.get("limit") or "",
                     "ban": (f"Условие: без средств этого способа — {lv['tools']} — ни в каком размере, форме и "
                             "канале. Заменить одно из них другим — это не смена механизма."),
                     "task_text": (f"Цель та же — {goal}. Придумайте вариант, в котором клиент делает первый шаг по "
                                   f"другой причине — не через {lv['name']}."),
                     "switch_principles": directions, "constraints": constraints,
                     "lib_ids": [f"switch:{d['code']}" for d in directions]})
        return True
    if not p.get("groups"):
        return False
    td, tax = loads(s["task_json"], {}), Taxonomy(loads(s["task_json"], {}))
    shared = p["groups"][0]["shared"].rstrip(". ")
    # F14 «Новые каналы» не предлагаем: задание как раз просит не менять канал при той же причине
    principles = [{"code": c, "name": tax.name(c), "mechanism": tax.mechanism(c).rstrip(". ")}
                  for c in (mirror.get("untouched") or []) if c != "F14"][:3]
    tool.update({"phase": "ready", "source": "pattern", "shared": shared, "switch_principles": principles,
                 "header": f"Все ваши первые идеи держатся на одном: {shared[:1].lower() + shared[1:]}.",
                 "task_text": (f"Цель та же — {goal}. Придумайте вариант, в котором клиент делает первый шаг по "
                               "другой причине: не другой канал или размер того же, а другой механизм."),
                 "constraints": resources_line(loads(s["card_json"], {}), td.get("bans", "")),
                 "lib_ids": [f"switch:{c['code']}" for c in principles]})
    return True


def soften_tool(s, tool):
    """«Слишком сложно»: облегчить один раз за сессию (B3, §6)."""
    t = tool["type"]
    tool["strength"] = max(1, tool["strength"] - 1)
    if t == "operator":
        _operator_into(tool, s, tool.get("target"), exclude=tool.get("lib_ids", []))
    elif t == "constraint":
        _constraint_into(tool, s, loads(s["mirror_json"], {}), exclude=tool.get("lib_ids", []))
    elif t == "contradiction":
        k = next((x for x in lib().contradictions if x["id"] == tool.get("item_id")), None)
        if k:
            tool["task_text"] = engine.contradiction_text(lib(), k, None, weakened=True)
    elif t == "assumptions":
        chosen = tool.get("chosen") or {}
        if chosen.get("direction"):
            tool["extra"] = f"Направление для размышления: {chosen['direction'].lower()}."
    elif t == "chain":
        if tool.get("q2"):
            tool["extra"] = tool["q2"].replace("[цель]", tool.get("goal", ""))
    elif t == "switch" and tool.get("switch_principles"):
        first = tool["switch_principles"][0]
        mech = first["mechanism"][:1].lower() + first["mechanism"][1:]
        tool["extra"] = f"Начните с одного направления — «{first['name']}»: {mech}{'' if mech.endswith('?') else '.'}"
    tool["softened_text"] = lib().t("UI-079", **{"новая формулировка": tool.get("task_text", "")})
    if tool.get("task_text") and engine.has_open_slots(tool["task_text"]):
        tool["slot_note"] = SLOT_NOTE
    return tool


def start_fav_rubric(s):
    """Рубрика первой идеи-фаворита — в фоне с момента диагноза (раньше — с перехода к идее): она нужна на шаге
    выбора и на шаге задания — сильна ли уже первая идея. Тот же вызов, только раньше; повторно не запускается."""
    if not (is_short(s) and ai().available and PREFETCH):
        return
    fav = favorite_idea(s["id"])
    if not fav or (loads(fav["change_json"], {}) or {}).get("rubric"):
        return
    key = ("rubric", fav["id"])
    with _PREFETCH_LOCK:
        if key in _PREFETCHED:
            return
    library, td, card = lib(), loads(s["task_json"], {}), loads(s["card_json"], {})
    line = card_text(card)
    run_in_background(key, fav["text"], lambda w: ask_rubric(w, library, fav["text"], td, line, card))


def background_result(key, check, timeout=0):
    """Готовый фоновый результат, не забирая его: заберёт шаг, которому он нужен. None — нет или ещё считается."""
    with _PREFETCH_LOCK:
        entry = _PREFETCHED.get(key)
    if entry is None or entry[0] != check:
        return None
    try:
        return entry[1].result(timeout=timeout)[0]
    except Exception:  # ещё считается, сбой или тайм-аут
        return None


def first_idea_strong(s, timeout=0):
    """Первая идея-фаворит уже выполнима при ваших ресурсах и прямо ведёт к цели (рубрика): тогда смену механизма
    предлагаем, но не навязываем. Нет оценки — не утверждаем ни того, ни другого."""
    fav = favorite_idea(s["id"])
    if not fav:
        return False
    rub = (loads(fav["change_json"], {}) or {}).get("rubric") or background_result(("rubric", fav["id"]), fav["text"],
                                                                                   timeout)
    return bool(rub) and engine.feasibility_level(rub) == "высокая" and (rub.get("RB1") or {}).get("level") == 3


@step_handler("tool")
def tool_step(s):
    c = ctx(s)
    diag = loads(s["diagnosis_json"], {})
    tool = loads(s["tool_json"], None)
    flags = c["flags"]
    td = c["task_data"]
    goal = td.get("goal") or td.get("text", "")
    error = None
    if request.method == "POST":
        action = request.form.get("action")
        if action in ("self_diag", "choose") and s["level"] >= 2 and tool is None:
            label = request.form.get("self_diag")
            if label in SELF_DIAG and "self_diag_label" not in diag:
                diag.update({"self_diag_label": label, "self_diag": SELF_DIAG[label],
                             "match": SELF_DIAG[label] == diag["code"]})
            if action == "self_diag":  # уровень 2: сначала самодиагноз, потом наш диагноз
                if "self_diag_label" not in diag:
                    error = "Выберите вариант — или «Не знаю»."
                else:
                    save(s["id"], diagnosis_json=dumps(diag))
                    log_event("self_diagnosis", {"session": s["id"], "self": diag["self_diag"], "match": diag["match"]})
                    return go(s["id"], "tool")
            else:
                chosen = request.form.get("tool")
                if chosen not in tool_options(s["level"], diag):
                    error = "Выберите инструмент."
                elif "self_diag_label" not in diag:
                    error = "Сначала отметьте, в чём, по-вашему, застряли — или «Не знаю»."
                else:
                    diag.update({"chosen_by": "user", "chosen_tool": chosen,
                                 "overridden_by_user": chosen != diag["tool_type"]})
                    tool = build_tool(s, chosen)
                    save(s["id"], diagnosis_json=dumps(diag), tool_json=dumps(tool))
                    log_event("tool_chosen_by_user", {"session": s["id"], "tool": chosen, "recommended": diag["tool_type"],
                                                      "self_diag": diag["self_diag"], "match": diag["match"]})
                    return go(s["id"], "tool")
        elif tool and action == "why" and tool["phase"] == "why":
            why = (request.form.get("why") or "").strip()[:200]
            if not why:
                error = "Ответьте одной фразой."
            else:
                flags_update(s, why=why)
                s = load_session(s["id"])
                _chain_into(tool, s, why)
                save(s["id"], tool_json=dumps(tool))
                return go(s["id"], "tool")
        elif tool and action == "select" and tool["phase"] == "select":
            about = [i for i in request.form.getlist("about_me") if i in {x["id"] for x in tool["items"]}]
            chosen_id = request.form.get("chosen")
            own = (request.form.get("own") or "").strip()[:200]
            chosen = next((x for x in tool["items"] if x["id"] == chosen_id), None)
            if own and not chosen:
                chosen = {"id": "own", "text": own, "challenge": "", "direction": ""}
            if not chosen:
                error = "Отметьте допущение, с которым будем работать, или напишите своё."
            else:
                text = chosen["text"]
                task_text = f"Представьте, что «{text}» — неправда. Как тогда добиться цели: {goal}?"
                if chosen.get("challenge"):
                    task_text += f" {chosen['challenge']}"
                tool.update({"phase": "ready", "chosen": chosen, "about_me": about, "own": own or None,
                             "task_text": task_text, "assumption": text})
                save(s["id"], tool_json=dumps(tool))
                log_event("assumptions_marked", {"session": s["id"], "shown": tool["lib_ids"], "about_me": about,
                                                 "chosen": chosen["id"]})
                return go(s["id"], "tool")
        elif tool and action == "own_assumption" and tool["type"] == "chain" and tool["phase"] == "ready":
            own = (request.form.get("own") or "").strip()[:200]
            if own:
                tool.update({"assumption": own, "source": "user",
                             "task_text": f"Сохраните цель — {goal}. Как добиться её, если «{own}» неверно?"})
                fav = favorite_idea(s["id"])
                tool["header"] = f"Ваша идея: «{fav['text'] if fav else ''}». Она работает, если верно: «{own}». Цель: {goal}."
                save(s["id"], tool_json=dumps(tool))
                log_event("chain_assumption_rejected", {"session": s["id"]})
            return go(s["id"], "tool")
        elif tool and action == "target" and tool["phase"] == "target":
            target = request.form.get("target")
            if target not in tool["targets"]:
                error = "Выберите направление."
            else:
                _operator_into(tool, s, target)
                save(s["id"], tool_json=dumps(tool))
                return go(s["id"], "tool")
        elif tool and action == "reframe_yes" and tool["phase"] == "reframe_confirm":
            tool.update({"phase": "ready", "reframe_confirmed": True,
                         "task_text": lib().t("UI-178", **{"цель за целью": tool["goal_behind"], "цель": goal})})
            save(s["id"], tool_json=dumps(tool))
            log_event("reframe_answered", {"session": s["id"], "confirmed": True})
            return go(s["id"], "tool")
        elif tool and action == "reframe_own" and tool["phase"] in ("reframe_confirm", "reframe_why"):
            behind = (request.form.get("goal_behind") or "").strip().rstrip(".")[:200]
            if not behind:
                error = "Напишите одной фразой, ради чего вам нужна эта цель."
            else:
                tool.update({"phase": "ready", "goal_behind": behind, "source": "user",
                             "task_text": lib().t("UI-178", **{"цель за целью": behind, "цель": goal})})
                save(s["id"], tool_json=dumps(tool))
                log_event("reframe_answered", {"session": s["id"], "confirmed": False})
                return go(s["id"], "tool")
        elif tool and action == "change" and not flags.get("tool_changed"):
            new_type = engine.ALTERNATIVES[tool["type"]][0]
            tool = build_tool(s, new_type)
            flags_update(s, tool_changed=True)
            diag["overridden_by_user"] = True
            save(s["id"], tool_json=dumps(tool), diagnosis_json=dumps(diag))
            log_event("tool_changed", {"session": s["id"], "to": new_type})
            return go(s["id"], "tool")
        elif tool and action == "soften" and not flags.get("softened") and tool["phase"] == "ready":
            tool = soften_tool(s, tool)
            flags_update(s, softened=True)
            save(s["id"], tool_json=dumps(tool))
            log_event("tool_softened", {"session": s["id"], "strength": tool["strength"]})
            return go(s["id"], "tool")
        elif tool and action == "not_rut" and tool["type"] == "switch":
            # «Мои идеи на самом деле разные»: вывод о колее отвергнут — задание по нему больше не предлагаем
            flags_update(s, pattern_user="diverse")
            s = load_session(s["id"])
            fresh = diagnose_session(s, loads(s["mirror_json"], {}) or {})
            diag.update({k: fresh[k] for k in ("code", "tool_type", "switched", "pattern")})
            tool = build_tool(s, diag["tool_type"])
            save(s["id"], tool_json=dumps(tool), diagnosis_json=dumps(diag))
            log_event("pattern_corrected", {"session": s["id"], "to": "diverse", "where": "tool", "tool": tool["type"]})
            return go(s["id"], "tool")
        elif tool and action == "skip_switch" and tool["type"] == "switch" and is_short(s):
            # первая идея уже подходит: упражнение не навязываем — сразу к выбору решения
            save(s["id"], state="choose")
            log_event("switch_skipped", {"session": s["id"], "first_strong": first_idea_strong(s)})
            return go(s["id"], "choose")
        elif tool and action == "go" and tool["phase"] == "ready":
            save(s["id"], state="idea")
            start_fav_rubric(s)  # к шагу «Выбор»: ресурсы первой идеи-фаворита (если ещё не считается)
            log_event("tool_shown", {"session": s["id"], "type": tool["type"], "strength": tool["strength"],
                                     "items": tool.get("lib_ids")})
            return go(s["id"], "idea")
        elif not error:
            abort(400)
    if tool is None and s["level"] == 1:
        tool = build_tool(s, diag["tool_type"])
        save(s["id"], tool_json=dumps(tool))
    if tool is None:  # уровни 2–3: человек выбирает сам (B2 §4.5 п.4)
        need_self = s["level"] == 2 and "self_diag_label" not in diag
        return render_template("train/tool_choose.html", options=tool_options(s["level"], diag), diag=diag,
                               error=error, need_self=need_self, diag_text=diag_phrase(s, diag["code"]),
                               self_options=list(SELF_DIAG), when=WHEN_UI, names=engine.TOOL_NAMES, **c)
    after = None
    if s["level"] >= 3 and diag.get("chosen_by") == "user":  # диагноз — после выбора, для сверки
        after = lib().t("UI-061", **{"диагноз": diag_phrase(s, diag["code"]),
                                     "да/нет": "да" if diag.get("match") else "нет"})
    targets = [{"code": t, "name": Taxonomy(td).name(t), "mechanism": Taxonomy(td).mechanism(t)}
               for t in tool.get("targets", [])]
    diag_text = diag_phrase(s, diag["code"]) if s["level"] < 3 else None
    if tool.get("source") == "pattern":  # смена механизма: вывод по механизму точнее названия направления
        diag_text = tool["header"]
    switch_short = tool["type"] == "switch" and is_short(s)
    return render_template("train/tool.html", tool=tool, diag=diag, error=error, after=after, targets=targets,
                           diag_text=diag_text, can_skip=switch_short,
                           first_strong=switch_short and first_idea_strong(s, timeout=2), **c)


def tool_options(level, diag):
    if level >= 3:
        return list(engine.TOOL_TYPES)
    return [diag["tool_type"]] + engine.ALTERNATIVES[diag["tool_type"]]


def diag_phrase(s, code):
    """Строка диагноза из LIB-13 без служебного префикса «Д1:»."""
    text = lib().t(engine.DIAG_UI[code], семейство=_rut_name(s))
    return re.sub(r"^Д[0-5—]\s*(\(вариант\))?:\s*", "", text)


def _rut_name(s):
    tax = Taxonomy(loads(s["task_json"], {}))
    fams = [tax.fam(i["family"]) for i in ideas_of(s["id"], "first") if i["family"]]
    return tax.name(max(set(fams), key=fams.count)) if fams else ""


# ---------- С6. Своя идея и бюджет подсказок (B4, §1) ----------

IDEA_MAX = 600


def needs_self_check(tool):
    """Самопроверка — для взлома цепочки и запретов (B4 §1.5 п.5)."""
    return tool.get("type") == "chain" or (tool.get("type") == "constraint" and bool(tool.get("ban")))


def hints_of(sid):
    return get_db().execute("SELECT * FROM hints WHERE session_id = ? ORDER BY id", (sid,)).fetchall()


def save_draft(sid, text):
    db = get_db()
    last = db.execute("SELECT text FROM ideas WHERE session_id = ? AND stage = 'draft' ORDER BY id DESC LIMIT 1",
                      (sid,)).fetchone()
    if last and last["text"] == text:
        return False
    n = db.execute("SELECT COUNT(*) FROM ideas WHERE session_id = ? AND stage = 'draft'", (sid,)).fetchone()[0]
    db.execute("INSERT INTO ideas (session_id, stage, ord, text, created_at) VALUES (?, 'draft', ?, ?, ?)",
               (sid, n + 1, text, now_iso()))
    db.commit()
    return True


def last_draft(sid):
    row = get_db().execute("SELECT text FROM ideas WHERE session_id = ? AND stage = 'draft' ORDER BY id DESC LIMIT 1",
                           (sid,)).fetchone()
    return row["text"] if row else ""


def hint_slots(s, tool):
    """Значения слотов LIB-08 из задачи, инструмента и зеркала."""
    td = loads(s["task_json"], {})
    tax = Taxonomy(td)
    values = {"цель": f"«{td.get('goal') or td.get('text', '')}»"}
    if tool.get("assumption"):
        values["допущение"] = f"«{tool['assumption']}»"
    ban = tool.get("ban") or ""
    if ban.lower().startswith("без "):
        values["запрет"] = ban[4:]
    fav = favorite_idea(s["id"])
    if fav and fav["family"]:
        values["механизм фаворита"] = f"«{tax.name(tax.fam(fav['family']))}»"
    if tool.get("type") == "contradiction" and tool.get("item_id"):
        k = next((x for x in lib().contradictions if x["id"] == tool["item_id"]), None)
        if k:
            values["требование A"] = f"«{lib().side_text(k['a']).lower()}»"
            values["ограничение B"] = f"«{lib().side_text(k['b']).lower()}»"
    habit = loads(s["mirror_json"], {}).get("habit")
    if habit:
        values["семейство"] = f"«{tax.name(habit[0])}»"
    if tool.get("target"):
        values["целевое"] = f"«{tax.name(tool['target'])}»"
    return values


def give_hint(s, tool, kind, draft):
    """Одна подсказка по запросу: шаблон LIB-08 → (если есть ИИ) адаптация Р6 с проверкой на утечку идеи."""
    used = hints_of(s["id"])
    if len(used) >= engine.hint_budget(s["level"]):
        return None
    td = loads(s["task_json"], {})
    rng = engine.rng_for(s["id"], "hint", kind, len(used))
    exclude = recently_shown(g.user["id"], "LIB-08") | {h["lib_id"] for h in used}
    tpl, text = engine.pick_hint(lib(), kind, tool, f"{td.get('text', '')} {td.get('goal', '')}",
                                 hint_slots(s, tool), exclude, rng)
    if tpl is None:
        return None
    source = "template"
    if ai().available:
        adapted = ai_roles.hint(ai(), lib(), kind, text, td.get("text", ""), tool.get("task_text", ""), draft)
        if adapted:
            text, source = adapted, "ai"
    db = get_db()
    db.execute("INSERT INTO hints (session_id, kind, lib_id, text, source, created_at) VALUES (?, ?, ?, ?, ?, ?)",
               (s["id"], kind, tpl["id"], text, source, now_iso()))
    db.commit()
    mark_shown(g.user["id"], "LIB-08", tpl["id"], s["id"])
    log_event("hint_used", {"session": s["id"], "kind": kind, "lib_id": tpl["id"], "source": source,
                            "n": len(used) + 1, "had_draft": bool(draft)})
    return text


def versions_of(sid):
    """Версии идеи по порядку (эволюция идеи, C1 §2): стадия version и итоговая final."""
    return get_db().execute("SELECT * FROM ideas WHERE session_id = ? AND stage IN ('version', 'final') "
                            "ORDER BY version, id", (sid,)).fetchall()


def base_for_shift(sid):
    """С чем сравниваем новую версию: первые идеи человека — его привычный ход мысли. lever — способ воздействия
    идеи по разбору «что общего» (Р16 v2), если он есть: детектор сдвига сравнивает с ним."""
    row = get_db().execute("SELECT flags_json FROM sessions WHERE id = ?", (sid,)).fetchone()
    parts = ((loads(row["flags_json"], {}) if row else {}).get("pattern") or {}).get("ideas") or []
    levers = {p.get("n"): p.get("lever") for p in parts if isinstance(p, dict)}
    return [{"text": i["text"], "xray": loads(i["xray_json"], None), "family": i["family"],
             "subfamily": i["subfamily"], "favorite": bool(i["is_favorite"]), "lever": levers.get(i["ord"])}
            for i in ideas_of(sid, "first")]


OUTCOME_TITLES = {"changed": "Механизм действительно изменился", "partial": "Изменение частичное",
                  "same": "Механизм по существу остался прежним", "unknown": "Данных недостаточно для вывода"}
OUTCOME_KIND = {"changed": "механизм", "partial": "частично", "same": "форма", "unknown": None}
BAN_STOP = {"без", "не", "нельзя", "никаких", "только", "для", "или", "and"}


def effective_kind(vf, outcome):
    """Вид сдвига для показа и оценки: по сверке способов (outcome), а для старых версий — как ответил детектор."""
    if outcome and outcome.get("verdict") in OUTCOME_KIND:
        kind = OUTCOME_KIND[outcome["verdict"]]
        return (vf or {}).get("shift") if kind == "форма" and (vf or {}).get("shift") in ("форма", "нет") else kind
    return (vf or {}).get("shift")


# траты самого человека, а не клиента: «оплатить бонусами вторую обложку» — это платит клиент (браузер 19.10)
USER_SPEND = re.compile(r"(?<!бес)платн\w*\s+(?:реклам|размещ|продвиж)|таргет|заказ\w*\s+(?:платн\w*\s+)?реклам|купить\s+реклам|аренд|нанять|наём", re.I)


def constraint_notes(text, card, bans):
    """Грубая проверка новой идеи на ограничения по правилам, без вызова модели: деньги при бюджете 0 ₽ и слова из
    запретов задачи. Только «возможно» — точнее ресурсы оценит рубрика на шаге выбора."""
    notes, low = [], (text or "").lower()
    if str(card.get("R01", "")).startswith("0") and USER_SPEND.search(low):
        notes.append("похоже, нужны деньги, а бюджет — 0 ₽")
    for ban in re.split(r"[;,]", bans or ""):
        stems = [w[:max(4, len(w) - 3)] for w in re.findall(r"[а-яёa-z]{4,}", ban.lower()) if w not in BAN_STOP]
        if stems and all(st in low for st in stems[-1:]) and any(st in low for st in stems):
            notes.append(f"возможно, нарушает запрет задачи: «{ban.strip()}»")
    return notes


VAGUE_IDEA = re.compile(r"\b(?:что|как|где|кто|какой|какая|какое|какие)[-‐](?:то|нибудь)\b|\bкое[-‐]\w+", re.I)


def outcome_verdict(vf, old, model_available=True, text=""):
    """Ядро проверки результата — без сессии (сервис и eval «outcome»): (вывод, основание) по ответу детектора
    сдвига и способам первых идей old. Основание: ai, rules (ИИ нет вовсе), failed (сбой, неполный ответ), vague."""
    vf = vf or {}
    if vf.get("source") != "ai":
        if model_available or not vf.get("shift"):  # модель есть, но не ответила, — успеха не объявляем
            return "unknown", "failed"
        return ("changed" if vf["shift"] == "механизм" else "same"), "rules"
    if vf.get("incomplete"):  # неполный ответ модели — как сбой
        return "unknown", "failed"
    if vf.get("confidence") == "low" or vf.get("clear") is False or VAGUE_IDEA.search(text or ""):
        # «Сделать что-то интересное»: живой eval 19.10 дал на ней «механизм изменился» — по такой идее не судим
        return "unknown", "vague"
    kind, newset = vf["shift"], {c for c in vf.get("levers") or [] if c in ai_roles.LEVERS}
    if newset and old:
        if newset & old and newset - old:
            return "partial", "ai"
        if newset <= old:
            return ("same" if kind != "механизм" else "partial"), "ai"
        return ("changed" if kind == "механизм" else "partial"), "ai"
    return ("changed" if kind == "механизм" else "same"), "ai"


def mechanism_outcome(s, vf, text, xray):
    """Проверка результата на уровне механизма (19.10): что было механизмом первых идей, какой способ у новой, в чём
    разница, сохранилась ли цель, не нарушены ли ограничения. Вывод — один из четырёх, без общего балла:
    changed — способ новой идеи не совпадает ни с одним способом первых идей, и детектор видит смену механизма;
    partial — новая идея добавляет способ, но держится и на прежнем, или сигналы расходятся;
    same — способ тот же, сменились слова, инструмент или канал;
    unknown — разбора нет (сбой, тайм-аут, неполный ответ модели) или идея описана слишком скупо.
    Без ИИ вовсе (нет ключа) — прежнее сравнение по направлениям, с пометкой, что это не разбор механизма."""
    p = pattern_of(s)
    first = [i for i in p.get("ideas") or [] if isinstance(i, dict)]
    old = {i["lever"] for i in first if i.get("lever")}
    rut = rut_lever(p) if (p.get("user_verdict") == "one" or (not p.get("user_verdict")
                                                                and p.get("verdict") in ("one", "partly"))) else None
    vf = vf or {}
    new = [c for c in vf.get("levers") or [] if c in ai_roles.LEVERS]
    verdict, basis = outcome_verdict(vf, old, ai().available, text)
    name = (lambda c: ai_roles.LEVERS[c]["name"])
    before = None
    if rut in ai_roles.LEVERS:
        g = next((g for g in p.get("groups") or [] if g.get("lever") == rut), None)
        examples = [e["text"] for e in (g or {}).get("evidence") or []] or [i["text"] for i in first
                                                                              if i.get("lever") == rut]
        before = {"lever": name(rut), "why": ai_roles.LEVERS[rut]["why"], "examples": examples,
                  "limit": p.get("limit") or ""}
    elif first:
        before = {"lever": None, "ideas": [{"text": i["text"], "lever": name(i["lever"]) if i.get("lever") else None}
                                           for i in first]}
    after = None
    if vf.get("source") == "ai":
        after = {"levers": [name(c) for c in new], "driver": vf.get("new_driver"), "differs": vf.get("explanation"),
                 "opens": vf.get("opens") or "", "risk": vf.get("risk") or "", "condition": condition_of(xray),
                 "goal_kept": vf.get("goal_kept")}
    kept_old = sorted(set(new) & old)
    names = (lambda codes: ", ".join(name(c) for c in codes))
    if verdict == "changed":
        conclusion = ((f"Был способ «{names(sorted(old))}», стал «{names(new)}». " if new and old else "")
                      + "Это другой способ, а не обещание результата: сработает ли он, покажет проверка.")
    elif verdict == "partial":
        if kept_old and set(new) - old:
            conclusion = (f"Добавился способ «{names([c for c in new if c not in old])}», но идея по-прежнему "
                          f"держится и на прежнем — «{names(kept_old)}».")
        elif kept_old:
            conclusion = (f"Разбор видит другую причину шага, но способ воздействия тот же, что у первых идей, — "
                          f"«{names(kept_old)}».")
        else:
            conclusion = "Способ другой, но разбор видит тот же принцип, что у одной из первых идей."
    elif verdict == "same":
        lever = names(kept_old) or (name(rut) if rut in ai_roles.LEVERS else "")
        conclusion = ("Способ тот же" + (f" — «{lever}»" if lever else "")
                      + ": поменялись слова, инструмент или канал, а причина, по которой клиент делает шаг, прежняя. "
                      "Так часто бывает: привычный способ возвращается в новой упаковке. Можно попробовать ещё раз — "
                      "или перейти к выбору: «переход пока не состоялся» — тоже честный результат.")
    elif basis == "vague":
        conclusion = ("По описанию не видно, почему клиент сделает шаг. Допишите, кто и почему должен действовать, — "
                      "тогда сравнение станет возможным.")
    else:
        conclusion = ("Разбор механизма сейчас не получился, поэтому о смене механизма ничего не утверждаем. Можно "
                      "отправить идею ещё раз или перейти к выбору.")
    if basis == "rules":
        conclusion += " Без ИИ сравниваем только направления идей, а не механизм."
    if after and after["goal_kept"] is False and verdict in ("changed", "partial"):
        conclusion += " Но идея уходит от исходной цели — это уже другая задача, а не альтернатива первым идеям."
    td, card = loads(s["task_json"], {}), loads(s["card_json"], {})
    return {"verdict": verdict, "title": OUTCOME_TITLES[verdict], "basis": basis, "conclusion": conclusion,
            "old": sorted(old), "new": new, "rut": rut, "before": before, "after": after,
            "constraints": constraint_notes(text, card, td.get("bans", ""))}


def _mechanism_of(xray, family_name):
    """Механизм одной фразой: поправка человека → разбор ИИ → название семейства."""
    if xray:
        return xray.get("driver_user") or xray.get("driver")
    return family_name


def compare_version(s, text, cls, prev, precomputed=None):
    """Детектор ложного сдвига (C1 §3): сменился механизм или только форма — против первых идей и прошлой версии.

    precomputed — (против первых, против прошлой), если ответы модели уже получены параллельно с рентгеном."""
    td = loads(s["task_json"], {})
    tax = Taxonomy(td)
    base = base_for_shift(s["id"])
    prev_item = [{"text": prev["text"], "xray": loads(prev["xray_json"], None)}] if prev else None
    vs_first = vs_prev = None
    if precomputed is not None:
        vs_first = dict(precomputed[0], source="ai") if precomputed[0] else None
        vs_prev = dict(precomputed[1], source="ai") if prev_item and precomputed[1] else None
    elif ai().available:
        task, goal = td.get("text", ""), td.get("goal", "")
        jobs = [lambda w: ai_roles.shift_check_current(w, base, text, task, goal)]
        if prev_item:
            jobs.append(lambda w: ai_roles.shift_check_current(w, prev_item, text, task, goal))
        results = run_ai_parallel(jobs)
        vs_first = dict(results[0], source="ai") if results[0] else None
        vs_prev = dict(results[1], source="ai") if prev_item and results[1] else None
    if vs_first is None:
        vs_first = engine.fallback_shift(tax.fam(cls["family"]), [tax.fam(b["family"]) for b in base])
    if prev_item and vs_prev is None:
        vs_prev = engine.fallback_shift(tax.fam(cls["family"]), [tax.fam(prev["family"])])
    return {"vs_first": vs_first, "vs_prev": vs_prev, "prev_id": prev["id"] if prev else None}


def create_version(s, text, extras, self_check):
    """Отправка идеи создаёт версию: рентген + детектор сдвига; человек дорабатывает или оставляет (C1 §2)."""
    td = loads(s["task_json"], {})
    versions = versions_of(s["id"])
    n = len(versions) + 1
    prev = versions[-1] if versions else None
    if ai().available:
        # рентген, детектор сдвига и рубрика не зависят друг от друга — один параллельный заход
        # (в живом прогоне последовательно версия ждала 17–21 с, а переход к оценке — ещё 12 с)
        base = base_for_shift(s["id"])
        prev_item = [{"text": prev["text"], "xray": loads(prev["xray_json"], None)}] if prev else None
        library, card_line = lib(), card_text(loads(s["card_json"], {}))
        # детектор сдвига v2: на отложенном эталоне precision 85% → 100% при той же полноте (13_Аудит, §7)
        task, goal = td.get("text", ""), td.get("goal", "")
        jobs = xray_jobs(td, [text]) + [lambda w: ai_roles.shift_check_current(w, base, text, task, goal)]
        if prev_item:
            jobs.append(lambda w: ai_roles.shift_check_current(w, prev_item, text, task, goal))
        card = loads(s["card_json"], {})
        rubric_job = lambda w: ask_rubric(w, library, text, td, card_line, card)  # noqa: E731
        if not PREFETCH:  # прежний путь: рубрика в том же заходе, человек ждёт и её
            jobs.append(rubric_job)
        results = run_ai_parallel(jobs)
        cls = finish_xray(td, [text], results[:1])[0]
        change = compare_version(s, text, cls, prev,
                                 precomputed=(results[1], results[2] if prev_item else None))
        if not PREFETCH:
            change["rubric"] = results[-1]  # оценка этой версии готова заранее
    else:
        cls = xray_many(td, [text])[0]
        change = compare_version(s, text, cls, prev)
    # что было механизмом, какой способ у новой идеи, сохранилась ли цель и ограничения — для «До → После»
    change["outcome"] = mechanism_outcome(s, change["vs_first"], text, cls.get("xray"))
    idea_id, _ = store_idea(s["id"], "version", n, text, td, cls=cls, version=n, change=change)
    if ai().available and PREFETCH:
        # рубрика — самый долгий из трёх вызовов версии (медиана 11 с против 7 у рентгена и детектора), а нужна
        # только на шаге «Оценка»: считаем в фоне, пока человек читает карточку версии
        run_in_background(("rubric", idea_id), text, rubric_job)
        job = overlap_job(s, text)
        if job:  # сверка с чат-ботом — тоже к шагу «Оценка», тоже в фоне
            run_in_background(("overlap", idea_id), text, job)
    db = get_db()
    for k, extra in enumerate(extras, 1):  # остальные идеи из поля — в банк как «дополнительные»
        db.execute("INSERT INTO ideas (session_id, stage, ord, text, status, created_at) "
                   "VALUES (?, 'extra', ?, ?, 'дополнительная', ?)", (s["id"], k, extra, now_iso()))
    db.commit()
    flags_update(s, self_check=self_check, multi=None, pending_version=idea_id, improve_target=None)
    shift_kind = (change["vs_first"] or {}).get("shift")
    log_event("version_created", {"session": s["id"], "n": n, "family": cls["family"], "source": cls["source"],
                                  "shift": shift_kind, "shift_source": (change["vs_first"] or {}).get("source"),
                                  "shift_prompt": ai_roles.PROMPT_VERSIONS["shift_check"],
                                  "shift_confidence": (change["vs_first"] or {}).get("confidence"),
                                  "outcome": change["outcome"]["verdict"],
                                  "hints": len(hints_of(s["id"])), "focus": request.form.get("focus") == "1"})
    return go(s["id"], "idea")


def accept_version(s, idea_id):
    db = get_db()
    if is_short(s):  # сценарий C: итог выбирают на шаге «Выбор решения» среди первой идеи и версий
        flags_update(s, pending_version=None)
        save(s["id"], state="choose")
        log_event("idea_submitted", {"session": s["id"], "flow": "short", "versions": len(versions_of(s["id"]))})
        return go(s["id"], "choose")
    db.execute("UPDATE ideas SET stage = 'final' WHERE id = ? AND session_id = ?", (idea_id, s["id"]))
    db.commit()
    flags_update(s, pending_version=None)
    save(s["id"], state="eval")
    row = db.execute("SELECT * FROM ideas WHERE id = ?", (idea_id,)).fetchone()
    log_event("idea_submitted", {"session": s["id"], "family": row["family"], "source": row["class_source"],
                                 "versions": row["version"], "hints": len(hints_of(s["id"])),
                                 "self_check": loads(s["flags_json"], {}).get("self_check")})
    return go(s["id"], "eval")


# «Хирургия идеи»: поля рентгена, по которым видно, как идея работает (ai_roles.xray)
XRAY_PARTS = (("actor", "Кто действует"), ("action", "Что делает"), ("driver", "Что побуждает"),
              ("barrier", "Какой барьер снимается"), ("value", "Что получает"), ("condition", "При каком условии"))


def surgery(vf, origin_xray, new_xray):
    """Что изменилось и что осталось — только из данных.

    dims: измерения детектора — «изменилось» (changed) и «осталось» (kept: модель назвала это прямо; у версий,
    разобранных до появления kept, строки «осталось» нет). Не названное ни там, ни там — не определено и не
    показывается. Только если сравнивал ИИ: правила видят одно семейство. parts: было/стало по полям рентгена."""
    dims = None
    if vf.get("source") == "ai" and vf.get("shift"):
        changed, kept = ai_roles.align_mechanism(vf["shift"], list(vf.get("changed") or []), list(vf.get("kept") or []))
        changed, kept = set(changed), set(kept)
        dims = [{"name": d, "changed": d in changed, "kept": d in kept} for d in ai_roles.SHIFT_DIMS
                if d in changed or d in kept] or None
    parts = None
    if origin_xray and new_xray:
        parts = []
        for key, label in XRAY_PARTS:
            before = (origin_xray.get("driver_user") if key == "driver" else None) or origin_xray.get(key)
            after = new_xray.get(key)
            if before or after:
                parts.append({"label": label, "before": before or "—", "after": after or "—"})
    return {"dims": dims, "parts": parts or None}


def version_view(row, base, tax=None):
    """Карточка версии для шаблона: рентген, сдвиг, что изменилось, откуда и куда сместился механизм."""
    x = loads(row["xray_json"], None)
    ch = loads(row["change_json"], {}) or {}
    vf = ch.get("vs_first") or {}
    outcome = ch.get("outcome")
    kind = effective_kind(vf, outcome)
    if outcome and outcome.get("basis") == "rules":  # без ИИ — прежние тексты по направлениям (UI-180/181)
        outcome = None
    same = vf.get("same_as")
    # «Откуда»: первая идея, на которую версия похожа; иначе фаворит; иначе первая по счёту
    origin = (base[same - 1] if same and same <= len(base)
              else next((b for b in base if b.get("favorite")), base[0] if base else None))
    if outcome:  # 19.10: вывод по сверке способов воздействия (mechanism_outcome)
        verdict = f"{outcome['title']}. {outcome['conclusion']}"
    elif kind == "механизм":
        verdict = lib().t("UI-180", объяснение=vf.get("explanation") or "")
    elif kind in ("форма", "нет"):
        verdict = lib().t("UI-181", **{"прежняя идея": origin["text"] if origin else "",
                                       "объяснение": vf.get("explanation") or ""})
    else:
        verdict = None
    vp = ch.get("vs_prev") or {}
    changed = vp.get("changed") or vf.get("changed") or []
    name = (lambda code: tax.name(code) if tax and code else None)
    return {"id": row["id"], "n": row["version"], "text": row["text"], "kind": kind, "verdict": verdict,
            "explanation": vf.get("explanation"), "dims": list(vf.get("changed") or []),
            "confidence": vf.get("confidence") if vf.get("source") == "ai" else None,
            "changed": lib().t("UI-184", список=", ".join(changed)) if changed else None,
            "mechanism": lib().t("UI-167", механизм=x["driver"], барьер=x["barrier"]) if x else None,
            "to_mechanism": _mechanism_of(x, name(row["subfamily"] or row["family"])),
            "to_barrier": (x or {}).get("barrier"), "to_family": name(row["family"]),
            "from_text": origin["text"] if origin else None,
            "from_mechanism": _mechanism_of(origin.get("xray"), name(origin.get("subfamily") or origin.get("family")))
            if origin else None,
            "from_family": name(origin.get("family")) if origin else None,
            "stage": row["stage"], "created_at": row["created_at"], "outcome": outcome,
            "surgery": surgery(vf, origin.get("xray") if origin else None, x)}


# ---------- Лестница выхода из тупика (C1 §5) ----------

def ladder_obstacles():
    return [o.strip() for o in lib().t("UI-186").split("?", 1)[1].split("·")]


def ladder_operations():
    return [o.strip() for o in lib().t("UI-188").split(":", 1)[1].split("·")]


def ladder_step(s, tool, action, text):
    """Одна ступень лестницы; возвращает ответ-перенаправление или None, если ступень не принята."""
    flags = loads(s["flags_json"], {})
    ladder = flags.get("ladder") or {}
    if text:
        save_draft(s["id"], text)
    if action == "ladder_start":
        ladder = {"step": 1}
    elif action == "ladder_1" and ladder.get("step") == 1:
        obstacle = request.form.get("obstacle")
        if obstacle not in ladder_obstacles():
            return None
        ladder.update(step=2, obstacle=obstacle)
    elif action == "ladder_2" and ladder.get("step") == 2:
        ladder.update(step=3, barrier=(request.form.get("barrier") or "").strip()[:200] or None)
    elif action == "ladder_3" and ladder.get("step") == 3:
        operation = request.form.get("operation")
        if operation not in ladder_operations():
            return None
        exclude = recently_shown(g.user["id"], "LIB-06")
        op = engine.pick_ladder_operator(lib(), operation, exclude,
                                         engine.rng_for(s["id"], "ladder", operation, len(exclude)))
        op_text = None
        if op:
            raw, _ = adapt(loads(s["task_json"], {}), op["template"], f"оператор «{op['name']}»",
                           ladder.get("barrier") or session_barrier(s).get("text"))
            op_text = f"«{op['name']}»: {raw[:1].lower() + raw[1:]}"
            mark_shown(g.user["id"], "LIB-06", op["id"], s["id"])
        ladder.update(step=4, operation=operation, op_text=op_text, op_id=op["id"] if op else None)
    elif action == "ladder_4" and ladder.get("step") == 4:
        what = (request.form.get("what") or "").strip()[:200]
        easier = (request.form.get("easier") or "").strip()[:200]
        if not what or not easier:
            return None
        save_draft(s["id"], f"{what[:1].upper() + what[1:]} — {easier[:1].lower() + easier[1:]}")
        log_event("ladder_step", {"session": s["id"], "step": 4, "result": "fragment"})
        flags_update(s, ladder=None, ladder_done=True)
        flash(lib().t("UI-191"))
        return go(s["id"], "idea")
    else:
        return None
    flags_update(s, ladder=ladder)
    log_event("ladder_step", {"session": s["id"], "step": ladder["step"], "obstacle": ladder.get("obstacle"),
                              "operation": ladder.get("operation")})
    return go(s["id"], "idea")


def finish_without_idea(s):
    """«Закончить без идеи» (UI-193): честно фиксируем, где возникло затруднение, без оценки и пре-мортема."""
    ladder = loads(s["flags_json"], {}).get("ladder") or {}
    flags_update(s, no_idea={"step": ladder.get("step"), "obstacle": ladder.get("obstacle")}, ladder=None,
                 pending_version=None)
    save(s["id"], state="route")
    log_event("ladder_failed", {"session": s["id"], "step": ladder.get("step"), "obstacle": ladder.get("obstacle")})
    return go(s["id"], "route")


@step_handler("idea")
def idea_step(s):
    c = ctx(s)
    tool = loads(s["tool_json"], {})
    flags = c["flags"]
    budget = engine.hint_budget(s["level"])
    error, refusal, confirm_needed = None, False, False
    text = last_draft(s["id"])
    self_check = None
    if request.method == "POST":
        action = request.form.get("action") or ("hint" if request.form.get("hint") else None)
        text = (request.form.get("idea") or "").strip()
        self_check = request.form.get("self_check")
        pending = flags.get("pending_version")
        if pending and action not in ("accept_version", "improve"):
            abort(400)  # сначала решите судьбу текущей версии
        if action == "accept_version" and pending:
            return accept_version(s, pending)
        if action == "improve" and pending:
            row = get_db().execute("SELECT * FROM ideas WHERE id = ?", (pending,)).fetchone()
            if not row or row["version"] >= engine.MAX_VERSIONS:
                abort(400)
            save_draft(s["id"], row["text"])  # доработка начинается с текста этой версии
            flags_update(s, pending_version=None)
            log_event("version_improve", {"session": s["id"], "from": row["version"]})
            return go(s["id"], "idea")
        if action and action.startswith("ladder_") and action != "ladder_quit":
            r = ladder_step(s, tool, action, text)
            if r is None:
                abort(400)
            return r
        if action == "ladder_quit":
            return finish_without_idea(s)
        if len(text) > IDEA_MAX:
            error = f"Идея длиннее {IDEA_MAX} символов — сократите до главного."
        elif action == "draft":
            if not text:
                error = "Пустой черновик не сохраняем."
            else:
                if save_draft(s["id"], text):
                    log_event("draft_saved", {"session": s["id"], "len": len(text)})
                flash("Черновик сохранён.")
                return go(s["id"], "idea")
        elif action == "hint":
            kind = request.form.get("hint")
            if kind not in engine.HINT_KINDS:
                abort(400)
            if text:
                save_draft(s["id"], text)
            if len(hints_of(s["id"])) >= budget:
                flash(lib().t("UI-084"))
            elif give_hint(s, tool, kind, text) is None:
                flash("Подходящей подсказки этого типа не нашлось — попробуйте другой тип.")
            return go(s["id"], "idea")
        elif action == "give_answer":
            refusal = True
        elif action == "soften" and not flags.get("softened"):
            if text:
                save_draft(s["id"], text)
            tool = soften_tool(s, tool)
            flags_update(s, softened=True)
            save(s["id"], tool_json=dumps(tool))
            log_event("tool_softened", {"session": s["id"], "strength": tool["strength"], "where": "idea"})
            return go(s["id"], "idea")
        elif action == "change" and not flags.get("tool_changed"):
            if text:
                save_draft(s["id"], text)
            diag = loads(s["diagnosis_json"], {})
            new_type = engine.ALTERNATIVES[tool["type"]][0]
            tool = build_tool(s, new_type)
            flags_update(s, tool_changed=True)
            diag["overridden_by_user"] = True
            save(s["id"], tool_json=dumps(tool), diagnosis_json=dumps(diag), state="tool")
            log_event("tool_changed", {"session": s["id"], "to": new_type, "where": "idea"})
            return go(s["id"], "tool")
        elif action == "pick" and flags.get("multi"):
            items = flags["multi"]
            try:
                idx = int(request.form.get("chosen", ""))
                chosen = items[idx]
            except (ValueError, IndexError):
                error = "Выберите одну идею."
            else:
                return create_version(s, chosen, [x for i, x in enumerate(items) if i != idx],
                                      flags.get("pending_check"))
        elif action == "submit":
            if not text:
                error = "Напишите идею — пустую отправить нельзя."
            elif GENERATION_REQUEST.search(text) and len(text) < 150:
                refusal = True
            elif needs_self_check(tool) and self_check not in ("yes", "no"):
                error = "Ответьте на вопрос самопроверки."
            elif self_check == "yes" and not request.form.get("confirm"):
                confirm_needed = True
                save_draft(s["id"], text)
            else:
                save_draft(s["id"], text)
                items = engine.split_ideas(text)
                if len(items) > 1:
                    flags_update(s, multi=items, pending_check=self_check)
                    return go(s["id"], "idea")
                return create_version(s, items[0], [], self_check)
        else:
            abort(400)
        if refusal:
            count = flags.get("wants_generation", 0) + 1
            flags = flags_update(s, wants_generation=count)
            log_event("wants_generation", {"session": s["id"], "where": action, "n": count})
            if text and action == "give_answer":
                save_draft(s["id"], text)
    flags = loads(load_session(s["id"])["flags_json"], {})
    hints = hints_of(s["id"])
    drafts = get_db().execute("SELECT COUNT(*) FROM ideas WHERE session_id = ? AND stage = 'draft'",
                              (s["id"],)).fetchone()[0]
    c["flags"] = flags
    base = base_for_shift(s["id"])
    history = [version_view(v, base, Taxonomy(c["task_data"])) for v in versions_of(s["id"])]
    pending_view = next((v for v in history if v["id"] == flags.get("pending_version")), None)
    if pending_view:
        history = [v for v in history if v["id"] != pending_view["id"]]
    improve_ui = {"mechanism": "UI-198", "feasibility": "UI-196"}.get(flags.get("improve_target"))
    return render_template("train/idea.html", tool=tool, hints=hints, left=max(0, budget - len(hints)),
                           budget=budget, text=text, error=error, refusal=refusal, confirm_needed=confirm_needed,
                           self_check_needed=needs_self_check(tool), self_check=self_check, drafts=drafts,
                           multi=flags.get("multi"), hint_names=engine.HINT_NAMES, pending=pending_view,
                           history=history, max_versions=engine.MAX_VERSIONS, ladder=flags.get("ladder"),
                           obstacles=ladder_obstacles(), operations=ladder_operations(),
                           improve_ui=improve_ui, **c)


# ---------- С7. Оценка (B4, §2) ----------

AUTHORSHIP_OWN = 0.5  # порог доли авторства «идея ваша» — стартовое значение, калибруется (A4 §7.4)
RUBRIC_IDS = ["RB1", "RB2", "RB3", "RB4"]
STEP_WORDS = {3: "конкретный", 2: "частично конкретный — уточним на этапе проверки",
              1: "пока размыт — уточним на этапе проверки"}


def chatbot_answers(s):
    """Ответы настоящего чат-бота из зеркала этой сессии. Заготовки LIB-14 — не ответы чат-бота: с ними не сверяем."""
    mirror = loads(s["mirror_json"], {})
    if mirror.get("baseline_source") != "ai":
        return []
    return [b["text"] for b in mirror.get("baseline") or [] if b.get("text")]


def overlap_job(s, text):
    """Задание Р15 для фонового расчёта или None, если сверять не с чем."""
    answers, td = chatbot_answers(s), loads(s["task_json"], {})
    if not (CHATBOT_OVERLAP and answers):
        return None
    return lambda w: ai_roles.chatbot_overlap(w, text, answers, td.get("text"), td.get("goal"))


def ask_rubric(worker, library, text, td, card_line, card):
    """Р9 в рабочей версии: v2 — свидетельства, якоря LIB-15 и RB2 по правилам против карточки (eval 17.10,
    отложенный набор: однозначные критерии 72% → 83%, выбор под ограничения 78% → 89%; docs/llm-quality/RESULTS.md)."""
    if ai_roles.PROMPT_VERSIONS.get("rubric") == "v2":
        return ai_roles.rubric_v2(worker, library, text, td.get("text", ""), td.get("goal", ""), card_line, card)
    return ai_roles.rubric(worker, library, text, td.get("text", ""), td.get("goal", ""), card_line)


def final_idea(sid):
    return get_db().execute("SELECT * FROM ideas WHERE session_id = ? AND stage = 'final' ORDER BY id DESC LIMIT 1",
                            (sid,)).fetchone()


def _level_word(rid, level):
    levels = lib().rubrics[rid]["levels"]
    label = (levels.get(level) or levels[str(level)])["label"]
    return label.split("—", 1)[-1].strip()


def rubric_by_rules(s, final):
    """Без ИИ: только RB2 — по атрибутам ресурсоёмкости семейства и карточке (B4 §2, A2 §7)."""
    card = loads(s["card_json"], {})
    if not final["family"] or Taxonomy(loads(s["task_json"], {})).temp or not card:
        return {}
    conflicts = engine.resource_conflicts(lib(), final["subfamily"] or final["family"], card)
    if conflicts:
        reason = "; ".join(text for _, _, text in conflicts) + " (оценка по типу решения)"
        return {"RB2": {"level": 1, "reason": reason}}
    return {"RB2": {"level": 3, "reason": "оценка по типу решения и вашей карточке ресурсов"}}


def rubric_lines(rubric):
    lines = []
    for rid in RUBRIC_IDS:
        r = rubric.get(rid)
        if not r:
            continue
        word = _level_word(rid, r["level"])
        if rid == "RB1":
            lines.append(lib().t("UI-093", уровень=word))
        elif rid == "RB2":
            reason = r.get("reason") or ""
            lines.append(lib().t("UI-094", **{"что укладывается и что нет": f"{word} — {reason[:1].lower() + reason[1:]}"
                                             if reason else word}))
        elif rid == "RB3":
            lines.append(lib().t("UI-095", **{"конкретный / пока размыт — уточним на этапе проверки":
                                             STEP_WORDS[r["level"]]}))
        else:
            lines.append(f"Механизм: {word}.")
    return lines


def compute_eval(s):
    td = loads(s["task_json"], {})
    tax = Taxonomy(td)
    mirror = loads(s["mirror_json"], {})
    common = set(mirror.get("common", []))
    final = final_idea(s["id"])
    first = ideas_of(s["id"], "first")
    fav = next((i for i in first if i["is_favorite"]), None)
    final_f = tax.fam(final["family"])
    first_f = [tax.fam(i["family"]) for i in first]
    # Сдвиг — по механизму (детектор ложного сдвига, C1 §3), без ИИ — по семействам
    vs_first = (loads(final["change_json"], {}) or {}).get("vs_first") or engine.fallback_shift(final_f, first_f) or {}
    shift_kind = effective_kind(vs_first, (loads(final["change_json"], {}) or {}).get("outcome"))
    shift = (shift_kind == "механизм") if shift_kind else None
    hints = hints_of(s["id"])
    author = engine.authorship(final["text"], [h["text"] for h in hints])
    rubric, r_source = None, None
    cached = (loads(final["change_json"], {}) or {}).get("rubric")
    if cached:  # посчитана параллельно при создании версии
        rubric, r_source = cached, "ai"
    elif ai().available:
        found, rubric = take_background(("rubric", final["id"]), final["text"])
        if not found:
            card = loads(s["card_json"], {})
            rubric = ask_rubric(ai(), lib(), final["text"], td, card_text(card), card)
        r_source = "ai" if rubric else None
        if rubric:  # сохраняем при версии, как раньше: повторная оценка и журнал берут готовое
            change = dict(loads(final["change_json"], {}) or {}, rubric=rubric)
            get_db().execute("UPDATE ideas SET change_json = ? WHERE id = ?", (dumps(change), final["id"]))
            get_db().commit()
    if not rubric:
        rubric, r_source = rubric_by_rules(s, final), "rules"
    families = {f for f in first_f + [final_f] if f}
    freq = family_frequency(final_f, mirror)
    rare = {"rare": True, "common": False}.get(freq)  # «встречается иногда» — не редкое и не частое: None
    # сверка с чат-ботом (Р15) — только подсказка «сравните сами»: на эталоне ~1/3 её совпадений ложные
    # (docs/llm-quality/RESULTS.md §4), поэтому новизну она не меняет
    overlap = chatbot_overlap_of(s, final)
    novelty, feasibility = engine.novelty_level(shift_kind, rare), engine.feasibility_level(rubric)
    type_ui, improve = engine.idea_type(shift_kind, novelty, feasibility)
    return {
        "final_id": final["id"], "family": final_f, "family_name": tax.name(final_f),
        "first_name": tax.name(tax.fam(fav["family"])) if fav and fav["family"] else _rut_name(s),
        "shift": shift, "shift_kind": shift_kind, "shift_explanation": vs_first.get("explanation"),
        "shift_source": vs_first.get("source"), "novelty": novelty, "feasibility": feasibility,
        "type_ui": type_ui, "improve": improve, "versions": final["version"] or 1,
        "rare": rare, "freq": freq, "chatbot_same": overlap, "overlap_disputed": False,
        "common_source": mirror.get("common_source"), "n_basis": mirror.get("n_basis", 0),
        "flexibility": len(families), "hints": len(hints), "authorship": author,
        "own": len(hints) == 0 or (author is not None and author >= AUTHORSHIP_OWN),
        "rubric": rubric, "rubric_source": r_source,
        "same_as_favorite": bool(fav and final_f and tax.fam(fav["family"]) == final_f),
        "taken": False, "feedback": None,
    }


def chatbot_overlap_of(s, final):
    """Ответ чат-бота с тем же механизмом, что у итоговой идеи ({text, explanation}), или None. Готовый расчёт —
    из фона (создание версии); нет — считаем сейчас. Сверяем только с настоящими ответами чат-бота этой сессии."""
    if not ai().available:
        return None
    found, r = take_background(("overlap", final["id"]), final["text"])
    if not found:
        job = overlap_job(s, final["text"])
        r = job(ai()) if job else None
    if not r or not r.get("same"):
        return None
    answers = chatbot_answers(s)
    if r["same"] > len(answers):
        return None
    return {"text": answers[r["same"] - 1].rstrip(". "), "explanation": r["explanation"]}  # точка — из текста UI-211


def premortem_enabled(s):
    """В учебной задаче проверка на прочность выключена, пока человек её не включит (UI-154)."""
    return s["mode"] != "practice" or bool(loads(s["flags_json"], {}).get("premortem_on"))


@step_handler("eval")
def eval_step(s):
    c = ctx(s)
    ev = loads(s["eval_json"], None)
    if ev is None or ev.get("final_id") != (final_idea(s["id"]) or {"id": None})["id"]:
        ev = compute_eval(s)
        save(s["id"], eval_json=dumps(ev))
        log_event("evaluated", {"session": s["id"], "shift": ev["shift"], "shift_kind": ev["shift_kind"],
                                "rare": ev["rare"], "novelty": ev["novelty"], "feasibility": ev["feasibility"],
                                "versions": ev["versions"], "flexibility": ev["flexibility"], "hints": ev["hints"],
                                "authorship": ev["authorship"], "rubric_source": ev["rubric_source"],
                                "rubric_prompt": ai_roles.PROMPT_VERSIONS.get("rubric"), "freq": ev.get("freq"),
                                "chatbot_same": bool(ev.get("chatbot_same")),
                                "rubric": {k: v["level"] for k, v in ev["rubric"].items()}})
    flags = c["flags"]
    if request.method == "POST":
        action = request.form.get("action")
        if action == "take":
            ev["taken"] = True
            db = get_db()
            db.execute("UPDATE ideas SET status = 'в работе' WHERE id = ?", (ev["final_id"],))
            db.commit()
            save(s["id"], eval_json=dumps(ev))
            log_event("idea_taken", {"session": s["id"], "idea": ev["final_id"]})
        elif action == "overlap_disagree" and ev.get("chatbot_same") and not ev.get("overlap_disputed"):
            # человек не согласен, что чат-бот предлагал тот же механизм: новизна — без этой поправки, спор — в данные
            ev["overlap_disputed"] = True
            rare = ev.get("rare")
            ev["novelty"] = engine.novelty_level(ev.get("shift_kind"), rare)
            ev["type_ui"], ev["improve"] = engine.idea_type(ev.get("shift_kind"), ev["novelty"], ev.get("feasibility"))
            save(s["id"], eval_json=dumps(ev))
            log_event("chatbot_overlap_disputed", {"session": s["id"], "idea": ev["final_id"]})
        elif action in ("too_easy", "too_hard") and not ev.get("feedback"):
            ev["feedback"] = action
            save(s["id"], eval_json=dumps(ev))
            log_event("difficulty_feedback", {"session": s["id"], "value": action})
        elif action == "improve" and can_improve(ev, flags):
            # «Доработать слабую сторону» (C1 §7): итоговая идея снова становится версией, человек делает следующую
            final = final_idea(s["id"])
            db = get_db()
            db.execute("UPDATE ideas SET stage = 'version' WHERE id = ?", (ev["final_id"],))
            db.commit()
            save_draft(s["id"], final["text"])
            flags_update(s, improve_used=True, improve_target=ev["improve"], pending_version=None)
            save(s["id"], eval_json=None, state="idea")
            log_event("improve_weak_side", {"session": s["id"], "target": ev["improve"], "from": ev["versions"]})
            return go(s["id"], "idea")
        elif action == "next":
            if s["mode"] == "practice" and request.form.get("premortem_on"):
                flags_update(s, premortem_on=True)
                s = load_session(s["id"])
            nxt = "premortem" if premortem_enabled(s) else "route"
            if nxt == "route":
                log_event("premortem_skipped", {"session": s["id"], "reason": "practice"})
            save(s["id"], state=nxt)
            return go(s["id"], nxt)
        else:
            abort(400)
        return go(s["id"], "eval")
    final = final_idea(s["id"])
    hints = hints_of(s["id"])
    td = c["task_data"]
    hint_words = words_from_hints(final["text"], [h["text"] for h in hints],
                                  [td.get("text"), td.get("goal")] + [i["text"] for i in ideas_of(s["id"], "first")]
                                  ) if hints else []
    return render_template("train/eval.html", ev=ev, final=final, lines=rubric_lines(ev["rubric"]),
                           practice=s["mode"] == "practice", can_improve=can_improve(ev, flags),
                           hint_words=hint_words, **c)


def can_improve(ev, flags):
    """Доработка слабой стороны — один раз за сессию и пока есть место для новой версии."""
    return bool(ev.get("improve")) and not flags.get("improve_used") and (ev.get("versions") or 1) < engine.MAX_VERSIONS


# ---------- Сценарий C: выбор решения под ограничения (docs/product-value/SHORT_FLOW.md) ----------

def choice_candidates(s):
    """Из чего выбирать: первая идея-фаворит и все версии. У каждой — механизм, сдвиг, ресурсы по карточке (RB2,
    правила engine.resource_fit), связь с целью и совпадение с обычным чат-ботом. Расчёты берутся из фона; чего
    нет — один параллельный заход. Результат сохраняется в change_json, повторное открытие модель не зовёт."""
    td, card, mirror = loads(s["task_json"], {}), loads(s["card_json"], {}), loads(s["mirror_json"], {})
    tax = Taxonomy(td)
    fav = favorite_idea(s["id"])
    rows = ([("first", fav)] if fav else []) + [("version", v) for v in versions_of(s["id"])]
    items, seen = [], set()
    for origin, row in rows:
        if row["text"] in seen:
            continue
        seen.add(row["text"])
        ch = loads(row["change_json"], {}) or {}
        items.append({"origin": origin, "row": row, "change": ch, "rubric": ch.get("rubric"),
                      "overlap": ch.get("overlap"), "checked": "overlap" in ch})
    missing = []
    for it in items:
        row = it["row"]
        if it["rubric"] is None and ai().available:
            found, it["rubric"] = take_background(("rubric", row["id"]), row["text"])
            if not found:
                missing.append(it)
        if it["origin"] == "version" and not it["checked"] and ai().available:
            found, r = take_background(("overlap", row["id"]), row["text"])
            answers = chatbot_answers(s)
            it["overlap"] = ({"text": answers[r["same"] - 1].rstrip(". "), "explanation": r["explanation"]}
                             if found and r and r.get("same") and r["same"] <= len(answers) else None)
    if missing:
        library, line = lib(), card_text(card)
        found = run_ai_parallel([lambda w, t=it["row"]["text"]: ask_rubric(w, library, t, td, line, card)
                                 for it in missing])
        for it, r in zip(missing, found):
            it["rubric"] = r
    db = get_db()
    for it in items:
        if not it["rubric"]:
            it["rubric"], it["rubric_source"] = rubric_by_rules(s, it["row"]), "rules"
        else:
            it["rubric_source"] = "ai"
        change = dict(it["change"], rubric=it["rubric"], overlap=it["overlap"])
        db.execute("UPDATE ideas SET change_json = ? WHERE id = ?", (dumps(change), it["row"]["id"]))
    db.commit()
    views = []
    for it in items:
        row, vf = it["row"], (it["change"].get("vs_first") or {})
        kind = effective_kind(vf, it["change"].get("outcome")) if it["origin"] == "version" else None
        fam = tax.fam(row["family"])
        rare = {"rare": True, "common": False}.get(family_frequency(fam, mirror))
        novelty = engine.novelty_level(kind, rare) if kind else None
        x = loads(row["xray_json"], None)
        rub = it["rubric"] or {}
        views.append({"id": row["id"], "origin": it["origin"], "text": row["text"],
                      "assumption": condition_of(x),  # «сработает, если…» — главный риск варианта
                      "shift_explanation": vf.get("explanation") if kind else None,
                      "shift_confidence": vf.get("confidence") if kind else None,
                      "rb1": (rub.get("RB1") or {}).get("level"), "rb2": (rub.get("RB2") or {}).get("level"),
                      "label": "Ваша первая идея (фаворит)" if it["origin"] == "first" else f"Версия {row['version']}",
                      "mechanism": _mechanism_of(x, tax.name(row["subfamily"] or row["family"])),
                      "shift_kind": kind, "novelty": novelty, "feasibility": engine.feasibility_level(it["rubric"]),
                      "lines": rubric_lines(it["rubric"]), "rubric": it["rubric"],
                      "rubric_source": it["rubric_source"], "overlap": it["overlap"], "family": fam})
    return views


@step_handler("choose")
def choose_step(s):
    c = ctx(s)
    candidates = choice_candidates(s)
    errors, values = {}, {}
    if request.method == "POST":
        chosen = next((v for v in candidates if str(v["id"]) == request.form.get("chosen")), None)
        why = (request.form.get("why") or "").strip()[:300]
        wow = request.form.get("wow")
        values = {"chosen": request.form.get("chosen"), "why": why, "wow": wow}
        if not chosen:
            errors["chosen"] = "Выберите вариант, который будете делать."
        if not why:
            errors["why"] = "Одной фразой: почему этот вариант, а не другие."
        if wow not in wow_options():
            errors["wow"] = "Ответьте, пришла бы эта идея вам в голову без тренировки."
        if not errors:
            return choose_save(s, chosen, candidates, why, wow,
                               premortem=request.form.get("action") == "premortem")
    advice = choice_advice(candidates)
    if request.method == "GET":
        prefetch_plans(s, candidates, advice)
    return render_template("train/choose.html", candidates=candidates, errors=errors, values=values,
                           wow=wow_options(), advice=advice, **c)


PLAN_PREFETCH = 3  # для скольких вариантов готовить план проверки заранее (≈ $0.0003 каждый, экономит ~8 с)


def no_money(card):
    """Бюджет в карточке — ноль: платные шаги в плане проверки не предлагаем (ai_roles.test_plan)."""
    return str(card.get("R01", "")).startswith("0")


def condition_of(x):
    """Допущение из разбора идеи для фразы «сработает, если …»: модель пишет то «если клиент…», то «клиент…»."""
    cond = ((x or {}).get("condition") or "").strip().rstrip(". ")
    return re.sub(r"^(сработает,?\s+)?если\s+", "", cond, flags=re.I)


def plan_inputs(row):
    """Что проверяем и на ком — из разбора идеи: допущение «сработает, если…» и тот, кто должен действовать."""
    x = loads(row["xray_json"], {}) or {}
    cond = condition_of(x)
    hypothesis = (f"Идея сработает, если {cond[:1].lower() + cond[1:]}" if cond
                  else "Что идея вызовет нужное действие клиента")
    return hypothesis, (x.get("actor") or "").rstrip(". ")


def prefetch_plans(s, candidates, advice):
    """План проверки за 48 часов зависит от выбранного варианта — поэтому, пока человек выбирает, готовим его для
    самых вероятных: подсказанного, последней версии, первой идеи. Выберет другой — посчитаем после выбора."""
    if not (ai().available and PREFETCH and PLAN_PREFETCH):
        return
    order = ([advice["best"]] if advice else []) + list(reversed(candidates))
    picked, seen = [], set()
    for v in order:
        if v["id"] not in seen:
            seen.add(v["id"])
            picked.append(v)
    td, card, db = loads(s["task_json"], {}), loads(s["card_json"], {}), get_db()
    library_check = clean_check(engine.check_for(lib(), None)["action"])
    line, broke = card_text(card), no_money(card)
    for v in picked[:PLAN_PREFETCH]:
        key = ("plan", s["id"], v["id"])
        with _PREFETCH_LOCK:
            if key in _PREFETCHED:
                continue
        row = db.execute("SELECT * FROM ideas WHERE id = ?", (v["id"],)).fetchone()
        hypothesis, audience = plan_inputs(row)
        run_in_background(key, row["text"], lambda w, t=row["text"], h=hypothesis, a=audience: ai_roles.test_plan(
            w, t, td.get("text", ""), td.get("goal", ""), h, a, line, library_check, no_money=broke))


FEAS_ORDER = {"высокая": 3, "средняя": 2, "низкая": 1}


def choice_advice(candidates):
    """Подсказка к выбору — без «магического балла»: сначала выполнимость при ваших ресурсах, затем связь с целью,
    при равенстве — сдвиг механизма. Честно говорит, если первая идея уже сильная, и если самый новый по механизму
    вариант не проходит по ресурсам. Выбирает человек."""
    if len(candidates) < 2:
        return None

    def key(v):
        return (FEAS_ORDER.get(v["feasibility"], 0), v["rb1"] or 0, v["shift_kind"] == "механизм")
    best = max(candidates, key=key)
    if not FEAS_ORDER.get(best["feasibility"]):
        return None  # оценок нет — подсказывать не на чем
    novel = [v for v in candidates if v["shift_kind"] == "механизм"]
    out = {"best": best, "first_strong": best["origin"] == "first" and best["feasibility"] == "высокая",
           "novel_risky": None}
    if novel and best not in novel:
        bold = max(novel, key=key)
        if FEAS_ORDER.get(bold["feasibility"], 0) < FEAS_ORDER[best["feasibility"]]:
            out["novel_risky"] = bold
    return out


def choose_save(s, chosen, candidates, why, wow, premortem=False):
    """Выбранный вариант становится итоговой идеей; первая идея — копией со стадией final (сама она остаётся первой)."""
    db = get_db()
    row = db.execute("SELECT * FROM ideas WHERE id = ?", (chosen["id"],)).fetchone()
    change = dict(loads(row["change_json"], {}) or {}, chosen_from=chosen["origin"])
    if chosen["origin"] == "first":
        cur = db.execute(
            "INSERT INTO ideas (session_id, stage, ord, text, family, subfamily, class_source, confidence, explanation, "
            "xray_json, change_json, created_at) VALUES (?, 'final', 1, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (s["id"], row["text"], row["family"], row["subfamily"], row["class_source"], row["confidence"],
             row["explanation"], row["xray_json"], dumps(change), now_iso()))
        final_id = cur.lastrowid
    else:
        db.execute("UPDATE ideas SET stage = 'final', change_json = ? WHERE id = ?", (dumps(change), row["id"]))
        final_id = row["id"]
    db.commit()
    kind = chosen["shift_kind"]
    mirror = loads(s["mirror_json"], {})
    ev = {"final_id": final_id, "flow": "short", "chosen_from": chosen["origin"], "chosen_id": chosen["id"], "why": why,
          "shift": kind == "механизм" if kind else (False if chosen["origin"] == "first" else None),
          "shift_kind": kind, "novelty": chosen["novelty"], "feasibility": chosen["feasibility"],
          "rubric": chosen["rubric"], "rubric_source": chosen["rubric_source"],
          "freq": family_frequency(chosen["family"], mirror), "chatbot_same": chosen["overlap"],
          "versions": len(versions_of(s["id"])), "hints": len(hints_of(s["id"])),
          "candidates": [{"id": v["id"], "origin": v["origin"], "feasibility": v["feasibility"],
                          "novelty": v["novelty"]} for v in candidates],
          "taken": False, "feedback": None}
    nxt = "premortem" if premortem else "check"  # проверка на прочность — по желанию, а не для каждой задачи
    if premortem:
        flags_update(s, pm_opt=True)
    save(s["id"], eval_json=dumps(ev), route_json=dumps({"wow": wow, "flow": "short"}), state=nxt)
    log_event("choice_made", {"session": s["id"], "chosen_from": chosen["origin"], "n": len(candidates),
                              "feasibility": chosen["feasibility"], "novelty": chosen["novelty"], "wow": wow,
                              "premortem": premortem, "rubric_prompt": ai_roles.PROMPT_VERSIONS.get("rubric")})
    return go(s["id"], nxt)


# ---------- С8. Пре-мортем и защита (B4, §3) ----------

RESPONSE_TYPES = {"argument": "Аргумент", "fact": "Факт", "change": "Меняю идею", "agree": "Согласен(на), принимаю риск"}
_PERIOD = re.compile(r"за\s+(\d+\s+(?:дн\w*|недел\w*|месяц\w*|квартал\w*|год\w*|лет))", re.I)


_SINGLE_PERIOD = re.compile(r"за\s+(?:(?:этот|ближайший|один|1)\s+)?(день|неделю|месяц|квартал|год)\b", re.I)
_PAST = {"день": "прошёл день", "неделю": "прошла неделя", "месяц": "прошёл месяц", "квартал": "прошёл квартал",
         "год": "прошёл год"}


def premortem_frame(td):
    """UI-100 «Представьте: прошло [срок задачи или 2 месяца]…» — срок берём из цели, если он там есть."""
    goal = td.get("goal") or ""
    m = _PERIOD.search(goal)
    if m:
        return lib().t("UI-100", срок=m.group(1))
    m = _SINGLE_PERIOD.search(goal)
    text = lib().t("UI-100", срок="2 месяца")
    return text.replace("прошло 2 месяца", _PAST[m.group(1).lower()]) if m else text


def objections_of(sid, author=None):
    db = get_db()
    if author:
        return db.execute("SELECT * FROM objections WHERE session_id = ? AND author = ? ORDER BY id",
                          (sid, author)).fetchall()
    return db.execute("SELECT * FROM objections WHERE session_id = ? ORDER BY id", (sid,)).fetchall()


def field_label(fid):
    return next((f["label"] for f in lib().card_fields if f["id"] == fid), fid)


def objection_line(o):
    """UI-104: «Не сработает, если [условие]. Связано с: [поле или механизм]. Проверить можно так: [способ]»."""
    cond = re.sub(r"^\s*не сработает,?\s*если\s*", "", o["condition"] or "", flags=re.I).rstrip(". ")
    link = field_label(o["card_field"]) if o["card_field"] else (o["mechanism_link"] or "механизм идеи")
    return lib().t("UI-104", условие=cond, **{"поле карточки или механизм": link,
                                              "способ": clean_check(o["check_text"]) or "—"})


def clean_check(text):
    """Текст проверки без служебных ссылок на коды LIB-10 («(из T03 или T05)»)."""
    return re.sub(r"\s*\((?:из|см\.?)\s+T\d\d[^)]*\)", "", text or "").strip().rstrip(". ")


def objection_text(o):
    """Возражение одной фразой для вставки в середину предложения (UI-110, UI-115, UI-120)."""
    text = (o["condition"] or "").strip().rstrip(". ")
    return text[:1].lower() + text[1:] if text else ""


def verdict_line(o):
    ui_id = {"снято": "UI-107", "ослаблено": "UI-108", "в силе": "UI-109"}.get(o["verdict"])
    if not ui_id:
        return ""
    basis = (o["reason"] or o["basis"] or "").rstrip(". ")
    return lib().t(ui_id, основание=basis[:1].lower() + basis[1:])


def build_objections(s, reasons):
    """Р7 критик или LIB-09 по правилам: до 3 возражений, которых человек не назвал."""
    td = loads(s["task_json"], {})
    card = loads(s["card_json"], {})
    final = final_idea(s["id"])
    items, source = None, "library"
    if ai().available:
        items = ai_roles.critic(ai(), lib(), final["text"], td.get("text", ""), td.get("goal", ""),
                                card_text(card, with_ids=True), reasons)
        source = "ai" if items else "library"
    if not items:
        fam = None if Taxonomy(td).temp else (final["subfamily"] or final["family"])
        items = engine.fallback_objections(lib(), fam, card, reasons, engine.rng_for(s["id"], "pm"),
                                           idea_text=final["text"])
    db = get_db()
    for o in items:
        db.execute("INSERT INTO objections (session_id, author, lib_id, category, card_field, mechanism_link, "
                   "condition, check_text, check_id, severity, created_at) "
                   "VALUES (?, 'critic', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   (s["id"], o.get("lib_id"), o["category"], o.get("card_field"), o.get("mechanism_link"),
                    o["condition"], o.get("check"), o.get("check_id"), o.get("severity", 2), now_iso()))
    db.commit()
    for o in items:
        if o.get("lib_id"):
            mark_shown(g.user["id"], "LIB-09", o["lib_id"], s["id"])
    return source, len(items)


def judge_many(s, items):
    """Р8 — отдельный вызов без истории на каждый ответ; вызовы идут параллельно (в живом прогоне три
    вердикта подряд занимали до 22 с). Без ИИ — правила: настойчивость не снимает возражение."""
    final = final_idea(s["id"])
    verdicts = [None] * len(items)
    asked = [i for i, (_, rtype, _) in enumerate(items) if rtype != "agree"]
    if asked and ai().available:
        idea, card_line = final["text"], card_text(loads(s["card_json"], {}))
        jobs = []
        for i in asked:
            obj, rtype, text = items[i]
            objection = f"{obj['condition']} (проверка: {obj['check_text'] or '—'})"
            jobs.append(lambda w, o=objection, r=rtype, t=text: ai_roles.judge(w, idea, card_line, o, r, t))
        for i, v in zip(asked, run_ai_parallel(jobs)):
            verdicts[i] = dict(v, source="ai") if v else None
    out = []
    for (obj, rtype, text), verdict in zip(items, verdicts):
        if rtype == "agree":  # человек принимает риск — оценивать нечего, ИИ не нужен
            verdict = {"verdict": "в силе", "basis": "нет", "reason": "Риск принят.", "source": "rules"}
        elif not verdict:
            verdict = dict(engine.fallback_verdict(rtype, text), source="rules")
        if rtype == "fact" and verdict["verdict"] == "снято":
            verdict["verdict"] = "ослаблено"  # факт не проверен — максимум «ослаблено» (B4 §3.11)
        out.append(verdict)
    return out


def strongest_of(sid):
    return engine.strongest_open([dict(o) for o in objections_of(sid, "critic")])


@step_handler("premortem")
def premortem_step(s):
    c = ctx(s)
    flags = c["flags"]
    td = c["task_data"]
    critic_rows = objections_of(s["id"], "critic")
    user_rows = objections_of(s["id"], "user")
    phase = flags.get("pm_phase", "reasons")
    errors, values = {}, {}
    if request.method == "POST":
        action = request.form.get("action")
        after = "check" if is_short(s) else "route"  # в коротком пути маршрута нет
        if action == "skip":
            flags_update(s, premortem_skipped=True)
            save(s["id"], state=after)
            log_event("premortem_skipped", {"session": s["id"], "phase": phase, "reason": "user"})
            return go(s["id"], after)
        if action == "reasons" and phase == "reasons":
            reasons = [(request.form.get(f"reason{i}") or "").strip()[:300] for i in (1, 2)]
            reasons = [r for r in reasons if r]
            none = bool(request.form.get("none"))
            if not reasons and not none:
                errors["reasons"] = "Назовите хотя бы одну причину — или отметьте, что не можете назвать ни одной."
            else:
                db = get_db()
                for r in reasons:
                    db.execute("INSERT INTO objections (session_id, author, condition, about_me, created_at) "
                               "VALUES (?, 'user', ?, 1, ?)", (s["id"], r, now_iso()))
                db.commit()
                source, n = build_objections(s, reasons)
                flags_update(s, pm_phase="defense" if n else "done", pm_no_reasons=not reasons)
                log_event("premortem_reasons", {"session": s["id"], "user": len(reasons), "critic": n,
                                                "critic_source": source})
                return go(s["id"], "premortem")
        elif action == "defend" and phase in ("defense", "round2"):
            rnd = 1 if phase == "defense" else 2
            targets = [o for o in critic_rows if rnd == 1 or o["verdict"] in ("в силе", "ослаблено")]
            answers = {}
            for o in targets:
                oid = o["id"]
                about = request.form.get(f"about_{oid}")
                rtype = request.form.get(f"type_{oid}")
                text = (request.form.get(f"text_{oid}") or "").strip()[:400]
                values[oid] = {"about": about, "type": rtype, "text": text}
                if rnd == 2 and not rtype and not text:
                    continue  # во втором раунде можно ответить не на все
                if rnd == 1 and about not in ("1", "0"):
                    errors[oid] = "Отметьте: про вас это возражение или нет."
                elif rtype not in RESPONSE_TYPES:
                    errors[oid] = "Выберите, как отвечаете."
                elif rtype != "agree" and not text:
                    errors[oid] = "Напишите ответ коротко."
                else:
                    answers[oid] = (about, rtype, text)
            if rnd == 2 and not answers and not errors:
                errors["round"] = "Ответьте хотя бы на одно возражение — или переходите дальше."
            if not errors:
                db = get_db()
                answered = [o for o in targets if o["id"] in answers]
                verdicts = judge_many(s, [(o, answers[o["id"]][1], answers[o["id"]][2]) for o in answered])
                for o, v in zip(answered, verdicts):
                    about, rtype, text = answers[o["id"]]
                    if rnd == 2:  # прежний ответ — в журнал событий, в таблице остаётся последний раунд
                        log_event("premortem_round1", {"session": s["id"], "objection": o["id"],
                                                       "type": o["response_type"], "verdict": o["verdict"]})
                    db.execute("UPDATE objections SET about_me = COALESCE(?, about_me), response_type = ?, "
                               "response_text = ?, verdict = ?, basis = ?, reason = ?, verdict_source = ?, round = ? "
                               "WHERE id = ?", (int(about) if about in ("0", "1") else None, rtype, text,
                                                v["verdict"], v["basis"], v["reason"], v["source"], rnd, o["id"]))
                db.commit()
                flags_update(s, pm_phase="verdicts" if rnd == 1 else "done")
                log_event("premortem_defended", {"session": s["id"], "round": rnd, "answered": len(answers)})
                return go(s["id"], "premortem")
        elif action == "round2" and phase == "verdicts" and s["level"] >= 2:
            if any(o["verdict"] in ("в силе", "ослаблено") for o in critic_rows):
                flags_update(s, pm_phase="round2")
            return go(s["id"], "premortem")
        elif action == "finish" and phase in ("verdicts", "done"):
            strongest = strongest_of(s["id"])
            if strongest is None and user_rows:  # критику нечего добавить — берём причину самого человека
                strongest = dict(user_rows[0])
            flags_update(s, pm_phase="finished", strongest=strongest["id"] if strongest else None)
            save(s["id"], state=after)
            log_event("premortem_done", {"session": s["id"], "strongest": strongest["id"] if strongest else None,
                                         "about_me": sum(1 for o in critic_rows if o["about_me"] == 1),
                                         "critic": len(critic_rows)})
            return go(s["id"], after)
        else:
            abort(400)
    final = final_idea(s["id"])
    strongest = None
    if phase in ("verdicts", "done"):
        strongest = strongest_of(s["id"]) or (dict(user_rows[0]) if user_rows else None)
    can_round2 = (phase == "verdicts" and s["level"] >= 2
                  and any(o["verdict"] in ("в силе", "ослаблено") for o in critic_rows))
    return render_template("train/premortem.html", phase=phase, final=final, critic=critic_rows, mine=user_rows,
                           frame=premortem_frame(td), errors=errors, values=values, types=RESPONSE_TYPES,
                           strongest=strongest, can_round2=can_round2, line=objection_line, verdict=verdict_line,
                           objection_text=objection_text, **c)


# ---------- С9. Маршрут мысли (B4, §4) ----------

DIAG_SHORT = {"Д0": "с ходу идей было мало", "Д1": "все идеи шли одной колеёй", "Д2": "направления разные, но привычные",
              "Д3": "держались за любимую идею", "Д4": "упёрлись в ресурсы", "Д5": "две сильные идеи разного типа",
              "Д—": "явного тупика не было"}
HELPED = {"mirror": "Увидеть, что мои идеи однотипны", "tool": "Сам инструмент", "hint": "Подсказка",
          "premortem": "Проверка на прочность", "knew": "Я и так знал(а)", "nothing": "Ничего не помогло"}


def strongest_objection(s):
    sid = loads(s["flags_json"], {}).get("strongest")
    if not sid:
        return None
    row = get_db().execute("SELECT * FROM objections WHERE id = ? AND session_id = ?", (sid, s["id"])).fetchone()
    return dict(row) if row else None


def route_text(s):
    ev = loads(s["eval_json"], {})
    diag = loads(s["diagnosis_json"], {})
    tool = loads(s["tool_json"], {})
    no_idea = loads(s["flags_json"], {}).get("no_idea")
    if no_idea:  # попытка без идеи (C1 §5): честно фиксируем, где возникло затруднение
        return (f"Начали с «{_rut_name(s) or 'ваших первых идей'}» → {DIAG_SHORT.get(diag.get('code'), '—')} → "
                f"«{tool.get('name', '—')}» → идея не сложилась"
                + (f"; затруднение: «{no_idea['obstacle'].lower()}»." if no_idea.get("obstacle") else "."))
    strongest = strongest_objection(s)
    text = lib().t("UI-115", **{
        "семейства": ev.get("first_name") or "ваших первых идей",
        "диагноз одной фразой": DIAG_SHORT.get(diag.get("code"), "—"),
        "инструмент": f"«{tool.get('name', '—')}»",
        "семейство финала": ev.get("family_name") or "нового для нас направления",
        "возражение": f"«{objection_text(strongest)}»" if strongest else "не выбрано: проверку на прочность пропустили",
    })
    return text.replace("««", "«").replace("»»", "»")


def helped_history(uid, exclude_sid):
    rows = get_db().execute("SELECT route_json FROM sessions WHERE user_id = ? AND id != ? AND route_json IS NOT NULL "
                            "ORDER BY id", (uid, exclude_sid)).fetchall()
    return [loads(r["route_json"], {}).get("helped") for r in rows if loads(r["route_json"], {}).get("helped")]


@step_handler("route")
def route_step(s):
    c = ctx(s)
    text = route_text(s)
    history = helped_history(g.user["id"], s["id"])
    before = None
    if len(history) >= 2:  # с 3-й сессии — сравнение (B4 §4.4 п.4)
        top = max(set(history), key=history.count)
        before = lib().t("UI-119", что=HELPED[top].lower())
    error = None
    no_idea = bool(c["flags"].get("no_idea"))
    if request.method == "POST":
        helped = request.form.get("helped")
        correction = (request.form.get("correction") or "").strip()[:300]
        wow = request.form.get("wow")
        if helped not in HELPED:
            error = "Выберите один вариант."
        elif not no_idea and wow not in wow_options():
            error = "Ответьте, пришла бы эта идея вам в голову без тренировки."
        else:
            save(s["id"], route_json=dumps({"text": text, "correction": correction or None, "helped": helped,
                                            "wow": wow if not no_idea else None}),
                 state="done" if no_idea else "check")
            log_event("route_done", {"session": s["id"], "helped": helped, "corrected": bool(correction),
                                     "wow": wow, "no_idea": no_idea})
            if no_idea:  # проверять нечего — завершаем без плана
                complete_session(load_session(s["id"]), plan_saved=False)
                return go(s["id"], "done")
            return go(s["id"], "check")
    base = base_for_shift(s["id"])
    genealogy = [version_view(v, base, Taxonomy(c["task_data"])) for v in versions_of(s["id"])]
    fav = favorite_idea(s["id"])
    return render_template("train/route.html", text=text, before=before, helped=HELPED, error=error,
                           genealogy=genealogy, start=fav["text"] if fav else None, wow=wow_options(),
                           no_idea=no_idea, **c)


def wow_options():
    """«Пришла бы вам эта идея в голову без тренировки?» (UI-201) — главный вопрос о моменте открытия."""
    return [o.strip() for o in lib().t("UI-201").split("?", 1)[1].split("·")]


# ---------- С10. Проверка за 48 часов и завершение сессии (B4, §5) ----------

DUE = {"сегодня": 0, "завтра": 1, "послезавтра": 2}
PAID_WORDS = re.compile(r"₽|руб|оплат|купить|заказать рекламу|(?<!бес)платн", re.I)  # «бесплатно» — не платно


def objection_for_check(s):
    """Самое сильное открытое возражение; для причины пользователя категорию угадываем по словам."""
    o = strongest_objection(s)
    if o and not o.get("category"):
        text = (o["condition"] or "").lower()
        o["category"] = next((cat for cat, words in engine.CATEGORY_WORDS.items()
                              if any(w in text for w in words)), "К1")
    return o


def check_plan(s):
    """План проверки за 48 часов, отвечающий на пять вопросов: что проверяем (самое сильное открытое возражение или
    допущение «сработает, если…» из разбора идеи), на ком (кто должен действовать), какой шаг, какой сигнал «за» и
    какой — «пересмотреть». ИИ (Р5 v2) подстраивает шаг и сигналы одним вызовом; не вышло — прежняя адаптация
    действия и сигналы шаблона LIB-10."""
    td = loads(s["task_json"], {})
    o = objection_for_check(s)
    tpl = engine.check_for(lib(), o)
    final = final_idea(s["id"])
    hypothesis, audience = plan_inputs(final) if final else ("Что идея вызовет нужное действие клиента", "")
    if o:
        hypothesis = f"Риск: не сработает, если {objection_text(o)}"
    card = loads(s["card_json"], {})
    action, source = clean_check(tpl["action"]), "library"
    ok, fail = tpl.get("ok") or "", tpl.get("fail") or ""
    planned = None
    chosen_id = (loads(s["eval_json"], {}) or {}).get("chosen_id")
    if final and chosen_id and not o:  # план, подготовленный, пока человек выбирал
        planned = take_background(("plan", s["id"], chosen_id), final["text"])[1]
    if ai().available and final and not planned:
        planned = ai_roles.test_plan(ai(), final["text"], td.get("text", ""), td.get("goal", ""), hypothesis,
                                     audience, card_text(card), action, no_money=no_money(card))
    if planned:
        action, ok, fail, source = planned["step"], planned["success"], planned["fail"], "ai"
        if planned.get("assumption") and not o:  # роль видит идею целиком: допущение из разбора бывает неточным
            a = planned["assumption"]
            hypothesis = f"Идея сработает, если {a[:1].lower() + a[1:]}"
        audience = planned.get("audience") or audience
    if ai().available and not planned:
        kind = ("проверка за 48 часов не дольше 2 часов"
                + (f"; проверяет возражение: <<<{o['condition']}>>>" if o else ""))  # текст может быть от человека
        adapted = ai_roles.adapt_tool(ai(), td.get("text", ""), td.get("goal", ""), action, kind,
                                      idea=final["text"] if final else None)
        if adapted:
            action, source = adapted.rstrip(". "), "ai"
    paid = bool(PAID_WORDS.search(action)) and str(card.get("R01", "")).startswith("0")
    return {"template": tpl, "action": action, "source": source, "objection": o, "paid": paid,
            "hypothesis": hypothesis, "audience": audience, "ok": ok, "fail": fail, "planned": bool(planned),
            "today": (planned or {}).get("today") or ""}


def complete_session(s, plan_saved):
    """Завершение: статусы идей, сила инструмента, уровень опор, события (B3 §6, B5 §2)."""
    uid = g.user["id"]
    db = get_db()
    ev = loads(s["eval_json"], {})
    tool = loads(s["tool_json"], {})
    flags = loads(s["flags_json"], {})
    final_id = ev.get("final_id")
    if final_id:
        status = "к проверке" if plan_saved else ("в работе" if ev.get("taken") else "без проверки")
        db.execute("UPDATE ideas SET status = ? WHERE id = ?", (status, final_id))
    db.execute("UPDATE ideas SET status = 'черновик' WHERE session_id = ? AND stage IN ('draft', 'retry') "
               "AND status IS NULL", (s["id"],))
    db.execute("UPDATE ideas SET status = 'первая' WHERE session_id = ? AND stage = 'first' AND status IS NULL",
               (s["id"],))
    db.commit()
    state = get_state(uid)
    strengths = loads(state["strengths_json"], {})
    if tool.get("type"):
        too_weak = ev.get("feedback") == "too_easy" or ev.get("same_as_favorite") or ev.get("rare") is False
        softened = bool(flags.get("softened")) or ev.get("feedback") == "too_hard"
        strengths = engine.next_strengths(strengths, tool["type"], softened=softened,
                                          too_weak=bool(too_weak) and not softened)
    ts = now_iso()
    save(s["id"], state="done", completed_at=ts)
    done = done_sessions(uid)
    history = [(d["level"], loads(d["eval_json"], {}).get("shift"), loads(d["eval_json"], {}).get("hints", 0))
               for d in done]
    level, reason = engine.next_level(state["level"], history)
    update_state(uid, strengths_json=dumps(strengths), level=level)
    if reason:
        db.execute("INSERT INTO scaffold_changes (user_id, from_level, to_level, reason, at) VALUES (?, ?, ?, ?, ?)",
                   (uid, state["level"], level, reason, ts))
        db.commit()
        log_event("scaffold_changed", {"from": state["level"], "to": level, "reason": reason})
        flash(lib().t({2: "UI-136", 3: "UI-137"}.get(level, "UI-138")) if level > state["level"] else lib().t("UI-138"))
    log_event("session_completed", {"session": s["id"], "mode": s["mode"], "shift": ev.get("shift"),
                                    "hints": ev.get("hints"), "plan": plan_saved, "level": state["level"],
                                    "strengths": strengths})


@step_handler("check")
def check_step(s):
    c = ctx(s)
    plan = c["flags"].get("check_plan")
    if plan is None:  # подбор шаблона (и адаптация ИИ) — один раз за сессию
        plan = check_plan(s)
        flags_update(s, check_plan=plan)
    errors, values = {}, {}
    if request.method == "POST":
        action = request.form.get("action")
        if action == "skip":
            log_event("check_skipped", {"session": s["id"]})
            complete_session(s, plan_saved=False)
            return go(s["id"], "done")
        if action != "save":
            abort(400)
        values = {k: (request.form.get(k) or "").strip()[:400]
                  for k in ("plan", "when", "ok", "fail", "hypothesis", "audience")}
        if not values["plan"]:
            errors["plan"] = "Опишите, что именно сделаете."
        if values["when"] not in DUE:
            errors["when"] = "Выберите срок."
        if not values["ok"] or not values["fail"]:
            errors["signals"] = "Запишите оба сигнала: что будет в пользу идеи и что заставит её пересмотреть."
        if not errors:
            due = (now() + timedelta(days=DUE[values["when"]])).date().isoformat()
            o = plan["objection"]
            db = get_db()
            db.execute("INSERT INTO experiments (session_id, objection_id, template_id, plan, due, success_signal, "
                       "fail_signal, hypothesis, audience, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (s["id"], o["id"] if o else None, plan["template"]["id"], values["plan"], due,
                        values["ok"], values["fail"], values["hypothesis"] or None, values["audience"] or None,
                        now_iso()))
            db.commit()
            log_event("check_planned", {"session": s["id"], "template": plan["template"]["id"], "due": due,
                                        "source": plan["source"]})
            complete_session(s, plan_saved=True)
            return go(s["id"], "done")
    o = plan["objection"]
    title = lib().t("UI-120", возражение=objection_text(o) if o else (plan.get("hypothesis") or "что идея сработает"))
    return render_template("train/check.html", plan=plan, title=title, errors=errors, values=values,
                           due_options=list(DUE), **c)


ORIGIN_WHO = {"you": "Вы", "service": "Тренажёр", "ai": "ИИ"}
SOURCE_NOTE = {"ai": "формулировку адаптировал ИИ", "library": "из библиотеки тренажёра",
               "template": "из библиотеки тренажёра", "user": "по вашим словам"}


def words_from_hints(final_text, hint_texts, own_texts):
    """Слова итоговой идеи, совпавшие со словами подсказок, — ровно та основа, на которой считается доля авторства
    (A4 §2.7). also_own: слово было и в задаче или в первых идеях — то есть могло прийти не из подсказки."""
    hint_stems = set().union(*(engine.stems(t) for t in hint_texts)) if hint_texts else set()
    own = set().union(*(engine.stems(t) for t in own_texts if t)) if own_texts else set()
    out, seen = [], set()
    for raw in re.findall(r"[А-Яа-яЁёA-Za-z0-9]+", final_text or ""):
        w = raw.lower().replace("ё", "е")
        if len(w) < 4 or w in engine.STOP or w[:5] in seen or w[:5] not in hint_stems:
            continue
        seen.add(w[:5])
        out.append({"word": raw.lower(), "also_own": w[:5] in own})
    return out


def elapsed_label(delta):
    """Сколько прошло с первых идей: минуты, часы или дни — сессию могли отложить и вернуться."""
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"{minutes} мин"
    if minutes < 24 * 60:
        return f"{minutes // 60} ч {minutes % 60} мин" if minutes % 60 else f"{minutes // 60} ч"
    return f"через {minutes // (24 * 60)} дн."


def origin_story(s, td, versions, incubations):
    """Паспорт происхождения идеи: что сделали вы, что дал тренажёр и где работал ИИ — по времени, из сохранённых
    данных сессии. Связей «эта подсказка породила эту версию» не строим: данные их не подтверждают."""
    sid = s["id"]
    first = ideas_of(sid, "first")
    hints = hints_of(sid)
    tool = loads(s["tool_json"], {})
    flags = loads(s["flags_json"], {})
    barrier = flags.get("barrier") or {}
    rows = get_db().execute(
        "SELECT name, props_json, at FROM events WHERE user_id = ? AND name IN ('diagnosis_made', 'tool_shown', "
        "'tool_softened', 'tool_changed', 'ladder_step', 'wants_generation') ORDER BY id", (s["user_id"],)).fetchall()
    events = [(r["name"], loads(r["props_json"], {}), r["at"]) for r in rows]
    events = [e for e in events if e[1].get("session") == sid]
    steps = []

    def add(at, who, title, text=None, note=None, node=False, cls="", ai_touch=False):
        # ai_touch: материал тренажёра, формулировку которого подстроил ИИ, — автор «Тренажёр», засечка отмечена
        steps.append({"at": at, "who": who, "who_name": ORIGIN_WHO[who], "title": title, "text": text,
                      "note": note, "node": node, "cls": cls, "ai_touch": ai_touch or who == "ai",
                      "order": len(steps)})

    if first:
        add(first[0]["created_at"], "you", f"Первые идеи — {len(first)}", note="до инструмента и подсказок",
            node=True, cls="is-start")
    ladder_noted = False
    for name, p, at in events:
        if name == "diagnosis_made" and barrier:
            lens = ai_roles.LENS_NAMES.get(barrier.get("lens"))
            if barrier.get("source") == "user":
                add(at, "you", "Препятствие — своими словами", barrier.get("text"))
            elif barrier.get("source") == "ai":
                add(at, "ai", "Препятствие", barrier.get("text"),
                    note=f"линзу «{lens}» выбрали вы, формулировку предложил ИИ" if lens else None)
            elif barrier.get("question"):
                add(at, "service", "Вопрос-линза", barrier.get("question"),
                    note=f"линзу «{lens}» выбрали вы" if lens else None)
        elif name == "tool_shown":
            add(at, "service", f"Инструмент «{engine.TOOL_NAMES.get(p.get('type'), tool.get('name', ''))}»",
                note=SOURCE_NOTE.get(tool.get("source")), ai_touch=tool.get("source") == "ai")
        elif name == "tool_softened":
            add(at, "service", "Задание облегчено", note="по вашей просьбе")
        elif name == "tool_changed":
            add(at, "service", f"Инструмент сменён на «{engine.TOOL_NAMES.get(p.get('to'), p.get('to'))}»",
                note="по вашей просьбе")
        elif name == "ladder_step":
            if p.get("step") == 1 and not ladder_noted:
                add(at, "service", "Лестница «я застрял(а)»", note="вы попросили сменить способ мышления")
                ladder_noted = True
            elif p.get("step") == 4 and p.get("operation"):
                add(at, "service", f"Операция «{p['operation']}»", note="выбрали вы, задание к ней подобрал тренажёр")
            elif p.get("result") == "fragment":
                add(at, "you", "Начало идеи из лестницы", note="ваши слова по шагам лестницы")
        elif name == "wants_generation":
            add(at, "service", "Просьба «просто дай ответ»", note="тренажёр не пишет идею за вас — отказал")
    for h in hints:
        add(h["created_at"], "service", f"Подсказка «{engine.HINT_NAMES.get(h['kind'], h['kind'])}»", h["text"],
            note="из библиотеки, формулировку подстроил под задачу ИИ" if h["source"] == "ai"
            else "из библиотеки тренажёра", ai_touch=h["source"] == "ai")
    for v in versions + incubations:
        title = "После сессии (второе открытие)" if v["stage"] == "incubation" else f"Версия {v['n']}"
        kind = ("сдвиг механизма" if v["kind"] == "механизм" else "та же ветка" if v["kind"] in ("форма", "нет")
                else None)
        add(v["created_at"], "you", title, v["text"], note=kind, node=True,
            cls=("is-shift" if v["kind"] == "механизм" else "is-same" if kind else "")
            + (" is-final" if v["stage"] == "final" else ""))
    start = parse_time(first[0]["created_at"]) if first else None
    for st in steps:
        t = parse_time(st["at"])
        st["elapsed"] = ("старт" if "is-start" in st["cls"] else elapsed_label(t - start)) if start and t and t >= start             else None
    steps.sort(key=lambda st: (st["at"] or "", st["order"]))
    final = next((v for v in versions if v["stage"] == "final"), None)
    words = words_from_hints(final["text"], [h["text"] for h in hints],
                             [td.get("text"), td.get("goal")] + [i["text"] for i in first]) if final and hints else []
    n_ai = sum(1 for h in hints if h["source"] == "ai")
    return {"steps": steps, "hints": len(hints), "hints_ai": n_ai, "tool_ai": tool.get("source") == "ai",
            "words": words, "final": bool(final),
            "ai_parsed": any(i["xray_json"] for i in first) or any(v["mechanism"] for v in versions),
            "ladder": any(n == "ladder_step" for n, _, _ in events),
            "refusals": sum(1 for n, _, _ in events if n == "wants_generation")}


def session_story(s):
    """Из чего собраны журнал идеи и карточка в банке: первые идеи, версии и сдвиг, возражения, проверка.
    Только сохранённые данные сессии — ничего не досчитываем и не вызываем ИИ."""
    sid = s["id"]
    td = loads(s["task_json"], {})
    tax = Taxonomy(td)
    mirror = loads(s["mirror_json"], {})
    common = set(mirror.get("common", []))
    first = []
    for i in ideas_of(sid, "first"):
        f = tax.fam(i["family"])
        freq = family_frequency(f, mirror)
        first.append({"text": i["text"], "favorite": bool(i["is_favorite"]), "family": tax.name(i["family"]),
                      "common": bool(f and f in common), "known": bool(f),
                      "freq_word": {"common": "частое", "rare": "редкое"}.get(freq) or lib().t("UI-208"),
                      "mechanism": _mechanism_of(loads(i["xray_json"], None), None)})
    base = base_for_shift(sid)
    versions = [version_view(v, base, tax) for v in versions_of(sid)]
    incubations = [version_view(v, base, tax) for v in get_db().execute(
        "SELECT * FROM ideas WHERE session_id = ? AND stage = 'incubation' ORDER BY id", (sid,)).fetchall()]
    strongest = strongest_objection(s)
    flags = loads(s["flags_json"], {})
    ev = loads(s["eval_json"], {})
    db = get_db()
    return {"td": td, "card_line": card_text(loads(s["card_json"], {})), "first": first,
            "barrier": flags.get("barrier"), "tool": loads(s["tool_json"], {}),
            "diag_text": diag_phrase(s, loads(s["diagnosis_json"], {}).get("code", "Д—")),
            "versions": versions, "incubations": incubations,
            "final_view": next((v for v in versions if v["stage"] == "final"), None),
            "ev": ev, "lines": rubric_lines(ev.get("rubric") or {}),
            "objections": [dict(o, line=objection_line(o), verdict_text=verdict_line(o))
                           for o in objections_of(sid, "critic")],
            "risk": objection_text(strongest) if strongest else None,
            "exp": db.execute("SELECT * FROM experiments WHERE session_id = ?", (sid,)).fetchone(),
            "outcomes": db.execute("SELECT * FROM outcomes WHERE session_id = ? ORDER BY day", (sid,)).fetchall(),
            "route": loads(s["route_json"], {}), "no_idea": flags.get("no_idea"), "helped": HELPED,
            "origin": origin_story(s, td, versions, incubations)}


@bp.get("/s/<int:sid>/journal")
@login_required
def journal(sid):
    """Лабораторный журнал идеи: компактный итог сессии из реальных данных — сохранить или показать другому."""
    s = load_session(sid)
    final = final_idea(sid)
    if s["state"] != "done" or (not final and not loads(s["flags_json"], {}).get("no_idea")):
        return redirect(url_for("train.session_home", sid=sid))
    log_event("journal_opened", {"session": sid})
    return render_template("train/journal.html", s=s, final=final, **session_story(s))


INCUBATION_AFTER = timedelta(days=1)  # «второе открытие» — через день, только по желанию (C1 §6)


# Итоговая оценка тренировки. Вопросы 2–3 — дословно B6 §2.6: ответы прямо ложатся на метрику H4 из реестра
# («ChatGPT не заменит» или «стал(а) бы искать аналог»). Вопрос 4 — намерение; H3 в реестре — фактический возврат.
EXIT_REPLACE = ("Полностью заменит", "Частично", "Не заменит")
EXIT_CLOSED = ("Ничего", "Пользовался(лась) бы ChatGPT", "Стал(а) бы искать аналог", "Другое")
EXIT_NEXT = ("Снова воспользуюсь АктКреватором", "Сначала попробую обычный ИИ",
             "Буду искать сам(а) или спрашивать других", "Зависит от задачи",
             "Скорее не воспользуюсь АктКреватором")


def exit_survey_of(sid):
    return get_db().execute("SELECT * FROM exit_surveys WHERE session_id = ?", (sid,)).fetchone()


def save_exit_survey(s):
    """Ответы проверяются по спискам; одна запись на сессию — повторная отправка её обновляет, а не дублирует.
    Возвращает (значения, ошибка). «Сохранено» показываем только после подтверждённой записи."""
    f = request.form
    useful = f.get("useful", type=int)
    closed = f.get("if_closed") if f.get("if_closed") in EXIT_CLOSED else None
    values = {"useful": useful if useful in range(1, 6) else None,
              "chatgpt_replace": f.get("chatgpt_replace") if f.get("chatgpt_replace") in EXIT_REPLACE else None,
              "if_closed": closed,
              "if_closed_other": ((f.get("if_closed_other") or "").strip()[:300] or None) if closed == "Другое"
              else None,
              "next_time": f.get("next_time") if f.get("next_time") in EXIT_NEXT else None,
              "barriers": (f.get("barriers") or "").strip()[:1000] or None}
    if not any(values.values()):
        return values, "Ответьте хотя бы на один вопрос — или просто пропустите оценку."
    existed = exit_survey_of(s["id"]) is not None
    stamp = now_iso()
    db = get_db()
    try:
        db.execute("INSERT INTO exit_surveys (session_id, user_id, useful, chatgpt_replace, if_closed, "
                   "if_closed_other, next_time, barriers, created_at, updated_at) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                   "ON CONFLICT(session_id) DO UPDATE SET useful = excluded.useful, "
                   "chatgpt_replace = excluded.chatgpt_replace, if_closed = excluded.if_closed, "
                   "if_closed_other = excluded.if_closed_other, next_time = excluded.next_time, "
                   "barriers = excluded.barriers, updated_at = excluded.updated_at",
                   (s["id"], s["user_id"], values["useful"], values["chatgpt_replace"], values["if_closed"],
                    values["if_closed_other"], values["next_time"], values["barriers"], stamp, stamp))
        db.commit()
    except sqlite3.Error:
        db.rollback()
        return values, "Не удалось сохранить ответы — попробуйте отправить ещё раз. Мы ничего не записали."
    log_event("exit_survey_saved", {"session": s["id"], "updated": existed, "useful": values["useful"],
                                    "chatgpt_replace": values["chatgpt_replace"], "if_closed": values["if_closed"],
                                    "next_time": values["next_time"], "has_text": bool(values["barriers"])})
    return values, None


@step_handler("done")
def done_step(s):
    c = ctx(s)
    db = get_db()
    incubation = db.execute("SELECT * FROM incubations WHERE session_id = ?", (s["id"],)).fetchone()
    survey_values, survey_error = None, None
    if request.method == "POST" and request.form.get("action") == "exit_survey":
        survey_values, survey_error = save_exit_survey(s)
        if not survey_error:
            # «Сохранено» — только когда запись подтвердило хранилище исследования (на бесплатном Render
            # база сервера временная, поэтому настоящее хранилище — внешняя таблица)
            if research.enabled() and not research.send_now([research.session_record(s["id"])]):
                flash("Спасибо — ответы приняты. В хранилище исследования они пока не записались: "
                      "отправим повторно автоматически.")
            else:
                flash("Спасибо — оценка сохранена.")
            return redirect(url_for("train.step", sid=s["id"], step="done") + "#exit")
    elif request.method == "POST":
        if request.form.get("action") != "incubate" or s["mode"] != "task":
            abort(405)
        if not incubation:
            db.execute("INSERT INTO incubations (session_id, due, created_at) VALUES (?, ?, ?)",
                       (s["id"], (now() + INCUBATION_AFTER).isoformat(timespec="seconds"), now_iso()))
            db.commit()
            log_event("incubation_requested", {"session": s["id"]})
            flash("Завтра в кабинете будет один вопрос об этой задаче. Напоминаний не будет.")
        return go(s["id"], "done")
    exp = db.execute("SELECT * FROM experiments WHERE session_id = ?", (s["id"],)).fetchone()
    survey = exit_survey_of(s["id"])
    return render_template("train/done.html", final=final_idea(s["id"]), route=loads(s["route_json"], {}),
                           ev=loads(s["eval_json"], {}), exp=exp, incubation=incubation,
                           can_incubate=s["mode"] == "task", survey=survey,
                           survey_values=survey_values or (dict(survey) if survey else {}), survey_error=survey_error,
                           exit_replace=EXIT_REPLACE, exit_closed=EXIT_CLOSED, exit_next=EXIT_NEXT, **c)
