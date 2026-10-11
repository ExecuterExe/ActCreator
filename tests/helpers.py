"""Общие помощники для тестов тренировочного контура."""

import json
import re
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import llm  # noqa: E402
from app import create_app  # noqa: E402

CSRF_RE = re.compile(r'name="csrf" value="([0-9a-f]+)"')


class TrainingCase(unittest.TestCase):
    """Без ИИ (NullClient), если тест не задал свой клиент через self.ai_client."""

    ai_client = None
    full_access = True

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "t.db")
        self.app = create_app({"TESTING": True, "DATABASE": self.db_path, "SECRET_KEY": "t",
                               "AI_CLIENT": self.ai_client or llm.NullClient(),
                               "FULL_ACCESS": self.full_access})
        self.c = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    # --- запросы ---
    def csrf(self, path="/train/card"):
        page = self.c.get(path).get_data(as_text=True)
        m = CSRF_RE.search(page)
        if not m:
            page = self.c.get("/register").get_data(as_text=True)
            m = CSRF_RE.search(page)
        return m.group(1)

    def post(self, path, data, csrf_from=None):
        data = dict(data)
        data["csrf"] = self.csrf(csrf_from or path)
        return self.c.post(path, data=data)

    def page(self, path):
        return self.c.get(path, follow_redirects=True).get_data(as_text=True)

    # --- база ---
    def q(self, sql, args=()):
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(sql, args).fetchall()

    # --- сценарий ---
    def register(self, login="user1"):
        self.post("/register", {"login": login, "password": "password-123", "password2": "password-123",
                                "consent": "1"}, csrf_from="/register")

    def fill_card(self, **extra):
        data = {"R01": "0 ₽", "R02": "2–5 ч", "R10": "посты в соцсетях — не помогли", "ai_notice": "1"}
        data.update(extra)
        return self.post("/train/card?next=/train", data)

    def start(self, mode="task"):
        r = self.post("/train/start", {"mode": mode}, csrf_from="/train")
        m = re.search(r"/s/(\d+)/", r.headers.get("Location", ""))
        return int(m.group(1)) if m else None

    def set_task(self, sid, **extra):
        data = {"text": "Иллюстратор-фрилансер хочет найти новых заказчиков среди небольших издательств.",
                "goal": "3 новых заказа за месяц", "bans": "", "in_domain": "yes"}
        data.update(extra)
        return self.post(f"/s/{sid}/task", data)

    def state(self, sid):
        return self.q("SELECT state FROM sessions WHERE id = ?", (sid,))[0]["state"]

    def execute(self, sql, args=()):
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(sql, args)
            conn.commit()

    def set_level(self, level, user_id=1):
        self.execute("UPDATE user_state SET level = ? WHERE user_id = ?", (level, user_id))

    def submit_first(self, sid, ideas, favorite=1, why="Скидка мотивирует вернуться", **extra):
        data = {f"idea{i}": t for i, t in enumerate(ideas, 1)}
        data.update({"favorite": str(favorite), "why": why})
        data.update(extra)
        return self.post(f"/s/{sid}/first", data)

    def to_tool(self, ideas=("Скидка 10% на первый заказ", "Скидка 20% постоянным", "Промокод для новых"),
                favorite=1, level=None, card=None, task=None, mode="task", **first_extra):
        """Регистрация → карточка → задача → первые идеи → зеркало → шаг «Инструмент»."""
        self.register()
        self.fill_card(**(card or {}))
        if level:
            self.set_level(level)
        sid = self.start(mode)
        self.set_task(sid, **(task or {}))
        self.submit_first(sid, list(ideas), favorite, **first_extra)
        self.page(f"/s/{sid}/mirror")
        self.post(f"/s/{sid}/mirror", {"action": "continue"})
        return sid

    def to_idea(self, code=None, ttype=None, **kw):
        sid = self.to_tool(**kw)
        if code:
            self.force_diagnosis(sid, code, ttype)
        self.page(f"/s/{sid}/tool")
        tool = self.session_json(sid, "tool_json")
        if tool is None:  # уровни 2–3: самодиагноз и выбор рекомендованного инструмента
            d = self.session_json(sid, "diagnosis_json")
            self.post(f"/s/{sid}/tool", {"action": "self_diag", "self_diag": "Не знаю"})
            self.post(f"/s/{sid}/tool", {"action": "choose", "tool": d["tool_type"], "self_diag": "Не знаю"})
            tool = self.session_json(sid, "tool_json")
        if tool["phase"] == "target":
            self.post(f"/s/{sid}/tool", {"action": "target", "target": tool["targets"][0]})
            tool = self.session_json(sid, "tool_json")
        if tool["phase"] == "select":
            self.post(f"/s/{sid}/tool", {"action": "select", "chosen": tool["items"][0]["id"]})
        self.post(f"/s/{sid}/tool", {"action": "go"})
        assert self.state(sid) == "idea", self.state(sid)
        return sid

    def idea(self, sid, action, text="", **extra):
        data = {"action": action, "idea": text}
        data.update(extra)
        return self.post(f"/s/{sid}/idea", data)

    def to_eval(self, final="Договориться с авторами издательства о рекомендациях редакторам", **kw):
        sid = self.to_idea(**kw)
        self.idea(sid, "submit", final, self_check="no")
        assert self.state(sid) == "eval", self.state(sid)
        self.page(f"/s/{sid}/eval")
        return sid

    def run_session(self, final="Стенд на книжной ярмарке", ideas=("Скидка 10% на первый заказ",
                    "Скидка 20% постоянным", "Промокод для новых"), helped="tool", hints=0, plan=False,
                    soften=False, feedback=None):
        """Полная сессия уже зарегистрированного пользователя: от старта до «done» (без пре-мортема)."""
        sid = self.start()
        self.set_task(sid)
        self.submit_first(sid, list(ideas), 1)
        self.page(f"/s/{sid}/mirror")
        self.post(f"/s/{sid}/mirror", {"action": "continue"})
        self.page(f"/s/{sid}/tool")
        tool = self.session_json(sid, "tool_json")
        if tool is None:
            d = self.session_json(sid, "diagnosis_json")
            self.post(f"/s/{sid}/tool", {"action": "self_diag", "self_diag": "Одна колея"})
            self.post(f"/s/{sid}/tool", {"action": "choose", "tool": d["tool_type"], "self_diag": "Одна колея"})
            tool = self.session_json(sid, "tool_json")
        if tool["phase"] == "target":
            self.post(f"/s/{sid}/tool", {"action": "target", "target": tool["targets"][0]})
        tool = self.session_json(sid, "tool_json")
        if tool["phase"] == "select":
            self.post(f"/s/{sid}/tool", {"action": "select", "chosen": tool["items"][0]["id"]})
        if soften:
            self.post(f"/s/{sid}/tool", {"action": "soften"})
        self.post(f"/s/{sid}/tool", {"action": "go"})
        for kind in ("question", "transfer", "fragment")[:hints]:
            self.post(f"/s/{sid}/idea", {"hint": kind})
        self.idea(sid, "submit", final, self_check="no")
        self.page(f"/s/{sid}/eval")
        if feedback:
            self.post(f"/s/{sid}/eval", {"action": feedback})
        self.post(f"/s/{sid}/eval", {"action": "next"})
        if self.state(sid) == "premortem":
            self.post(f"/s/{sid}/premortem", {"action": "skip"})
        self.post(f"/s/{sid}/route", {"helped": helped})
        if plan:
            self.post(f"/s/{sid}/check", {"action": "save", "plan": "Написать 3 редакторам", "when": "завтра",
                                         "ok": "ответил хотя бы один", "fail": "никто не ответил"})
        else:
            self.post(f"/s/{sid}/check", {"action": "skip"})
        assert self.state(sid) == "done", self.state(sid)
        return sid

    def force_diagnosis(self, sid, code, tool_type):
        d = json.loads(self.q("SELECT diagnosis_json FROM sessions WHERE id = ?", (sid,))[0]["diagnosis_json"])
        d.update({"code": code, "tool_type": tool_type})
        self.execute("UPDATE sessions SET diagnosis_json = ?, tool_json = NULL WHERE id = ?",
                     (json.dumps(d, ensure_ascii=False), sid))

    def session_json(self, sid, field):
        return json.loads(self.q(f"SELECT {field} FROM sessions WHERE id = ?", (sid,))[0][field] or "null")
