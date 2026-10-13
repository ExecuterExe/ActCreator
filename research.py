"""Данные исследования → внешнее хранилище (Google Sheets), пока диск сервера временный.

Как устроено:
- каждый значимый шаг (анкета и задача входного теста, шаги тренировки, итоговая оценка) кладёт строку
  в очередь research_outbox — в той же транзакции, что и сам шаг, поэтому ничего не теряется до отправки;
- фоновый поток сразу отправляет очередь в таблицу; неудача — строка остаётся в очереди, следующая попытка —
  при следующем шаге любого участника или кнопкой на /admin;
- строки те же, что в CSV (export_csv.attempt_row / session_row): одна строка — одна попытка или сессия;
  повторная отправка той же записи обновляет её строку в таблице на месте, а не добавляет копию;
- номера участников и сессий — с префиксом базы (meta.db_uid): после стирания диска номера начнутся
  с 1 заново, и «u1» до и после не сольются в одного человека.
Без настроек Google модуль молчит: данные остаются только в базе сервера.
"""

import hashlib
import json
import os
import sqlite3
import threading

from flask import current_app

import export_csv
import sheets
from db import get_db, now_iso

TAB_ATTEMPTS, TAB_SESSIONS, TAB_IDEAS, TAB_CHECK = "Входной тест", "Тренировки", "Идеи", "Проверка"
KEY_COLUMNS = ["record_id", "db", "updated_at"]
IDEA_FIELDS = ["participant", "session", "task", "goal", "source", "stage", "ord", "version", "text", "family",
               "is_favorite", "created_at"]
HEADERS = {
    TAB_ATTEMPTS: KEY_COLUMNS + export_csv.ATTEMPT_FIELDS,
    TAB_SESSIONS: KEY_COLUMNS + export_csv.SESSION_FIELDS,
    TAB_IDEAS: KEY_COLUMNS + IDEA_FIELDS,
    TAB_CHECK: ["record_id", "db", "checked_at", "note"],
}
_lock = threading.Lock()


# ---------- настройка ----------

def client():
    """Клиент таблицы или None. В тестах — подставной (config SHEETS_CLIENT), на сервере — по переменным."""
    app = current_app
    if "SHEETS_CLIENT" in app.config:
        return app.config["SHEETS_CLIENT"]
    if "akt_sheets" not in app.extensions:
        script_url = os.environ.get("APPS_SCRIPT_URL", "").strip()
        script_token = os.environ.get("APPS_SCRIPT_TOKEN", "").strip()
        sheet_id = os.environ.get("GOOGLE_SHEETS_ID", "").strip()
        key = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
        made, error = None, None
        if script_url or script_token:  # основной способ — Apps Script в самой таблице, без Google Cloud
            if script_url.startswith("https://script.google.com/") and script_token:
                made = sheets.AppsScriptClient(script_url, script_token)
            else:
                error = "нужны обе переменные: APPS_SCRIPT_URL (https://script.google.com/…/exec) и APPS_SCRIPT_TOKEN"
        elif sheet_id and key.strip():
            try:
                made = sheets.SheetsClient(sheet_id, sheets.load_key(key))
            except (ValueError, KeyError) as e:  # кривой ключ — видно на /admin, сайт работает дальше
                error = f"ключ сервисного аккаунта не читается: {type(e).__name__}"
        app.extensions["akt_sheets"] = made
        app.extensions["akt_sheets_error"] = error
    return app.extensions["akt_sheets"]


def enabled():
    return client() is not None


def with_texts():
    return os.environ.get("GOOGLE_SHEETS_TEXTS", "1").strip() != "0"


def db_uid(db=None):
    db = db or get_db()
    row = db.execute("SELECT value FROM meta WHERE key = 'db_uid'").fetchone()
    return row[0] if row else "db"


# ---------- очередь ----------

def _enqueue(db, tab, record_id, row):
    payload = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()
    old = db.execute("SELECT row_hash FROM research_outbox WHERE record_id = ?", (record_id,)).fetchone()
    if old and old[0] == digest:
        return  # то же содержимое уже в очереди или отправлено — дубликатов не плодим
    db.execute("INSERT INTO research_outbox (record_id, tab, row_json, row_hash, updated_at) VALUES (?, ?, ?, ?, ?) "
               "ON CONFLICT(record_id) DO UPDATE SET row_json = excluded.row_json, row_hash = excluded.row_hash, "
               "updated_at = excluded.updated_at", (record_id, tab, payload, digest, now_iso()))


def _ids(uid, row):
    row = dict(row)
    if row.get("participant") is not None:
        row["participant"] = f"{uid}-u{row['participant']}"
    if row.get("session") is not None:
        row["session"] = f"{uid}-s{row['session']}"
    return row


def queue_attempt(attempt_id):
    if not enabled():
        return
    db = get_db()
    r = db.execute("SELECT * FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
    if r:
        uid = db_uid(db)
        _enqueue(db, TAB_ATTEMPTS, f"{uid}:attempt:{r['id']}", _ids(uid, export_csv.attempt_row(r)))
        db.commit()
    kick()


def queue_session(sid):
    if not enabled():
        return
    db = get_db()
    s = db.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
    if not s:
        return
    uid = db_uid(db)
    _enqueue(db, TAB_SESSIONS, f"{uid}:session:{sid}", _ids(uid, export_csv.session_row(db, s)))
    if with_texts():
        task = json.loads(s["task_json"] or "{}")
        common = {"participant": s["user_id"], "session": sid, "task": task.get("text"), "goal": task.get("goal")}
        for i in db.execute("SELECT * FROM ideas WHERE session_id = ? AND stage != 'draft' ORDER BY id", (sid,)):
            _enqueue(db, TAB_IDEAS, f"{uid}:idea:{i['id']}", _ids(uid, dict(
                common, source="участник", stage=i["stage"], ord=i["ord"], version=i["version"], text=i["text"],
                family=i["family"], is_favorite=i["is_favorite"], created_at=i["created_at"])))
        for k, b in enumerate((json.loads(s["mirror_json"] or "{}") or {}).get("baseline") or [], 1):
            if isinstance(b, dict) and b.get("text"):
                _enqueue(db, TAB_IDEAS, f"{uid}:baseline:{sid}:{k}", _ids(uid, dict(
                    common, source="чат-бот", stage="baseline", ord=k, text=b["text"], family=b.get("family"))))
    db.commit()
    kick()


def on_event(name, props):
    """Из журнала событий: любой шаг тренировки обновляет строку сессии (и её идей) в очереди."""
    sid = (props or {}).get("session")
    if isinstance(sid, int):
        queue_session(sid)


# ---------- отправка ----------

def flush(db_path, sheet, only=None, limit=200):
    """Отправить очередь. Своё соединение с базой — работает и из фонового потока без контекста Flask.
    Возвращает (отправлено, ошибок, последняя ошибка)."""
    with _lock:
        conn = sqlite3.connect(db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        sent = failed = 0
        last_error = None
        try:
            uid = db_uid(conn)
            rows = conn.execute("SELECT * FROM research_outbox WHERE sent_hash IS NULL OR sent_hash != row_hash "
                                "ORDER BY updated_at LIMIT ?", (limit,)).fetchall()
            if only is not None:
                rows = [r for r in rows if r["record_id"] in only]
            if hasattr(sheet, "upsert_many"):  # Apps Script: одна пачка, строку по record_id находит сама таблица
                return _flush_batch(conn, sheet, uid, rows)
            ready = set()
            for r in rows:
                header = HEADERS[r["tab"]]
                data = json.loads(r["row_json"])
                line = [r["record_id"], uid, r["updated_at"]] + [data.get(c) for c in header[3:]]
                try:
                    if r["tab"] not in ready:
                        sheet.ensure_tab(r["tab"], header)
                        ready.add(r["tab"])
                    rng = sheet.update(r["sheet_range"], line) if r["sheet_range"] else sheet.append(r["tab"], line)
                except Exception as e:  # сеть, права, квота — строка остаётся в очереди
                    failed += 1
                    last_error = str(e)[:300]
                    conn.execute("UPDATE research_outbox SET attempts = attempts + 1, last_error = ? "
                                 "WHERE record_id = ?", (last_error, r["record_id"]))
                    conn.commit()
                    if failed >= 3:  # похоже, недоступна вся таблица — не долбим её дальше
                        break
                    continue
                conn.execute("UPDATE research_outbox SET sent_hash = ?, sheet_range = ?, sent_at = ?, "
                             "last_error = NULL WHERE record_id = ? AND row_hash = ?",
                             (r["row_hash"], rng or r["sheet_range"], now_iso(), r["record_id"], r["row_hash"]))
                conn.commit()
                sent += 1
        finally:
            conn.close()
        return sent, failed, last_error


def _line(uid, r):
    data = json.loads(r["row_json"])
    return [r["record_id"], uid, r["updated_at"]] + [data.get(c) for c in HEADERS[r["tab"]][3:]]


def _flush_batch(conn, sheet, uid, rows, size=40):
    """Отправка пачками через Apps Script. Пачка либо записана целиком, либо остаётся в очереди."""
    sent = failed = 0
    last_error = None
    for start in range(0, len(rows), size):
        part = rows[start:start + size]
        try:
            sheet.upsert_many(HEADERS, [(r["tab"], _line(uid, r)) for r in part])
        except Exception as e:  # сеть, пароль, доступ — вся пачка ждёт следующей попытки
            failed += len(part)
            last_error = str(e)[:300]
            conn.executemany("UPDATE research_outbox SET attempts = attempts + 1, last_error = ? WHERE record_id = ?",
                             [(last_error, r["record_id"]) for r in part])
            conn.commit()
            break
        stamp = now_iso()
        conn.executemany("UPDATE research_outbox SET sent_hash = ?, sent_at = ?, last_error = NULL "
                         "WHERE record_id = ? AND row_hash = ?",
                         [(r["row_hash"], stamp, r["record_id"], r["row_hash"]) for r in part])
        conn.commit()
        sent += len(part)
    return sent, failed, last_error


_worker = {"running": False, "again": False}
_worker_lock = threading.Lock()


def kick():
    """Отправить очередь в фоне, не задерживая ответ участнику. Один поток на процесс: если он уже работает,
    он сделает ещё один проход. В тестах — сразу и в том же потоке (RESEARCH_SYNC)."""
    sheet = client()
    if sheet is None:
        return
    db_path = current_app.config["DATABASE"]
    if current_app.config.get("RESEARCH_SYNC"):
        flush(db_path, sheet)
        return
    with _worker_lock:
        if _worker["running"]:
            _worker["again"] = True
            return
        _worker["running"] = True

    def run():
        while True:
            try:
                flush(db_path, sheet)
            except Exception:  # фоновый поток не должен падать молча навсегда — следующий kick запустит снова
                pass
            with _worker_lock:
                if not _worker["again"]:
                    _worker["running"] = False
                    return
                _worker["again"] = False

    threading.Thread(target=run, daemon=True).start()


def send_now(record_ids):
    """Синхронно отправить конкретные записи; True — таблица подтвердила все."""
    sheet = client()
    if sheet is None:
        return False
    flush(current_app.config["DATABASE"], sheet, only=set(record_ids))
    db = get_db()
    marks = ",".join("?" * len(record_ids))
    pending = db.execute(f"SELECT COUNT(*) FROM research_outbox WHERE record_id IN ({marks}) "
                         "AND (sent_hash IS NULL OR sent_hash != row_hash)", list(record_ids)).fetchone()[0]
    return pending == 0


def session_record(sid):
    return f"{db_uid()}:session:{sid}"


# ---------- проверка для /admin ----------

def status():
    db = get_db()
    sheet = client()
    def q(sql):
        row = db.execute(sql).fetchone()
        return row[0] if row else None

    return {
        "enabled": sheet is not None,
        "mode": getattr(sheet, "mode", None),
        "config_error": current_app.extensions.get("akt_sheets_error"),
        "email": getattr(sheet, "email", None),
        "sheet_id": getattr(sheet, "spreadsheet_id", None),
        "texts": with_texts(),
        "db_uid": db_uid(db),
        "total": q("SELECT COUNT(*) FROM research_outbox"),
        "pending": q("SELECT COUNT(*) FROM research_outbox WHERE sent_hash IS NULL OR sent_hash != row_hash"),
        "failing": q("SELECT COUNT(*) FROM research_outbox WHERE last_error IS NOT NULL"),
        "last_error": q("SELECT last_error FROM research_outbox WHERE last_error IS NOT NULL "
                        "ORDER BY updated_at DESC LIMIT 1"),
        "last_sent": q("SELECT MAX(sent_at) FROM research_outbox"),
    }


def check_sheet():
    """Тестовая запись в вкладку «Проверка» и её подтверждение таблицей."""
    sheet = client()
    stamp = now_iso()
    uid = db_uid()
    line = [f"{uid}:check:{stamp}", uid, stamp, "проверка записи с /admin"]
    if hasattr(sheet, "upsert_many"):
        name = sheet.ping()
        result = sheet.upsert_many(HEADERS, [(TAB_CHECK, line)])
        ok = bool(result and result[0].get("id") == line[0] and result[0].get("row"))
        return ok, f"{stamp} — таблица «{name}», строка {result[0].get('row') if result else '?'}"
    sheet.ensure_tab(TAB_CHECK, HEADERS[TAB_CHECK])
    rng = sheet.append(TAB_CHECK, line)
    back = sheet.read(rng) if rng else []
    return bool(back and back[0] and back[0][0] == line[0]), stamp
