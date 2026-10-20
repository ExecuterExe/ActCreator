import secrets
import sqlite3
from datetime import datetime, timezone

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    login TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    consent_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Один входной тест на пользователя.
CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL UNIQUE REFERENCES users(id),
    task_id TEXT NOT NULL,
    step TEXT NOT NULL,
    answers_json TEXT NOT NULL DEFAULT '{}',
    ideas_json TEXT,
    top2_json TEXT,
    result_json TEXT,
    feedback_json TEXT,
    started_at TEXT NOT NULL,
    task_started_at TEXT,
    task_seconds REAL,
    completed_at TEXT
);

-- Тренировочный контур (A2, §3).
CREATE TABLE IF NOT EXISTS user_state (
    user_id INTEGER PRIMARY KEY REFERENCES users(id),
    level INTEGER NOT NULL DEFAULT 1,
    strengths_json TEXT NOT NULL DEFAULT '{}',
    card_json TEXT,
    card_kind TEXT,
    card_updated_at TEXT,
    ai_notice_at TEXT,
    trial_started_at TEXT,
    paid_until TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    mode TEXT NOT NULL,
    state TEXT NOT NULL,
    entry_source TEXT NOT NULL DEFAULT 'self',
    level INTEGER NOT NULL,
    methodology_version TEXT NOT NULL,
    task_json TEXT NOT NULL DEFAULT '{}',
    card_json TEXT NOT NULL DEFAULT '{}',
    mirror_json TEXT,
    diagnosis_json TEXT,
    tool_json TEXT,
    eval_json TEXT,
    route_json TEXT,
    flags_json TEXT NOT NULL DEFAULT '{}',
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS ideas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    stage TEXT NOT NULL,
    ord INTEGER NOT NULL DEFAULT 0,
    text TEXT NOT NULL,
    is_favorite INTEGER NOT NULL DEFAULT 0,
    family TEXT,
    subfamily TEXT,
    class_source TEXT,
    confidence TEXT,
    explanation TEXT,
    disputed INTEGER NOT NULL DEFAULT 0,
    status TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    kind TEXT NOT NULL,
    lib_id TEXT,
    text TEXT NOT NULL,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS objections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    author TEXT NOT NULL,
    lib_id TEXT,
    category TEXT,
    card_field TEXT,
    mechanism_link TEXT,
    condition TEXT NOT NULL,
    check_text TEXT,
    check_id TEXT,
    severity INTEGER,
    about_me INTEGER,
    response_type TEXT,
    response_text TEXT,
    verdict TEXT,
    basis TEXT,
    reason TEXT,
    verdict_source TEXT,
    round INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS experiments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL UNIQUE REFERENCES sessions(id),
    objection_id INTEGER,
    template_id TEXT,
    plan TEXT NOT NULL,
    due TEXT,
    success_signal TEXT,
    fail_signal TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    day INTEGER NOT NULL,
    status TEXT NOT NULL,
    text TEXT,
    answered_at TEXT NOT NULL,
    UNIQUE (session_id, day)
);

CREATE TABLE IF NOT EXISTS checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    task_id TEXT NOT NULL,
    ideas_json TEXT,
    top2_json TEXT,
    result_json TEXT,
    started_at TEXT NOT NULL,
    task_started_at TEXT,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS shown_items (
    user_id INTEGER NOT NULL,
    lib TEXT NOT NULL,
    item_id TEXT NOT NULL,
    session_id INTEGER,
    at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scaffold_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    from_level INTEGER NOT NULL,
    to_level INTEGER NOT NULL,
    reason TEXT NOT NULL,
    at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    name TEXT NOT NULL,
    props_json TEXT NOT NULL DEFAULT '{}',
    at TEXT NOT NULL
);

-- «Второе открытие» (C1 §6): вернуться к задаче после перерыва — только по желанию человека.
-- Итоговая оценка тренировки (B6 §2.6; H4 — метрика из реестра): одна запись на сессию, повторная отправка обновляет.
CREATE TABLE IF NOT EXISTS exit_surveys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL UNIQUE REFERENCES sessions(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    useful INTEGER,
    chatgpt_replace TEXT,
    if_closed TEXT,
    if_closed_other TEXT,
    next_time TEXT,
    barriers TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Служебные отметки базы: когда она создана (видно, пережили ли данные перезапуск сервера), последняя проверка записи.
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Очередь отправки данных исследования во внешнее хранилище (research.py → Google Sheets).
-- record_id — постоянный ключ строки; отправлено, когда sent_hash = row_hash; sheet_range — где строка лежит в таблице.
CREATE TABLE IF NOT EXISTS research_outbox (
    record_id TEXT PRIMARY KEY,
    tab TEXT NOT NULL,
    row_json TEXT NOT NULL,
    row_hash TEXT NOT NULL,
    sent_hash TEXT,
    sheet_range TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    updated_at TEXT NOT NULL,
    sent_at TEXT
);

-- Эксперимент «обычный чат-бот против тренировки» (lab.py, docs/product-value/EXPERIMENT_AB.md).
-- Одна задача и одни первые идеи на участника, две руки по очереди; у каждой руки — одинаковая карточка решения.
CREATE TABLE IF NOT EXISTS lab_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    code TEXT NOT NULL,              -- код участника от ведущего, без имени
    arms TEXT NOT NULL,              -- порядок рук: «A,B», «B,A», «A,C», «C,A»
    pre_json TEXT,                   -- вопросы до сессии
    task_json TEXT,                  -- задача, цель, запреты — одинаковые для обеих рук
    first_json TEXT,                 -- первые идеи до любой руки
    post_json TEXT,                  -- вопросы после обеих рук
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lab_arms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES lab_runs(id),
    position INTEGER NOT NULL,       -- 1 или 2
    arm TEXT NOT NULL,               -- A — обычный чат-бот, B — полная тренировка, C — сокращённая
    session_id INTEGER,              -- тренировка для B и C
    chat_json TEXT,                  -- переписка руки A: роль, текст, время ответа, токены
    started_at TEXT,
    work_done_at TEXT,               -- конец работы в руке (до карточки)
    card_json TEXT,                  -- карточка решения
    card_code TEXT,                  -- случайный код карточки для слепой оценки
    card_at TEXT,
    UNIQUE (run_id, position)
);

CREATE TABLE IF NOT EXISTS incubations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL UNIQUE REFERENCES sessions(id),
    due TEXT NOT NULL,
    reframe TEXT,
    barrier TEXT,
    idea_id INTEGER,
    answered_at TEXT,
    created_at TEXT NOT NULL
);
"""

# Колонки, добавленные после v0.2: в существующую базу добавляются при запуске (C1 — рентген и версии идеи).
MIGRATIONS = [
    ("ideas", "xray_json", "TEXT"),      # причинная конструкция идеи: кто, что, за счёт чего, какой барьер
    ("ideas", "version", "INTEGER"),     # номер версии идеи в сессии (эволюция идеи)
    ("ideas", "change_json", "TEXT"),    # что изменилось относительно первых идей / прошлой версии
    ("experiments", "hypothesis", "TEXT"),  # что проверяем: допущение или риск выбранной идеи (18.10)
    ("experiments", "audience", "TEXT"),    # на ком или на чём проверяем
]


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db(path):
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA)
        for table, column, kind in MIGRATIONS:
            existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                try:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
                except sqlite3.OperationalError:  # другой процесс сервера успел добавить колонку раньше
                    pass
        conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('created_at', ?)", (now_iso(),))
        # уникальный номер этой базы: после стирания временного диска номера участников начнутся с 1 заново,
        # а во внешней таблице «u1» из разных баз не должны слиться в одного человека
        conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('db_uid', ?)", (secrets.token_hex(3),))
        conn.commit()
    finally:
        conn.close()
