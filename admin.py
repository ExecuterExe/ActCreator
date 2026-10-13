"""Страница исследователя: проверить, что данные хранятся надёжно, и забрать их с сервера.

Доступ — только логинам из ACTKREATOR_ADMINS (через запятую); для остальных страницы нет (404).
На Render нет доступа к файлам сервера, поэтому выгрузка и копия базы скачиваются отсюда.
"""

import io
import os
import sqlite3
import tempfile
import zipfile
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, send_file, url_for

import export_csv
import research
from common import log_event, login_required, parse_time
from db import get_db, now_iso

bp = Blueprint("admin", __name__)

PERSISTENT_ROOT = "/var/data"  # путь постоянного диска из README (план Starter + Disk)


def admins():
    raw = current_app.config.get("ADMINS")
    if raw is None:
        raw = os.environ.get("ACTKREATOR_ADMINS", "")
    if isinstance(raw, str):
        raw = raw.split(",")
    return {a.strip() for a in raw if a and a.strip()}


def require_admin():
    if not g.user or g.user["login"] not in admins():
        abort(404)


def storage_status():
    db = get_db()
    path = Path(current_app.config["DATABASE"])
    meta = {r["key"]: r["value"] for r in db.execute("SELECT key, value FROM meta")}
    created = parse_time(meta.get("created_at"))
    # время старта фиксируется в create_app ДО создания базы: свежая база окажется не старше запуска
    # created_at хранится с точностью до секунды — старт округляем так же, иначе свежая база «старше» запуска
    started = current_app.config["STARTED_AT"].replace(microsecond=0)
    on_render = os.environ.get("RENDER", "").lower() == "true"
    persistent = str(path).replace("\\", "/").startswith(PERSISTENT_ROOT)
    counts = {name: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for name, table in
              (("Участников", "users"), ("Входных тестов", "attempts"), ("Тренировок", "sessions"),
               ("Итоговых оценок", "exit_surveys"), ("Событий", "events"))}
    last = db.execute("SELECT MAX(at) FROM events").fetchone()[0]
    return {"path": str(path), "size_kb": round(path.stat().st_size / 1024) if path.exists() else 0,
            "created": created, "started": started, "survived_restart": bool(created and created < started),
            "on_render": on_render, "persistent": persistent, "risky": on_render and not persistent,
            "last_check": meta.get("last_check"), "last_event": last, "counts": counts}


def check_write():
    """Тестовая запись: пишем отметку, закрываем транзакцию и читаем её НОВЫМ соединением — так видно,
    что данные действительно дошли до файла базы, а не остались в памяти процесса."""
    stamp = now_iso()
    db = get_db()
    db.execute("INSERT INTO meta (key, value) VALUES ('last_check', ?) "
               "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (stamp,))
    db.commit()
    conn = sqlite3.connect(current_app.config["DATABASE"])
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'last_check'").fetchone()
    finally:
        conn.close()
    return bool(row and row[0] == stamp), stamp


def export_zip(with_texts):
    """Все CSV выгрузки + сводка одним архивом; файлы собираются во временной папке и сразу удаляются."""
    with tempfile.TemporaryDirectory() as tmp:
        conn = sqlite3.connect(current_app.config["DATABASE"])
        conn.row_factory = sqlite3.Row
        summary = io.StringIO()
        try:
            with redirect_stdout(summary):
                export_csv.export_all(conn, Path(tmp), with_texts=with_texts)
        finally:
            conn.close()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for p in sorted(Path(tmp).iterdir()):
                z.write(p, p.name)
            z.writestr("summary.txt", summary.getvalue().replace(str(Path(tmp)) + os.sep, ""))
    buf.seek(0)
    return buf


def backup_db():
    """Копия базы средствами SQLite (корректна, даже если в этот момент идёт запись)."""
    src = sqlite3.connect(current_app.config["DATABASE"])
    out = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    out.close()
    dst = sqlite3.connect(out.name)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    data = Path(out.name).read_bytes()
    os.unlink(out.name)
    return io.BytesIO(data)


@bp.route("/admin", methods=["GET", "POST"])
@login_required
def admin_home():
    require_admin()
    stamp = f"{datetime.now():%Y%m%d_%H%M}"
    if request.method == "POST":
        action = request.form.get("action")
        if action == "check":
            ok, at = check_write()
            log_event("storage_check", {"ok": ok})
            flash(f"Запись и чтение прошли: {at}." if ok else "Запись не подтвердилась — данные не сохраняются!")
            return redirect(url_for("admin.admin_home"))
        if action == "sheet_check":
            if not research.enabled():
                abort(400)
            try:
                ok, at = research.check_sheet()
            except Exception as e:  # покажем причину: права, номер таблицы, сеть
                ok, at = False, str(e)[:200]
            log_event("sheet_check", {"ok": ok})
            flash(f"Таблица записала и вернула строку: {at}." if ok else f"Таблица не подтвердила запись: {at}")
            return redirect(url_for("admin.admin_home"))
        if action == "sheet_flush":
            if not research.enabled():
                abort(400)
            sent, failed, error = research.flush(current_app.config["DATABASE"], research.client())
            flash(f"Отправлено строк: {sent}." + (f" Не ушло: {failed} — {error}" if failed else ""))
            return redirect(url_for("admin.admin_home"))
        if action == "export":
            with_texts = request.form.get("with_texts") == "1"
            log_event("research_export", {"with_texts": with_texts})
            return send_file(export_zip(with_texts), mimetype="application/zip", as_attachment=True,
                             download_name=f"actkreator_export_{stamp}.zip")
        if action == "backup":
            log_event("db_backup", {})
            return send_file(backup_db(), mimetype="application/octet-stream", as_attachment=True,
                             download_name=f"actkreator_{stamp}.db")
        abort(400)
    return render_template("admin.html", st=storage_status(), rs=research.status())
