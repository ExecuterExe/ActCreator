"""Общие помощники для страниц тренировочного контура."""

import json
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import current_app, g, redirect, request, url_for

from db import get_db, now_iso

METHODOLOGY_VERSION = "v0.4"
TRIAL_SESSIONS = 2
TRIAL_DAYS = 7


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def lib():
    return current_app.extensions["akt_lib"]


def ai():
    return current_app.extensions["akt_ai"]


def loads(text, default):
    if not text:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def dumps(value):
    return json.dumps(value, ensure_ascii=False)


def now():
    return datetime.now(timezone.utc)


def parse_time(value):
    return datetime.fromisoformat(value) if value else None


def log_event(name, props=None, user_id=None):
    if user_id is None:
        user = getattr(g, "user", None)
        user_id = user["id"] if user else None
    db = get_db()
    db.execute("INSERT INTO events (user_id, name, props_json, at) VALUES (?, ?, ?, ?)",
               (user_id, name, dumps(props or {}), now_iso()))
    db.commit()


def get_state(user_id):
    db = get_db()
    row = db.execute("SELECT * FROM user_state WHERE user_id = ?", (user_id,)).fetchone()
    if row is None:
        db.execute("INSERT INTO user_state (user_id) VALUES (?)", (user_id,))
        db.commit()
        row = db.execute("SELECT * FROM user_state WHERE user_id = ?", (user_id,)).fetchone()
    return row


def update_state(user_id, **fields):
    get_state(user_id)
    cols = ", ".join(f"{k} = ?" for k in fields)
    db = get_db()
    db.execute(f"UPDATE user_state SET {cols} WHERE user_id = ?", (*fields.values(), user_id))
    db.commit()


def mark_shown(user_id, lib_name, item_id, session_id=None):
    db = get_db()
    db.execute("INSERT INTO shown_items (user_id, lib, item_id, session_id, at) VALUES (?, ?, ?, ?, ?)",
               (user_id, lib_name, item_id, session_id, now_iso()))
    db.commit()


def recently_shown(user_id, lib_name, last_sessions=10):
    """ID элементов, показанных пользователю за последние N сессий (неповторение, A2 §2.7)."""
    db = get_db()
    rows = db.execute(
        "SELECT item_id FROM shown_items WHERE user_id = ? AND lib = ? AND "
        "(session_id IS NULL OR session_id IN (SELECT id FROM sessions WHERE user_id = ? ORDER BY id DESC LIMIT ?))",
        (user_id, lib_name, user_id, last_sessions)).fetchall()
    return {r["item_id"] for r in rows}


def access_status(user_id):
    """Пробный период: 2 тренировки за 7 дней от первой (B5, §6)."""
    state = get_state(user_id)
    full = current_app.config.get("FULL_ACCESS", False)
    paid_until = parse_time(state["paid_until"])
    paid = bool(paid_until and paid_until > now())
    db = get_db()
    used = db.execute("SELECT COUNT(*) FROM sessions WHERE user_id = ? AND mode IN ('task','practice')",
                      (user_id,)).fetchone()[0]
    started = parse_time(state["trial_started_at"])
    until = started + timedelta(days=TRIAL_DAYS) if started else None
    in_trial = used < TRIAL_SESSIONS and (until is None or now() < until)
    return {"allowed": full or paid or in_trial, "full": full, "paid": paid, "used": used,
            "left": max(0, TRIAL_SESSIONS - used), "until": until}


def card_text(card, with_ids=False):
    """Карточка одной строкой; with_ids — с кодами полей (критику нужно ссылаться на поле карточки)."""
    labels = {f["id"]: f["label"] for f in lib().card_fields}
    parts = []
    for key, value in card.items():
        if key not in labels or not value:
            continue
        if isinstance(value, list):
            value = ", ".join(value)
        parts.append(f"{key + ' ' if with_ids else ''}{labels[key]}: {value}")
    return "; ".join(parts) or "не заполнена"
