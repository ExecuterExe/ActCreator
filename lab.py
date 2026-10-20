"""Эксперимент «обычный чат-бот против тренировки» (docs/product-value/EXPERIMENT_AB.md).

Участник приносит одну реальную задачу. До любой руки записывает задачу, ограничения и первые идеи. Затем проходит
две руки по очереди (порядок задаёт ведущий): A — обычный чат-бот той же модели с понятным запросом, B — полная
тренировка, C — сокращённая (сценарий «задача → механизм → один вызов → выбор → шаг»). После каждой руки — одинаковая
карточка решения; карточки уходят оценщикам вслепую (случайный код, без руки и порядка).

Включается только переменной ACTKREATOR_LAB=1 (или LAB в настройках приложения); иначе страниц нет (404).
"""

import csv
import io
import json
import os
import random
import re
import secrets
import time
import zipfile

from flask import Blueprint, abort, current_app, g, redirect, render_template, request, send_file, url_for

import admin
from common import ai, card_text, dumps, get_state, lib, loads, log_event, login_required, parse_time
from db import get_db, now_iso

bp = Blueprint("lab", __name__)

ARM_ORDERS = ("A,B", "B,A", "A,C", "C,A")
ARM_NAMES = {"A": "обычный чат-бот", "B": "полная тренировка", "C": "сокращённая тренировка"}
# Рука A: та же модель, что у тренировки, — различие в методе, а не в модели. Обычная роль помощника, без
# методики тренажёра; запрос человек видит и может поправить перед отправкой.
CHAT_SYSTEM = "Ты — полезный ассистент. Отвечай по-русски, по существу и конкретно."
CHAT_TEMPERATURE = 0.7
CHAT_MAX_TOKENS = 1200
CHAT_MAX_TURNS = 15
FIRST_PROMPT = ("Помоги решить рабочую задачу.\nЗадача: {text}\nЦель и срок: {goal}\nОграничения: {bans}\n"
                "Мои ресурсы: {card}\nМои первые идеи: {ideas}\n"
                "Предложи варианты, помоги выбрать один с учётом ограничений и сформулируй первый шаг, который можно "
                "сделать за 48 часов.")
SCALE5 = [str(n) for n in range(1, 6)]


def lab_enabled():
    flag = current_app.config.get("LAB")
    if flag is None:
        flag = os.environ.get("ACTKREATOR_LAB", "") == "1"
    return bool(flag)


@bp.before_request
def only_when_enabled():
    if not lab_enabled():
        abort(404)


def load_run(rid):
    run = get_db().execute("SELECT * FROM lab_runs WHERE id = ? AND user_id = ?", (rid, g.user["id"])).fetchone()
    if run is None:
        abort(404)
    return run


def arms_of(run):
    rows = get_db().execute("SELECT * FROM lab_arms WHERE run_id = ? ORDER BY position", (run["id"],)).fetchall()
    return {r["position"]: r for r in rows}


def update_run(rid, **fields):
    cols = ", ".join(f"{k} = ?" for k in fields)
    db = get_db()
    db.execute(f"UPDATE lab_runs SET {cols} WHERE id = ?", (*fields.values(), rid))
    db.commit()


def update_arm(aid, **fields):
    cols = ", ".join(f"{k} = ?" for k in fields)
    db = get_db()
    db.execute(f"UPDATE lab_arms SET {cols} WHERE id = ?", (*fields.values(), aid))
    db.commit()


# ---------- формы: одно описание полей — и для страницы, и для проверки ----------

def field(name, label, kind="text", options=None, required=True, hint=None):
    return {"name": name, "label": label, "kind": kind, "options": options or [], "required": required,
            "hint": hint}


NEW_FIELDS = [field("code", "Код участника (без имени и контактов)"),
              field("arms", "Порядок сессий", "radio", [f"{o} — {' → '.join(ARM_NAMES[a] for a in o.split(','))}"
                                                        for o in ARM_ORDERS])]
PRE_FIELDS = [
    field("occupation", "Чем вы сейчас занимаетесь?", "radio",
          ["Фрилансер / самозанятый", "Развиваю свой проект", "Работаю по найму", "Учусь", "Другое"]),
    field("chatbot_use", "Как часто вы пользуетесь ChatGPT или другим чат-ботом для рабочих задач?", "radio",
          ["Почти каждый день", "Несколько раз в неделю", "Реже", "Не пользуюсь"]),
    field("usual", "Как бы вы решали эту задачу без нас? Что сделали бы первым?", "textarea"),
    field("time_budget", "Сколько времени вы готовы потратить сегодня на поиск решения?", "radio",
          ["до 15 минут", "15–30 минут", "30–60 минут", "больше часа"]),
    field("good_result", "Что будет для вас хорошим результатом этой встречи?", "textarea"),
]
TASK_FIELDS = [
    field("text", "Задача: что нужно решить (1–3 предложения)", "textarea"),
    field("goal", "Цель и срок: что должно получиться и к какому сроку"),
    field("bans", "Чего нельзя или не хочется делать", required=False),
    field("in_domain", "Задача про то, чтобы привлечь клиентов, покупателей, учеников или пользователей?", "radio",
          ["Да", "Нет, про другое"]),
]
FIRST_FIELDS = [field(f"idea{i}", f"Идея {i}", required=(i == 1)) for i in range(1, 5)] + [
    field("favorite", "Какую из идей вы сделали бы скорее всего?", "radio", ["1", "2", "3", "4"]),
    field("why", "Почему она сработает? Одной фразой", required=False),
]
CARD_FIELDS = [
    field("solution", "Какое решение вы выбираете?", "textarea"),
    field("why", "Почему именно это, а не другие варианты?", "textarea"),
    field("constraints", "Как решение укладывается в ваши ограничения: деньги, время, навыки, запреты?", "textarea"),
    field("step", "Первый шаг, который вы сделаете в ближайшие 48 часов", "textarea"),
    field("success", "По какому сигналу поймёте, что сработало?"),
    field("fail", "По какому сигналу поймёте, что не сработало?"),
    field("confidence", "Насколько вы уверены, что сделаете этот шаг? 1 — точно нет, 5 — точно да", "radio", SCALE5),
    field("useful", "Насколько полезной была эта сессия? 1 — бесполезна, 5 — очень полезна", "radio", SCALE5),
]


def post_fields(cards):
    sol = [((c or {}).get("solution") or "")[:80] for c in cards]
    return [
        field("choice", "Какое решение вы будете реализовывать?", "radio",
              [f"Из сессии 1: {sol[0]}", f"Из сессии 2: {sol[1]}", "Оба", "Ни одно"]),
        field("better", "Какая сессия помогла больше?", "radio", ["Сессия 1", "Сессия 2", "Одинаково"]),
        field("again", "Какой способ вы бы выбрали для следующей похожей задачи?", "radio",
              ["Как в сессии 1", "Как в сессии 2", "Ни один из двух"]),
        field("new_way", "Узнали ли вы что-то новое о том, как вы обычно придумываете решения?", "radio",
              ["Да", "Скорее да", "Скорее нет", "Нет"]),
        field("return", "Записать вас на ещё одну тренировку по другой задаче в ближайшие 7 дней?", "radio",
              ["Да", "Нет"]),
        field("return_day", "Если да — в какой день удобно?", required=False),
        field("pay", f"Если бы тренировка была платной ({current_app.config.get('LAB_PRICE') or '490 ₽ в месяц'}), "
                     "что бы вы сделали?", "radio",
              ["Оформил(а) бы предзаказ сейчас", "Попробовал(а) бы ещё бесплатно, потом решил(а)",
               "Не стал(а) бы платить", "Не знаю"]),
        field("comment", "Что мешало или что улучшить в первую очередь?", "textarea", required=False),
    ]


def read_form(fields):
    values, errors = {}, {}
    for f in fields:
        v = (request.form.get(f["name"]) or "").strip()[:1500]
        if f["kind"] == "radio" and v and v not in f["options"]:
            v = ""
        values[f["name"]] = v
        if f["required"] and not v:
            errors[f["name"]] = "Ответьте, пожалуйста."
    return values, errors


def form_page(title, intro, fields, values, errors, action, button="Дальше", extra=None):
    return render_template("lab/form.html", title=title, intro=intro, fields=fields, values=values or {},
                           errors=errors, action=action, button=button, extra=extra)


# ---------- участник ----------

@bp.route("/lab/new", methods=["GET", "POST"])
@login_required
def new():
    values, errors = {}, {}
    if request.method == "POST":
        values, errors = read_form(NEW_FIELDS)
        if not errors:
            arms = values["arms"].split(" ")[0]
            db = get_db()
            cur = db.execute("INSERT INTO lab_runs (user_id, code, arms, created_at) VALUES (?, ?, ?, ?)",
                             (g.user["id"], values["code"][:40], arms, now_iso()))
            db.commit()
            log_event("lab_run_created", {"run": cur.lastrowid, "arms": arms})
            return redirect(url_for("lab.hub", rid=cur.lastrowid))
    return form_page("Новый участник эксперимента", "Заполняет ведущий перед началом. Порядок сессий чередуйте "
                     "между участниками.", NEW_FIELDS, values, errors, url_for("lab.new"), "Создать")


def next_stage(run):
    """Куда участнику дальше: вопросы → задача → первые идеи → сессия 1 → карточка 1 → сессия 2 → карточка 2 → итог."""
    if not run["pre_json"]:
        return "pre", None
    if not run["task_json"]:
        return "task", None
    if not run["first_json"]:
        return "first", None
    arms = arms_of(run)
    for pos, arm in enumerate(run["arms"].split(","), 1):
        row = arms.get(pos)
        if row is None or not row["work_done_at"]:
            return "arm", pos
        if not row["card_json"]:
            return "card", pos
    if not run["post_json"]:
        return "post", None
    return "done", None


@bp.get("/lab/<int:rid>")
@login_required
def hub(rid):
    run = load_run(rid)
    stage, pos = next_stage(run)
    arms = arms_of(run)
    if stage == "arm" and pos in arms and arms[pos]["session_id"]:
        s = get_db().execute("SELECT state, completed_at FROM sessions WHERE id = ?",
                             (arms[pos]["session_id"],)).fetchone()
        if s and s["state"] == "done":  # тренировка завершена — время работы в руке закончилось
            update_arm(arms[pos]["id"], work_done_at=s["completed_at"] or now_iso())
            return redirect(url_for("lab.hub", rid=rid))
    links = {"pre": url_for("lab.pre", rid=rid), "task": url_for("lab.task", rid=rid),
             "first": url_for("lab.first", rid=rid), "post": url_for("lab.post", rid=rid)}
    if pos:
        links["arm"] = url_for("lab.arm", rid=rid, pos=pos)
        links["card"] = url_for("lab.card", rid=rid, pos=pos)
    return render_template("lab/hub.html", run=run, stage=stage, pos=pos, link=links.get(stage),
                           n_arms=len(run["arms"].split(",")))


@bp.route("/lab/<int:rid>/pre", methods=["GET", "POST"])
@login_required
def pre(rid):
    run = load_run(rid)
    values, errors = loads(run["pre_json"], {}), {}
    if request.method == "POST":
        values, errors = read_form(PRE_FIELDS)
        if not errors:
            update_run(rid, pre_json=dumps(dict(values, at=now_iso())))
            return redirect(url_for("lab.hub", rid=rid))
    return form_page("Перед началом", "Несколько вопросов о вас и о том, как вы обычно решаете такие задачи. "
                     "Правильных ответов нет.", PRE_FIELDS, values, errors, url_for("lab.pre", rid=rid))


@bp.route("/lab/<int:rid>/task", methods=["GET", "POST"])
@login_required
def task(rid):
    run = load_run(rid)
    state = get_state(g.user["id"])
    if not state["card_json"]:  # ограничения — та же карточка ресурсов, что у тренировки, для обеих рук
        return redirect(url_for("train.card", next=url_for("lab.task", rid=rid)))
    values, errors = loads(run["task_json"], {}), {}
    if request.method == "POST":
        values, errors = read_form(TASK_FIELDS)
        if not errors and not 20 <= len(values["text"]) <= 600:
            errors["text"] = "Опишите задачу в 1–3 предложениях (от 20 до 600 символов)."
        if not errors and not 10 <= len(values["goal"]) <= 300:
            errors["goal"] = "Напишите, что должно получиться и к какому сроку."
        if not errors:
            data = {"text": values["text"], "goal": values["goal"], "bans": values["bans"][:300],
                    "in_domain": values["in_domain"] == "Да", "card": loads(state["card_json"], {}), "at": now_iso()}
            update_run(rid, task_json=dumps(data))
            return redirect(url_for("lab.hub", rid=rid))
    if values.get("in_domain") is not None and not isinstance(values.get("in_domain"), str):
        values = dict(values, in_domain="Да" if values["in_domain"] else "Нет, про другое")
    return form_page("Ваша задача", "Одна реальная задача — она будет одинаковой в обеих сессиях. Ресурсы и "
                     "ограничения берём из вашей карточки ресурсов.", TASK_FIELDS, values, errors,
                     url_for("lab.task", rid=rid))


@bp.route("/lab/<int:rid>/first", methods=["GET", "POST"])
@login_required
def first(rid):
    run = load_run(rid)
    values, errors = {}, {}
    if request.method == "POST":
        values, errors = read_form(FIRST_FIELDS)
        ideas = [values[f"idea{i}"] for i in range(1, 5) if values[f"idea{i}"]]
        fav = values.get("favorite")
        if ideas and len(ideas) == 1:
            errors.pop("favorite", None)
            fav = "1"
        elif ideas and fav and not values.get(f"idea{fav}"):
            errors["favorite"] = "Отметьте одну из записанных идей."
        if not errors:
            texts = [values[f"idea{i}"] for i in range(1, 5)]
            update_run(rid, first_json=dumps({"texts": texts, "favorite": int(fav), "why": values["why"],
                                              "at": now_iso()}))
            return redirect(url_for("lab.hub", rid=rid))
    return form_page("Первые идеи", "Запишите идеи, которые приходят в голову сразу, — около двух минут, коротко. "
                     "Они понадобятся в обеих сессиях.", FIRST_FIELDS, values, errors, url_for("lab.first", rid=rid))


def first_ideas(run):
    data = loads(run["first_json"], {})
    return [t for t in data.get("texts", []) if t], data


@bp.route("/lab/<int:rid>/arm/<int:pos>", methods=["GET", "POST"])
@login_required
def arm(rid, pos):
    """Начать руку: A — страница чата; B и C — тренировка с той же задачей, ограничениями и первыми идеями."""
    run = load_run(rid)
    order = run["arms"].split(",")
    if not 1 <= pos <= len(order) or next_stage(run) != ("arm", pos):
        return redirect(url_for("lab.hub", rid=rid))
    letter = order[pos - 1]
    row = arms_of(run).get(pos)
    db = get_db()
    if row is None:
        db.execute("INSERT INTO lab_arms (run_id, position, arm, started_at) VALUES (?, ?, ?, ?)",
                   (rid, pos, letter, now_iso()))
        db.commit()
        row = arms_of(run)[pos]
        log_event("lab_arm_started", {"run": rid, "position": pos, "arm": letter})
    if letter == "A":
        return redirect(url_for("lab.chat", rid=rid, pos=pos))
    if not row["session_id"]:
        sid = start_training(run, row, short=(letter == "C"))
        update_arm(row["id"], session_id=sid)
        return redirect(url_for("train.session_home", sid=sid))
    return redirect(url_for("train.session_home", sid=row["session_id"]))


def start_training(run, arm_row, short):
    """Сессия тренировки с задачей из эксперимента: человек видит её заполненной и нажимает «Дальше».
    Пробный период и оплата здесь не проверяются — это эксперимент."""
    import training
    td = loads(run["task_json"], {})
    task = {"text": td["text"], "goal": td["goal"], "bans": td.get("bans", ""), "in_domain": td.get("in_domain")}
    texts, data = first_ideas(run)
    flags = {"lab_run": run["id"], "lab_pos": arm_row["position"], "lab_first": data,
             "flow": "short" if short else "full"}  # рука задаёт путь явно, независимо от пути по умолчанию
    state = get_state(g.user["id"])
    ts = now_iso()
    db = get_db()
    cur = db.execute(
        "INSERT INTO sessions (user_id, mode, state, entry_source, level, methodology_version, task_json, card_json, "
        "flags_json, started_at, updated_at) VALUES (?, 'task', 'task', 'lab', ?, ?, ?, ?, ?, ?, ?)",
        (g.user["id"], state["level"], training.METHODOLOGY_VERSION, dumps(task), dumps(td.get("card") or {}),
         dumps(flags), ts, ts))
    db.commit()
    log_event("session_started", {"session": cur.lastrowid, "mode": "task", "source": "lab", "flow":
                                  "short" if short else "full"})
    return cur.lastrowid


@bp.route("/lab/<int:rid>/arm/<int:pos>/chat", methods=["GET", "POST"])
@login_required
def chat(rid, pos):
    run = load_run(rid)
    row = arms_of(run).get(pos)
    if row is None or row["arm"] != "A":
        return redirect(url_for("lab.hub", rid=rid))
    if row["work_done_at"]:
        return redirect(url_for("lab.card", rid=rid, pos=pos))
    messages = loads(row["chat_json"], [])
    error = None
    if request.method == "POST":
        action = request.form.get("action")
        if action == "finish" and any(m["role"] == "assistant" for m in messages):
            update_arm(row["id"], work_done_at=now_iso())
            log_event("lab_chat_finished", {"run": rid, "position": pos,
                                            "turns": sum(1 for m in messages if m["role"] == "user")})
            return redirect(url_for("lab.card", rid=rid, pos=pos))
        text = (request.form.get("message") or "").strip()[:4000]
        if action == "send" and text and sum(1 for m in messages if m["role"] == "user") < CHAT_MAX_TURNS:
            messages.append({"role": "user", "content": text, "at": now_iso()})
            history = [{"role": "system", "content": CHAT_SYSTEM}] + [
                {"role": m["role"], "content": m["content"]} for m in messages if m["role"] in ("user", "assistant")]
            client, start = ai().client, time.monotonic()
            try:
                answer = client.chat(history, max_tokens=CHAT_MAX_TOKENS, temperature=CHAT_TEMPERATURE)
                usage = dict(getattr(client, "last_usage", None) or {})
                messages.append({"role": "assistant", "content": answer, "at": now_iso(),
                                 "seconds": round(time.monotonic() - start, 1), "usage": usage})
            except Exception as exc:  # чат-бот не ответил: реплику человека сохраняем, ответ можно запросить снова
                messages.append({"role": "error", "content": type(exc).__name__, "at": now_iso(),
                                 "seconds": round(time.monotonic() - start, 1)})
                error = "Чат-бот не ответил. Отправьте сообщение ещё раз."
            update_arm(row["id"], chat_json=dumps(messages))
            if not error:
                return redirect(url_for("lab.chat", rid=rid, pos=pos))
    draft = ""
    if not any(m["role"] == "user" for m in messages):
        td = loads(run["task_json"], {})
        texts, _ = first_ideas(run)
        draft = FIRST_PROMPT.format(text=td.get("text", ""), goal=td.get("goal", ""), bans=td.get("bans") or "нет",
                                    card=card_text(td.get("card") or {}), ideas="; ".join(texts) or "нет")
    return render_template("lab/chat.html", run=run, pos=pos, messages=[m for m in messages if m["role"] != "error"],
                           draft=draft, error=error, ai_on=ai().available,
                           turns_left=CHAT_MAX_TURNS - sum(1 for m in messages if m["role"] == "user"))


@bp.route("/lab/<int:rid>/card/<int:pos>", methods=["GET", "POST"])
@login_required
def card(rid, pos):
    """Карточка решения — одинаковая для всех рук. Для B и C подставляем итог тренировки: человек правит сам."""
    run = load_run(rid)
    row = arms_of(run).get(pos)
    if row is None or not row["work_done_at"]:
        return redirect(url_for("lab.hub", rid=rid))
    values, errors = loads(row["card_json"], {}), {}
    if not values and row["session_id"]:
        values = card_from_session(row["session_id"])
    if request.method == "POST":
        values, errors = read_form(CARD_FIELDS)
        if not errors:
            code = unique_code()
            update_arm(row["id"], card_json=dumps(dict(values, at=now_iso())), card_code=code, card_at=now_iso())
            log_event("lab_card_saved", {"run": rid, "position": pos, "arm": row["arm"]})
            return redirect(url_for("lab.hub", rid=rid))
    return form_page(f"Карточка решения · сессия {pos}", "Коротко и своими словами. Карточку прочитает оценщик, "
                     "который не знает, как вы к ней пришли.", CARD_FIELDS, values, errors,
                     url_for("lab.card", rid=rid, pos=pos), "Сохранить карточку")


def card_from_session(sid):
    db = get_db()
    final = db.execute("SELECT text FROM ideas WHERE session_id = ? AND stage = 'final' ORDER BY id DESC LIMIT 1",
                       (sid,)).fetchone()
    exp = db.execute("SELECT plan, success_signal, fail_signal FROM experiments WHERE session_id = ?",
                     (sid,)).fetchone()
    ev = loads((db.execute("SELECT eval_json FROM sessions WHERE id = ?", (sid,)).fetchone() or {"eval_json": None})
               ["eval_json"], {})
    return {"solution": final["text"] if final else "", "why": ev.get("why") or "",
            "step": exp["plan"] if exp else "", "success": exp["success_signal"] if exp else "",
            "fail": exp["fail_signal"] if exp else ""}


def unique_code():
    db = get_db()
    while True:
        code = secrets.token_hex(3)
        if not db.execute("SELECT 1 FROM lab_arms WHERE card_code = ?", (code,)).fetchone():
            return code


@bp.route("/lab/<int:rid>/post", methods=["GET", "POST"])
@login_required
def post(rid):
    run = load_run(rid)
    if next_stage(run)[0] not in ("post", "done"):
        return redirect(url_for("lab.hub", rid=rid))
    arms = arms_of(run)
    fields = post_fields([loads(arms[p]["card_json"], {}) for p in sorted(arms)])
    values, errors = loads(run["post_json"], {}), {}
    if request.method == "POST":
        values, errors = read_form(fields)
        if not errors:
            update_run(rid, post_json=dumps(dict(values, at=now_iso())))
            log_event("lab_run_done", {"run": rid, "return": values["return"], "pay": values["pay"]})
            return redirect(url_for("lab.hub", rid=rid))
    return form_page("После обеих сессий", "Последние вопросы — о том, что вы будете делать дальше.", fields,
                     values, errors, url_for("lab.post", rid=rid), "Завершить")


# ---------- выгрузка для оценщиков и анализа (только исследователям) ----------

def _minutes(start, end):
    a, b = parse_time(start), parse_time(end)
    return round((b - a).total_seconds() / 60, 1) if a and b else None


def export_rows():
    db = get_db()
    runs = db.execute("SELECT * FROM lab_runs ORDER BY id").fetchall()
    cards, keys, run_rows = [], [], []
    labels = {f["id"]: f["label"] for f in lib().card_fields}
    for run in runs:
        td, fi = loads(run["task_json"], {}), loads(run["first_json"], {})
        texts = fi.get("texts") or []
        ideas = "; ".join(f"{t}{' (фаворит)' if i == fi.get('favorite') else ''}"
                          for i, t in enumerate(texts, 1) if t)
        resources = "; ".join(f"{labels.get(k, k)}: {', '.join(v) if isinstance(v, list) else v}"
                              for k, v in (td.get("card") or {}).items() if k in labels and v)
        pre, post_ = loads(run["pre_json"], {}), loads(run["post_json"], {})
        row = {"run": run["id"], "code": run["code"], "arms": run["arms"], "created_at": run["created_at"],
               **{f"pre_{k}": v for k, v in pre.items()}, **{f"post_{k}": v for k, v in post_.items()}}
        for arm in db.execute("SELECT * FROM lab_arms WHERE run_id = ? ORDER BY position", (run["id"],)).fetchall():
            card_ = loads(arm["card_json"], {})
            chat_ = loads(arm["chat_json"], [])
            p = f"s{arm['position']}_"
            row.update({p + "arm": arm["arm"], p + "work_min": _minutes(arm["started_at"], arm["work_done_at"]),
                        p + "card_min": _minutes(arm["work_done_at"], arm["card_at"]),
                        p + "session": arm["session_id"], p + "card_code": arm["card_code"],
                        p + "useful": card_.get("useful"), p + "confidence": card_.get("confidence"),
                        p + "chat_turns": sum(1 for m in chat_ if m["role"] == "user"),
                        p + "chat_model_sec": round(sum(m.get("seconds") or 0 for m in chat_
                                                        if m["role"] == "assistant"), 1),
                        p + "chat_tokens": sum((m.get("usage") or {}).get("total_tokens") or 0 for m in chat_)})
            if arm["card_code"]:
                cards.append({"card_code": arm["card_code"], "task": td.get("text"), "goal": td.get("goal"),
                              "bans": td.get("bans"), "resources": resources, "first_ideas": ideas,
                              **{f["name"]: card_.get(f["name"], "") for f in CARD_FIELDS[:6]}})
                keys.append({"card_code": arm["card_code"], "run": run["id"], "code": run["code"], "arm": arm["arm"],
                             "position": arm["position"], "session": arm["session_id"]})
        run_rows.append(row)
    random.Random(secrets.randbits(32)).shuffle(cards)  # порядок карточек не выдаёт ни участника, ни руку
    return cards, keys, run_rows


EVALUATOR_COLUMNS = ["card_code", "оценщик", "соответствие задаче и ограничениям (1–3)", "выполнимость (1–3)",
                     "механизм отличается от первых идей (да / нет / неясно)", "обоснованность выбора (1–3)",
                     "конкретный шаг за 48 часов (1–3)",
                     "по-вашему, карточка после: чат-бот / тренировка / не знаю", "комментарий"]  # проверка слепоты


def _csv(rows, columns=None):
    buf = io.StringIO()
    columns = columns or sorted({k for r in rows for k in r}, key=lambda k: (k not in ("run", "code", "arms"), k))
    w = csv.DictWriter(buf, fieldnames=columns, delimiter=";", extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v) for k, v in r.items()})
    return "﻿" + buf.getvalue()


@bp.get("/lab/export")
@login_required
def export():
    admin.require_admin()
    cards, keys, runs = export_rows()
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("cards_blind.csv", _csv(cards, ["card_code", "task", "goal", "bans", "resources", "first_ideas",
                                                   "solution", "why", "constraints", "step", "success", "fail"]))
        z.writestr("evaluator_sheet.csv", _csv([{"card_code": c["card_code"]} for c in cards], EVALUATOR_COLUMNS))
        z.writestr("cards_key.csv", _csv(keys))
        z.writestr("runs.csv", _csv(runs))
    out.seek(0)
    log_event("lab_exported", {"cards": len(cards), "runs": len(runs)})
    stamp = re.sub(r"\D", "", now_iso())[:12]
    return send_file(out, mimetype="application/zip", as_attachment=True, download_name=f"lab_{stamp}.zip")
