"""АктКреватор — минимальный сайт: регистрация, вход и входной тест.

Запуск: см. README.md. Дизайна нет намеренно — проверяем логику.
"""

import json
import os
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import (
    Flask,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

import common
import content
import library
import llm
import research
import scoring
from db import close_db, get_db, init_db, now_iso

TASK_SECONDS = 180
TASK_GRACE_SECONDS = 15
LOGIN_RE = re.compile(r"^[\w.\-]{3,32}$")
MIN_PASSWORD = 8
MAX_FAILED_LOGINS = 5
FAILED_WINDOW_SECONDS = 15 * 60


def ru_date(value):
    """«2026-10-11» → «11.10.2026» (для дат проверок и исходов)."""
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", value or "")
    return f"{m.group(3)}.{m.group(2)}.{m.group(1)}" if m else (value or "")


def _load_secret_key(instance_path):
    key = os.environ.get("ACTKREATOR_SECRET")
    if key:
        return key
    path = Path(instance_path) / "secret_key"
    if not path.exists():
        path.write_text(secrets.token_hex(32), encoding="utf-8")
    return path.read_text(encoding="utf-8").strip()


def create_app(test_config=None):
    # Путь задаём явно: иначе при запуске как скрипта Flask кладёт базу
    # в папку, из которой запущен python.
    app = Flask(__name__, instance_path=str(Path(__file__).resolve().parent / "instance"))
    # До создания базы: страница исследователя сравнивает с этим временем дату создания базы (пережит ли перезапуск)
    app.config["STARTED_AT"] = datetime.now(timezone.utc)
    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    app.config.update(
        # На хостинге база должна лежать на постоянном диске: путь задаётся ACTKREATOR_DB.
        DATABASE=os.environ.get("ACTKREATOR_DB") or str(Path(app.instance_path) / "actkreator.db"),
        TASK_PATH=str(scoring.default_task_path()),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("ACTKREATOR_HTTPS") == "1",
    )
    if test_config:
        app.config.update(test_config)
    if not app.config.get("SECRET_KEY"):
        app.config["SECRET_KEY"] = _load_secret_key(app.instance_path)

    Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)
    init_db(app.config["DATABASE"])
    app.teardown_appcontext(close_db)

    # Библиотеки содержания и ИИ (через OpenRouter, ключ в settings.env).
    settings = llm.load_settings()
    app.extensions["akt_lib"] = library.Library(app.config.get("LIBRARY_DIR") or library.DEFAULT_DIR)
    client = app.config.get("AI_CLIENT") or llm.make_client(settings)

    def ai_event(name, props):
        try:
            common.log_event(name, props)
        except Exception:  # журнал не должен ломать страницу
            pass

    app.extensions["akt_ai"] = llm.AI(client, on_event=ai_event)
    if "FULL_ACCESS" not in app.config:
        app.config["FULL_ACCESS"] = settings.get("PILOT_FULL_ACCESS", "0").strip() == "1"

    task = scoring.load_task(app.config["TASK_PATH"])
    classifier = scoring.Classifier(task["families"])
    failed_logins = {}

    # ---------- общие помощники ----------

    @app.before_request
    def load_user_and_check_csrf():
        g.user = None
        user_id = session.get("user_id")
        if user_id is not None:
            g.user = get_db().execute(
                "SELECT id, login FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            if g.user is None:
                session.clear()
        if request.method == "POST":
            token = session.get("csrf")
            if not token or not secrets.compare_digest(token, request.form.get("csrf", "")):
                abort(400)

    @app.context_processor
    def inject_helpers():
        def csrf_token():
            if "csrf" not in session:
                session["csrf"] = secrets.token_hex(16)
            return session["csrf"]

        return {"csrf_token": csrf_token, "task": task, "ui": common.lib().t,
                "ai_on": common.ai().available, "ru_date": ru_date}

    login_required = common.login_required

    def get_attempt():
        return get_db().execute(
            "SELECT * FROM attempts WHERE user_id = ?", (g.user["id"],)
        ).fetchone()

    def start_attempt():
        db = get_db()
        db.execute(
            "INSERT OR IGNORE INTO attempts (user_id, task_id, step, started_at) VALUES (?, ?, ?, ?)",
            (g.user["id"], task["id"], content.STEPS[0], now_iso()),
        )
        db.commit()
        return get_attempt()

    def next_step(step):
        return content.STEPS[content.STEPS.index(step) + 1]

    def redirect_to_step(step):
        if step == "done":
            return redirect(url_for("test_result"))
        if step in content.SURVEY:
            return redirect(url_for("test_survey", step=step))
        return redirect(url_for("test_" + step))

    def is_throttled(login):
        now = time.time()
        recent = [t for t in failed_logins.get(login, []) if now - t < FAILED_WINDOW_SECONDS]
        failed_logins[login] = recent
        return len(recent) >= MAX_FAILED_LOGINS

    def safe_next(target):
        if target and target.startswith("/") and not target.startswith("//"):
            return target
        return url_for("me")

    # ---------- страницы ----------

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.route("/register", methods=["GET", "POST"])
    def register():
        if g.user:
            return redirect(url_for("me"))
        errors, login_value = {}, ""
        if request.method == "POST":
            login_value = (request.form.get("login") or "").strip()
            password = request.form.get("password") or ""
            password2 = request.form.get("password2") or ""
            if not LOGIN_RE.match(login_value):
                errors["login"] = "Логин: от 3 до 32 символов — буквы, цифры, точка, дефис или подчёркивание."
            if len(password) < MIN_PASSWORD:
                errors["password"] = f"Пароль должен быть не короче {MIN_PASSWORD} символов."
            elif password != password2:
                errors["password2"] = "Пароли не совпадают."
            if not request.form.get("consent"):
                errors["consent"] = "Без согласия мы не можем сохранить ваши ответы."
            db = get_db()
            if not errors and db.execute(
                "SELECT 1 FROM users WHERE login = ?", (login_value,)
            ).fetchone():
                errors["login"] = "Такой логин уже занят."
            if not errors:
                now = now_iso()
                cur = db.execute(
                    "INSERT INTO users (login, password_hash, consent_at, created_at) VALUES (?, ?, ?, ?)",
                    (login_value, generate_password_hash(password), now, now),
                )
                db.commit()
                session.clear()
                session["user_id"] = cur.lastrowid
                return redirect(url_for("test_start"))
        return render_template("register.html", errors=errors, login_value=login_value)

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if g.user:
            return redirect(url_for("me"))
        error, login_value = None, ""
        if request.method == "POST":
            login_value = (request.form.get("login") or "").strip()
            password = request.form.get("password") or ""
            key = login_value.lower()
            if is_throttled(key):
                error = "Слишком много неудачных попыток. Попробуйте через 15 минут."
            else:
                user = get_db().execute(
                    "SELECT id, password_hash FROM users WHERE login = ?", (login_value,)
                ).fetchone()
                if user and check_password_hash(user["password_hash"], password):
                    failed_logins.pop(key, None)
                    session.clear()
                    session["user_id"] = user["id"]
                    return redirect(safe_next(request.args.get("next")))
                failed_logins.setdefault(key, []).append(time.time())
                error = "Неверный логин или пароль."
        return render_template("login.html", error=error, login_value=login_value)

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("index"))

    @app.get("/me")
    @login_required
    def me():
        from between import me_context  # кабинет собирает данные «между сессиями» (B5)

        return render_template("me.html", attempt=get_attempt(), **me_context())

    # ---------- входной тест ----------

    @app.get("/test")
    @login_required
    def test_start():
        attempt = get_attempt() or start_attempt()
        return redirect_to_step(attempt["step"])

    @app.route("/test/survey/<step>", methods=["GET", "POST"])
    @login_required
    def test_survey(step):
        if step not in content.SURVEY:
            abort(404)
        attempt = get_attempt() or start_attempt()
        if attempt["step"] != step:
            return redirect_to_step(attempt["step"])
        answers = json.loads(attempt["answers_json"])
        errors, current = {}, answers.get(step, {})
        if request.method == "POST":
            current, errors = content.validate(step, request.form)
            if not errors:
                answers[step] = current
                db = get_db()
                db.execute(
                    "UPDATE attempts SET answers_json = ?, step = ? WHERE id = ?",
                    (json.dumps(answers, ensure_ascii=False), next_step(step), attempt["id"]),
                )
                db.commit()
                research.queue_attempt(attempt["id"])  # каждый шаг анкеты — сразу во внешнюю таблицу
                return redirect_to_step(next_step(step))
        return render_template(
            "survey.html", step=step, block=content.SURVEY[step], values=current, errors=errors
        )

    @app.route("/test/task", methods=["GET", "POST"])
    @login_required
    def test_task():
        attempt = get_attempt() or start_attempt()
        if attempt["step"] != "task":
            return redirect_to_step(attempt["step"])
        db = get_db()
        started = attempt["task_started_at"]
        if request.method == "GET" and request.args.get("go") and not started:
            started = now_iso()
            db.execute("UPDATE attempts SET task_started_at = ? WHERE id = ?", (started, attempt["id"]))
            db.commit()
        if not started:
            return render_template("task_intro.html", seconds=TASK_SECONDS)

        elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(started)).total_seconds()
        if request.method == "POST":
            ideas = scoring.parse_ideas(request.form.get("ideas"))
            classified = scoring.classify_ideas(classifier, ideas)
            db.execute(
                "UPDATE attempts SET ideas_json = ?, task_seconds = ?, step = ? WHERE id = ?",
                (
                    json.dumps(classified, ensure_ascii=False),
                    round(min(elapsed, TASK_SECONDS + TASK_GRACE_SECONDS), 1),
                    "pick",
                    attempt["id"],
                ),
            )
            db.commit()
            research.queue_attempt(attempt["id"])
            return redirect(url_for("test_pick"))
        remaining = max(0, int(TASK_SECONDS - elapsed))
        return render_template("task.html", remaining=remaining, seconds=TASK_SECONDS)

    @app.route("/test/pick", methods=["GET", "POST"])
    @login_required
    def test_pick():
        attempt = get_attempt()
        if attempt is None or attempt["step"] != "pick":
            return redirect(url_for("test_start"))
        ideas = json.loads(attempt["ideas_json"] or "[]")
        need = min(2, len(ideas))
        error = None
        if request.method == "POST" or need < 2:
            picked = sorted({int(v) for v in request.form.getlist("pick") if v.isdigit()})
            picked = [i for i in picked if i < len(ideas)]
            if need < 2:
                picked = list(range(need))
            if len(picked) == need:
                finish_attempt(attempt, ideas, picked)
                return redirect(url_for("test_result"))
            error = "Отметьте ровно две идеи."
        return render_template("pick.html", ideas=ideas, error=error)

    def finish_attempt(attempt, ideas, picked):
        db = get_db()
        rows = db.execute(
            "SELECT ideas_json FROM attempts WHERE completed_at IS NOT NULL AND task_id = ?",
            (task["id"],),
        ).fetchall()
        others = [json.loads(r["ideas_json"]) for r in rows]
        n, shares, avg = scoring.family_frequencies(others)
        result = scoring.build_result(task, ideas, picked, n, shares, avg)
        db.execute(
            "UPDATE attempts SET top2_json = ?, result_json = ?, step = 'done', completed_at = ? WHERE id = ?",
            (json.dumps(picked), json.dumps(result, ensure_ascii=False), now_iso(), attempt["id"]),
        )
        db.commit()
        research.queue_attempt(attempt["id"])

    @app.get("/test/result")
    @login_required
    def test_result():
        attempt = get_attempt()
        if attempt is None or attempt["step"] != "done":
            return redirect(url_for("test_start"))
        result = json.loads(attempt["result_json"])
        feedback = json.loads(attempt["feedback_json"]) if attempt["feedback_json"] else None
        return render_template(
            "result.html",
            r=result,
            feedback=feedback,
            surprise_options=content.FEEDBACK_SURPRISE,
        )

    @app.post("/test/feedback")
    @login_required
    def test_feedback():
        attempt = get_attempt()
        if attempt is None or attempt["step"] != "done":
            return redirect(url_for("test_start"))
        allowed = {v for v, _ in content.FEEDBACK_SURPRISE}
        rating = request.form.get("useful")
        feedback = {
            "useful": rating if rating in {"1", "2", "3", "4", "5"} else None,
            "surprise": [v for v in request.form.getlist("surprise") if v in allowed],
            "comment": (request.form.get("comment") or "").strip()[:1000],
            "at": now_iso(),
        }
        db = get_db()
        db.execute(
            "UPDATE attempts SET feedback_json = ? WHERE id = ?",
            (json.dumps(feedback, ensure_ascii=False), attempt["id"]),
        )
        db.commit()
        research.queue_attempt(attempt["id"])
        flash("Спасибо! Ответ сохранён.")
        return redirect(url_for("test_result"))

    import between
    import training

    import admin
    import lab

    app.register_blueprint(training.bp)
    app.register_blueprint(between.bp)
    app.register_blueprint(admin.bp)
    app.register_blueprint(lab.bp)  # эксперимент A/B/C — только при ACTKREATOR_LAB=1
    return app


def __getattr__(name):
    """`gunicorn app:app` (стандартная команда Render) получает приложение при первом обращении.

    Лениво — чтобы простой импорт create_app (тесты, скрипты) не создавал базу и не читал библиотеки.
    """
    if name == "app":
        globals()["app"] = create_app()
        return globals()["app"]
    raise AttributeError(f"module 'app' has no attribute {name!r}")


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=int(os.environ.get("PORT", 5000)), debug=False)
