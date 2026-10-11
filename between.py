"""Между сессиями (B5): исходы и банк идей, контрольные точки (индекс первой мысли),
ручная смена уровня опор, итог месяца, кабинет.

Правило чистоты (B5 §1.4, §5): вопросы об исходе не зовут на новую тренировку;
в пробном периоде напоминаний о тренировках нет.
"""

import json
from collections import Counter
from datetime import timedelta

from flask import Blueprint, abort, flash, g, redirect, render_template, request, session, url_for

import engine
import scoring
from common import (access_status, dumps, get_state, lib, loads, log_event, login_required, now, parse_time,
                    update_state)
from db import get_db, now_iso

bp = Blueprint("between", __name__)

OUTCOME_DAYS = (2, 7, 30)
CP_SECONDS = 180          # контрольная точка: 3 минуты, как входной тест (B5 §3.7)
CP_GRACE = 15
CP_MIN_DAYS = 7           # не чаще раза в 7 дней
CP_MAX_DAYS = 14          # раз в 14 дней…
CP_SESSIONS = 5           # …или раз в 5 сессий — что раньше
GROUP_NEXT = {"Л": "Ф", "Ф": "П", "П": "Л"}
LEVEL_CHANGE_DAYS = 7     # ручная смена уровня — раз в неделю (B5 §2.4)

STATUS_GROUPS = [("в работе", "В работе"), ("к проверке", "К проверке"), ("проверено", "Проверено"),
                 ("сработало", "Сработало"), ("сработало частично", "Сработало частично"),
                 ("не сработало", "Не сработало"), ("отказались", "Отказались"), ("отложено", "Отложено"),
                 ("без проверки", "Без проверки")]
STATUS_NAMES = dict(STATUS_GROUPS)


def _options(ui_id, prefix=None):
    """Варианты ответа из строки LIB-13 вида «Вопрос? A · B · C. Можно не отвечать.»"""
    text = lib().t(ui_id)
    if prefix and text.startswith(prefix):
        text = text[len(prefix):]
    text = text.replace("Можно не отвечать.", "").replace("Можно не отвечать", "")
    return [o.strip().rstrip(".").strip() for o in text.split("·")]


def _safe_next(target):
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return url_for("me")


def outcome_options(day):
    return {2: _options("UI-129"), 7: _options("UI-131"), 30: _options("UI-134")}[day]


def refuse_reasons():
    return _options("UI-132", "Почему отказались?")


def status_after(day, answer):
    """Ответ об исходе → статус идеи в банке (B5 §1.5). None — статус не меняется."""
    o = outcome_options(day)
    if day == 2:
        return "проверено" if answer in o[1:4] else None
    if day == 7:
        return {o[0]: "в работе", o[1]: "отложено", o[2]: "отказались", o[3]: "сработало"}.get(answer)
    return {o[0]: "сработало", o[1]: "сработало частично", o[2]: "не сработало"}.get(answer)


# ---------- исходы (B5 §1.4) ----------

def _done_task_sessions(uid):
    return get_db().execute("SELECT * FROM sessions WHERE user_id = ? AND state = 'done' AND mode = 'task' "
                            "ORDER BY id", (uid,)).fetchall()


def _final(sid):
    return get_db().execute("SELECT * FROM ideas WHERE session_id = ? AND stage = 'final' ORDER BY id DESC LIMIT 1",
                            (sid,)).fetchone()


def due_outcomes(uid):
    """Все наступившие и неотвеченные вопросы об исходе, самые свежие первыми."""
    db = get_db()
    out = []
    for s in _done_task_sessions(uid):
        done_at = parse_time(s["completed_at"])
        final = _final(s["id"])
        if not done_at or not final or final["status"] == "удалено":
            continue
        answered = {r["day"] for r in db.execute("SELECT day FROM outcomes WHERE session_id = ?", (s["id"],))}
        exp = db.execute("SELECT * FROM experiments WHERE session_id = ?", (s["id"],)).fetchone()
        for day in OUTCOME_DAYS:
            if day in answered or now() < done_at + timedelta(days=day):
                continue
            if day == 2 and not exp:
                continue  # вопрос дня 2 — о проверке; без плана его нет
            if day == 2:
                question = lib().t("UI-128", план=exp["plan"])
            elif day == 7:
                question = lib().t("UI-130", идея=final["text"])
            else:
                question = lib().t("UI-133", идея=final["text"])
            out.append({"sid": s["id"], "day": day, "due": done_at + timedelta(days=day), "question": question,
                        "options": outcome_options(day), "idea": final["text"]})
    out.sort(key=lambda x: x["due"], reverse=True)
    return out


@bp.post("/outcome/<int:sid>/<int:day>")
@login_required
def outcome(sid, day):
    uid = g.user["id"]
    if day not in OUTCOME_DAYS:
        abort(404)
    item = next((o for o in due_outcomes(uid) if o["sid"] == sid and o["day"] == day), None)
    if item is None:
        abort(404)  # чужая сессия, срок не наступил или уже ответили
    answer = request.form.get("status")
    if answer not in item["options"]:
        flash("Выберите один из вариантов ответа.")
        return redirect(_safe_next(request.form.get("next")))
    text = (request.form.get("text") or "").strip()[:500]
    reason = request.form.get("reason")
    if day == 7 and answer == item["options"][2] and reason in refuse_reasons():
        text = f"{reason}. {text}".strip(". ") if text else reason
    db = get_db()
    db.execute("INSERT INTO outcomes (session_id, day, status, text, answered_at) VALUES (?, ?, ?, ?, ?)",
               (sid, day, answer, text or None, now_iso()))
    new_status = status_after(day, answer)
    final = _final(sid)
    if new_status and final:
        db.execute("UPDATE ideas SET status = ? WHERE id = ?", (new_status, final["id"]))
    db.commit()
    log_event("outcome_answered", {"session": sid, "day": day, "status": answer, "idea_status": new_status})
    session["after_outcome"] = True  # вход в тренировку после вопроса помечается outcome_link (B5 §1.4)
    flash("Ответ сохранён. Спасибо.")
    return redirect(_safe_next(request.form.get("next")))


# ---------- банк идей (B5 §1.5) ----------

@bp.get("/bank")
@login_required
def bank():
    uid = g.user["id"]
    db = get_db()
    finals = db.execute(
        "SELECT i.*, s.mode, s.task_json, s.completed_at FROM ideas i JOIN sessions s ON s.id = i.session_id "
        "WHERE s.user_id = ? AND i.stage = 'final' AND s.state = 'done' AND COALESCE(i.status, '') != 'удалено' "
        "ORDER BY i.id DESC", (uid,)).fetchall()
    groups = [(key, name, [i for i in finals if i["status"] == key]) for key, name in STATUS_GROUPS]
    other = [i for i in finals if i["status"] not in STATUS_NAMES]
    if other:
        groups.append(("другое", "Без статуса", other))
    drafts = db.execute(
        "SELECT i.* FROM ideas i JOIN sessions s ON s.id = i.session_id WHERE s.user_id = ? "
        "AND s.state = 'abandoned' AND i.status = 'черновик' ORDER BY i.id DESC", (uid,)).fetchall()
    extras = db.execute(
        "SELECT i.* FROM ideas i JOIN sessions s ON s.id = i.session_id WHERE s.user_id = ? "
        "AND i.stage = 'extra' AND COALESCE(i.status, '') != 'удалено' ORDER BY i.id DESC", (uid,)).fetchall()
    unanswered = {(o["sid"]) for o in due_outcomes(uid)}
    empty = not finals and not drafts and not extras
    return render_template("between/bank.html", groups=[x for x in groups if x[2]], drafts=drafts, extras=extras,
                           unanswered=unanswered, empty=empty, loads=loads)


def _own_idea(idea_id):
    row = get_db().execute("SELECT i.*, s.user_id, s.id AS sid, s.state AS s_state FROM ideas i "
                           "JOIN sessions s ON s.id = i.session_id WHERE i.id = ?", (idea_id,)).fetchone()
    if (row is None or row["user_id"] != g.user["id"] or row["status"] == "удалено"
            or row["stage"] not in ("final", "extra")):  # карточка — только у идей из банка
        abort(404)
    return row


@bp.route("/bank/<int:idea_id>", methods=["GET", "POST"])
@login_required
def idea_card(idea_id):
    idea = _own_idea(idea_id)
    db = get_db()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "status":
            status = request.form.get("status")
            if status not in STATUS_NAMES:
                abort(400)
            db.execute("UPDATE ideas SET status = ? WHERE id = ?", (status, idea_id))
            db.commit()
            log_event("idea_status_changed", {"idea": idea_id, "from": idea["status"], "to": status})
            flash("Статус изменён.")
            return redirect(url_for("between.idea_card", idea_id=idea_id))
        if action == "delete":
            # текст стираем, строку оставляем: на неё ссылаются оценка и исходы (без текста — это не данные человека)
            db.execute("UPDATE ideas SET text = '', status = 'удалено' WHERE id = ?", (idea_id,))
            db.commit()
            log_event("idea_deleted", {"idea": idea_id, "stage": idea["stage"]})
            flash("Идея удалена.")
            return redirect(url_for("between.bank"))
        abort(400)
    s = db.execute("SELECT * FROM sessions WHERE id = ?", (idea["sid"],)).fetchone()
    first = db.execute("SELECT * FROM ideas WHERE session_id = ? AND stage = 'first' ORDER BY ord",
                       (s["id"],)).fetchall()
    objections = db.execute("SELECT * FROM objections WHERE session_id = ? AND author = 'critic' ORDER BY id",
                            (s["id"],)).fetchall()
    exp = db.execute("SELECT * FROM experiments WHERE session_id = ?", (s["id"],)).fetchone()
    outcomes = db.execute("SELECT * FROM outcomes WHERE session_id = ? ORDER BY day", (s["id"],)).fetchall()
    pending = [o for o in due_outcomes(g.user["id"]) if o["sid"] == s["id"]]
    return render_template("between/idea.html", idea=idea, s=s, task=loads(s["task_json"], {}),
                           tool=loads(s["tool_json"], {}), first=first, objections=objections, exp=exp,
                           outcomes=outcomes, pending=pending, statuses=STATUS_GROUPS,
                           refuse=refuse_reasons())


# ---------- контрольные точки: индекс первой мысли (B5 §3, A4 §2.8) ----------

def checkpoints_done(uid):
    return get_db().execute("SELECT * FROM checkpoints WHERE user_id = ? AND completed_at IS NOT NULL ORDER BY id",
                            (uid,)).fetchall()


def open_checkpoint(uid):
    return get_db().execute("SELECT * FROM checkpoints WHERE user_id = ? AND completed_at IS NULL ORDER BY id DESC "
                            "LIMIT 1", (uid,)).fetchone()


def entry_attempt(uid):
    return get_db().execute("SELECT * FROM attempts WHERE user_id = ? AND step = 'done'", (uid,)).fetchone()


def next_checkpoint_task(uid):
    """По возрастанию номера, без повторов, группы чередуются Л → Ф → П (LIB-12)."""
    done = checkpoints_done(uid)
    done_ids = [c["task_id"] for c in done]
    by_id = {t["id"]: t for t in lib().standard_tasks}
    pool = [t for t in lib().standard_tasks if t["id"] != "S00"]
    fresh = [t for t in pool if t["id"] not in done_ids]
    if fresh:
        last_group = by_id[done_ids[-1]]["group"] if done_ids and done_ids[-1] in by_id else None
        want = GROUP_NEXT.get(last_group, "Л")
        return next((t for t in fresh if t["group"] == want), fresh[0]), False
    return by_id[done_ids[0]], True  # задачи кончились — повтор самой давней (B5 §3.11)


def _postpones(uid, since):
    rows = get_db().execute("SELECT props_json, at FROM events WHERE user_id = ? AND name = 'checkpoint_postponed' "
                            "ORDER BY id", (uid,)).fetchall()
    return [r for r in rows if not since or (parse_time(r["at"]) and parse_time(r["at"]) > since)]


def checkpoint_status(uid):
    """{'due': bool, 'base': дата отсчёта, 'next_allowed': дата, 'postpones': n, 'postponed_until': дата}"""
    done = checkpoints_done(uid)
    attempt = entry_attempt(uid)
    sessions = get_db().execute("SELECT completed_at FROM sessions WHERE user_id = ? AND state = 'done' "
                                "ORDER BY id", (uid,)).fetchall()
    if done:
        base = parse_time(done[-1]["completed_at"])
    elif attempt:
        base = parse_time(attempt["completed_at"])
    elif sessions:
        base = parse_time(sessions[0]["completed_at"])
    else:
        return {"due": False, "base": None, "next_allowed": None, "postpones": 0, "postponed_until": None}
    since = [s for s in sessions if parse_time(s["completed_at"]) and parse_time(s["completed_at"]) > base]
    days = (now() - base).total_seconds() / 86400
    postpones = _postpones(uid, base)
    until = None
    if postpones:
        until = parse_time(postpones[-1]["at"]) + timedelta(days=CP_MIN_DAYS)
    ready = days >= CP_MIN_DAYS and len(since) >= 1 and (len(since) >= CP_SESSIONS or days >= CP_MAX_DAYS)
    due = ready and not (until and now() < until)
    return {"due": due, "base": base, "next_allowed": base + timedelta(days=CP_MIN_DAYS),
            "postpones": len(postpones), "postponed_until": until if until and now() < until else None}


def checkpoint_common(task_id, exclude_id=None):
    """Облако частого: стартовая оценка (seed LIB-01) или данные 30+ участников этой задачи."""
    rows = get_db().execute("SELECT id, result_json FROM checkpoints WHERE task_id = ? AND completed_at IS NOT NULL",
                            (task_id,)).fetchall()
    fams = [set(loads(r["result_json"], {}).get("families", [])) for r in rows if r["id"] != exclude_id]
    n = len(fams)
    shares = {}
    if n:
        counts = Counter(f for s in fams for f in s)
        shares = {f: c / n for f, c in counts.items()}
    common, source = engine.common_families(lib(), [], shares, n)
    return set(common), source, n


def entry_point(uid):
    """Базовая точка — входной тест: число редких направлений (другая задача и разметка — с пометкой)."""
    a = entry_attempt(uid)
    if not a or not a["result_json"]:
        return None
    r = loads(a["result_json"], {})
    rare = {i["family"] for i in r.get("ideas", []) if i.get("family") and not i.get("common")}
    return {"label": "Входной тест", "date": parse_time(a["completed_at"]), "ipm": len(rare), "base": True,
            "n_ideas": r.get("n_ideas"), "families": r.get("n_families")}


def ipm_points(uid):
    points = []
    base = entry_point(uid)
    if base:
        points.append(base)
    by_id = {t["id"]: t for t in lib().standard_tasks}
    for c in checkpoints_done(uid):
        r = loads(c["result_json"], {})
        points.append({"label": f"Контрольная точка ({c['task_id']})", "date": parse_time(c["completed_at"]),
                       "ipm": r.get("metrics", {}).get("rare_families", 0), "base": False,
                       "n_ideas": r.get("metrics", {}).get("n_ideas"), "families": r.get("metrics", {}).get("flexibility"),
                       "repeat": r.get("repeat"), "group": by_id.get(c["task_id"], {}).get("group")})
    return points


def trend_text(points):
    t = engine.trend([p["ipm"] for p in points])
    return {"few": lib().t("UI-149"), "up": lib().t("UI-150"), "flat": lib().t("UI-151")}[t], t


def _checkpoint_access(uid):
    a = access_status(uid)
    if not (a["full"] or a["paid"]):
        log_event("paywall_shown", {"reason": "checkpoint"})
        return redirect(url_for("train.paywall"))
    return None


@bp.get("/checkpoint")
@login_required
def checkpoint_intro():
    uid = g.user["id"]
    blocked = _checkpoint_access(uid)
    if blocked:
        return blocked
    cp = open_checkpoint(uid)
    if cp:
        return redirect(url_for("between.checkpoint_task", cid=cp["id"]))
    st = checkpoint_status(uid)
    too_early = bool(st["next_allowed"] and now() < st["next_allowed"])
    return render_template("between/checkpoint_intro.html", st=st, too_early=too_early,
                           points=ipm_points(uid), seconds=CP_SECONDS)


@bp.post("/checkpoint/start")
@login_required
def checkpoint_start():
    uid = g.user["id"]
    blocked = _checkpoint_access(uid)
    if blocked:
        return blocked
    cp = open_checkpoint(uid)
    if cp:
        return redirect(url_for("between.checkpoint_task", cid=cp["id"]))
    st = checkpoint_status(uid)
    if st["next_allowed"] and now() < st["next_allowed"]:
        flash("Контрольная точка — не чаще раза в 7 дней.")
        return redirect(url_for("between.checkpoint_intro"))
    task, repeat = next_checkpoint_task(uid)
    db = get_db()
    cur = db.execute("INSERT INTO checkpoints (user_id, task_id, started_at, result_json) VALUES (?, ?, ?, ?)",
                     (uid, task["id"], now_iso(), dumps({"repeat": repeat})))
    db.commit()
    log_event("checkpoint_started", {"checkpoint": cur.lastrowid, "task": task["id"], "repeat": repeat,
                                     "due": st["due"], "source": request.form.get("src") or "self"})
    return redirect(url_for("between.checkpoint_task", cid=cur.lastrowid))


@bp.post("/checkpoint/postpone")
@login_required
def checkpoint_postpone():
    uid = g.user["id"]
    st = checkpoint_status(uid)
    log_event("checkpoint_postponed", {"n": st["postpones"] + 1})
    if st["postpones"] + 1 >= 2:
        flash(lib().t("UI-147"))
    return redirect(url_for("me"))


def _own_checkpoint(cid):
    cp = get_db().execute("SELECT * FROM checkpoints WHERE id = ?", (cid,)).fetchone()
    if cp is None or cp["user_id"] != g.user["id"]:
        abort(404)
    return cp


def _task(task_id):
    return next(t for t in lib().standard_tasks if t["id"] == task_id)


@bp.route("/checkpoint/<int:cid>/task", methods=["GET", "POST"])
@login_required
def checkpoint_task(cid):
    cp = _own_checkpoint(cid)
    if cp["completed_at"]:
        return redirect(url_for("between.checkpoint_result", cid=cid))
    if cp["ideas_json"]:
        return redirect(url_for("between.checkpoint_pick", cid=cid))
    db = get_db()
    if not cp["task_started_at"]:
        db.execute("UPDATE checkpoints SET task_started_at = ? WHERE id = ?", (now_iso(), cid))
        db.commit()
        cp = _own_checkpoint(cid)
    elapsed = (now() - parse_time(cp["task_started_at"])).total_seconds()
    error = None
    if request.method == "POST":
        ideas = scoring.parse_ideas(request.form.get("ideas", ""))
        if not ideas and elapsed < CP_SECONDS:
            error = "Напишите хотя бы одну идею — по одной на строку."
        else:
            late = elapsed > CP_SECONDS + CP_GRACE
            result = loads(cp["result_json"], {})
            result.update({"seconds": round(min(elapsed, CP_SECONDS + CP_GRACE), 1), "late": late})
            db.execute("UPDATE checkpoints SET ideas_json = ?, result_json = ? WHERE id = ?",
                       (dumps([{"text": t} for t in ideas]), dumps(result), cid))
            db.commit()
            return redirect(url_for("between.checkpoint_pick", cid=cid))
    task = _task(cp["task_id"])
    instruction = scoring.load_task(scoring.default_task_path()).get("instruction")
    return render_template("between/checkpoint_task.html", cp=cp, task=task, instruction=instruction,
                           remaining=max(0, int(CP_SECONDS - elapsed)), seconds=CP_SECONDS, error=error)


@bp.route("/checkpoint/<int:cid>/pick", methods=["GET", "POST"])
@login_required
def checkpoint_pick(cid):
    cp = _own_checkpoint(cid)
    if cp["completed_at"]:
        return redirect(url_for("between.checkpoint_result", cid=cid))
    if not cp["ideas_json"]:
        return redirect(url_for("between.checkpoint_task", cid=cid))
    ideas = loads(cp["ideas_json"], [])
    need = min(2, len(ideas))
    error = None
    if request.method == "POST" or need < 2:
        picked = sorted({int(v) for v in request.form.getlist("pick") if v.isdigit() and int(v) < len(ideas)})
        if need < 2:
            picked = list(range(need))
        if len(picked) == need:
            finish_checkpoint(cp, ideas, picked)
            return redirect(url_for("between.checkpoint_result", cid=cid))
        error = "Отметьте ровно две идеи."
    return render_template("between/checkpoint_pick.html", ideas=ideas, error=error)


def finish_checkpoint(cp, ideas, picked):
    """Классификация только словарём LIB-02 — без ИИ, чтобы замер был одинаковым во времени (B5 §3.8)."""
    classified = []
    for i in ideas:
        r = engine.classify_by_dictionary(lib(), i["text"])
        classified.append({"text": i["text"], "family": r["family"][:3] if r and r["family"] else None})
    common, source, n = checkpoint_common(cp["task_id"], exclude_id=cp["id"])
    metrics = engine.checkpoint_metrics(classified, common)
    top2 = [classified[i] for i in picked]
    result = loads(cp["result_json"], {})
    result.update({"metrics": metrics, "ideas": [dict(c, common=bool(c["family"] and c["family"] in common))
                                                 for c in classified],
                   "families": sorted({c["family"] for c in classified if c["family"]}),
                   "common_source": source, "n_basis": n,
                   "top2_rare": sum(1 for c in top2 if c["family"] and c["family"] not in common)})
    db = get_db()
    db.execute("UPDATE checkpoints SET top2_json = ?, result_json = ?, completed_at = ? WHERE id = ?",
               (dumps(picked), dumps(result), now_iso(), cp["id"]))
    db.commit()
    log_event("checkpoint_done", {"checkpoint": cp["id"], "task": cp["task_id"], "ipm": metrics["rare_families"],
                                  "n": metrics["n_ideas"], "late": result.get("late"), "repeat": result.get("repeat")})


@bp.get("/checkpoint/<int:cid>")
@login_required
def checkpoint_result(cid):
    cp = _own_checkpoint(cid)
    if not cp["completed_at"]:
        return redirect(url_for("between.checkpoint_task", cid=cid))
    r = loads(cp["result_json"], {})
    m = r["metrics"]
    points = ipm_points(g.user["id"])
    comment, kind = trend_text(points)
    return render_template("between/checkpoint_result.html", r=r, m=m, points=points, comment=comment,
                           summary=lib().t("UI-148", N=m["n_ideas"], K=m["flexibility"], R=m["rare_families"]),
                           task=_task(cp["task_id"]))


# ---------- уровень опор вручную (B5 §2.4) ----------

@bp.post("/level")
@login_required
def level_change():
    uid = g.user["id"]
    direction = request.form.get("direction")
    if direction not in ("up", "down"):
        abort(400)
    state = get_state(uid)
    db = get_db()
    last = db.execute("SELECT at FROM scaffold_changes WHERE user_id = ? AND reason LIKE 'user_%' "
                      "ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
    if last and now() - parse_time(last["at"]) < timedelta(days=LEVEL_CHANGE_DAYS):
        flash("Уровень можно менять вручную раз в неделю.")
        return redirect(url_for("me"))
    new = state["level"] + (1 if direction == "up" else -1)
    if not 1 <= new <= 3:
        flash("Такого уровня нет.")
        return redirect(url_for("me"))
    update_state(uid, level=new)
    db.execute("INSERT INTO scaffold_changes (user_id, from_level, to_level, reason, at) VALUES (?, ?, ?, ?, ?)",
               (uid, state["level"], new, f"user_{direction}", now_iso()))
    db.commit()
    log_event("scaffold_changed", {"from": state["level"], "to": new, "reason": f"user_{direction}"})
    flash(f"Уровень самостоятельности: {new} из 3.")
    return redirect(url_for("me"))


# ---------- итог месяца (B5 §7) ----------

def month_summary(uid, days=30):
    db = get_db()
    since = now() - timedelta(days=days)
    sessions = [s for s in db.execute("SELECT * FROM sessions WHERE user_id = ? AND state = 'done' ORDER BY id",
                                      (uid,)).fetchall()
                if parse_time(s["completed_at"]) and parse_time(s["completed_at"]) >= since]
    n = len(sessions)
    evs = [loads(s["eval_json"], {}) for s in sessions]
    k = sum(1 for e in evs if e.get("shift"))
    own = sum(1 for s in sessions if s["mode"] == "task")
    fam_counter = Counter()
    for s in sessions:
        for i in db.execute("SELECT family FROM ideas WHERE session_id = ? AND stage = 'first' AND family IS NOT NULL",
                            (s["id"],)):
            if i["family"] and i["family"][:1] == "F":
                fam_counter[i["family"][:3]] += 1
    top = [(lib().family_name(f), c) for f, c in fam_counter.most_common(3)]
    tool_shift = Counter()
    assumptions = Counter()
    by_id = {a["id"]: a["text"] for a in lib().assumptions}
    for s, e in zip(sessions, evs):
        tool = loads(s["tool_json"], {})
        if e.get("shift") and tool.get("type"):
            tool_shift[engine.TOOL_NAMES[tool["type"]]] += 1
        if tool.get("type") == "assumptions":
            for aid in tool.get("about_me", []):
                if aid in by_id:
                    assumptions[by_id[aid]] += 1
            chosen = tool.get("chosen") or {}
            if chosen.get("text"):
                assumptions[chosen["text"]] += 1
    reached = []
    for s in sessions:
        f = _final(s["id"])
        if f and f["status"] in ("в работе", "проверено", "сработало", "сработало частично"):
            reached.append(f)
    changes = [c for c in db.execute("SELECT * FROM scaffold_changes WHERE user_id = ? ORDER BY id", (uid,))
               if parse_time(c["at"]) and parse_time(c["at"]) >= since]
    points = ipm_points(uid)
    line = lib().t("UI-165", n=n, k=k,
                   **{"топ-3": ", ".join(f"«{name}»" for name, _ in top) or "—",
                      "инструменты": ", ".join(t for t, _ in tool_shift.most_common(2)) or "—",
                      "идеи": str(len(reached))})
    return {"n": n, "k": k, "own": own, "practice": n - own, "top": top, "tool_shift": tool_shift.most_common(),
            "assumptions": assumptions.most_common(3), "reached": reached, "changes": changes,
            "level": get_state(uid)["level"], "points": points, "trend": trend_text(points)[0] if points else None,
            "line": line, "short": n < 3}


@bp.route("/summary", methods=["GET", "POST"])
@login_required
def summary():
    uid = g.user["id"]
    if request.method == "POST":
        action = request.form.get("action")
        if action == "useful":
            text = (request.form.get("text") or "").strip()[:1000]
            if text:
                log_event("summary_feedback", {"text": text})
            flash("Спасибо.")
        elif action == "stop":
            reason = request.form.get("reason")
            options = _options("UI-164", "Почему решили не продолжать?")
            log_event("churned", {"reason": reason if reason in options else None})
            flash("Ваши идеи и результаты остаются с вами.")
        else:
            abort(400)
        return redirect(url_for("between.summary"))
    log_event("summary_opened", {})
    stop_options = _options("UI-164", "Почему решили не продолжать?")
    return render_template("between/summary.html", m=month_summary(uid), stop_options=stop_options)


# ---------- кабинет ----------

def me_context():
    uid = g.user["id"]
    state = get_state(uid)
    db = get_db()
    n_done = db.execute("SELECT COUNT(*) FROM sessions WHERE user_id = ? AND state = 'done'", (uid,)).fetchone()[0]
    open_session = db.execute("SELECT id, state FROM sessions WHERE user_id = ? AND state NOT IN ('done','abandoned') "
                              "ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
    questions = due_outcomes(uid)
    return {
        "state": state, "access": access_status(uid), "n_done": n_done, "open_session": open_session,
        "questions": questions[:3], "n_questions": len(questions),
        "cp": checkpoint_status(uid) if n_done else None, "open_cp": open_checkpoint(uid),
        "has_card": bool(state["card_json"]), "refuse": refuse_reasons(),
    }
