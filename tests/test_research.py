"""Данные исследования: итоговая оценка тренировки, выгрузка с сигналом H4, страница исследователя."""

import csv
import io
import sqlite3
import tempfile
import zipfile
from contextlib import closing, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

import export_csv
import llm
from app import create_app
from helpers import TrainingCase
from test_a11y import a11y_problems


class ExitSurveyTest(TrainingCase):
    def setUp(self):
        super().setUp()
        self.register()
        self.fill_card()
        self.sid = self.run_session(plan=True)

    def answer(self, **data):
        data.setdefault("action", "exit_survey")
        return self.post(f"/s/{self.sid}/done", data)

    def test_survey_on_done_page_and_upsert(self):
        page = self.page(f"/s/{self.sid}")
        for text in ("Итоговая оценка тренировки", "Мог бы обычный ChatGPT заменить вам такую тренировку?",
                     "Если бы такой сервис закрылся, что бы вы сделали?"):
            self.assertIn(text, page)
        self.assertIn("Ответьте хотя бы на один вопрос", self.answer().get_data(as_text=True))
        self.assertEqual(self.q("SELECT COUNT(*) AS n FROM exit_surveys")[0]["n"], 0)
        r = self.answer(useful="4", chatgpt_replace="Не заменит", if_closed="Ничего", next_time="Зависит от задачи",
                        barriers="Долго ждать ответа ИИ")
        self.assertEqual(r.status_code, 302)
        self.assertIn("Спасибо — оценка сохранена", self.page(f"/s/{self.sid}"))
        # повторная отправка обновляет ту же запись, а не создаёт вторую
        self.answer(useful="5", chatgpt_replace="Частично")
        rows = self.q("SELECT * FROM exit_surveys")
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["useful"], rows[0]["chatgpt_replace"], rows[0]["barriers"]), (5, "Частично", None))
        events = self.q("SELECT props_json FROM events WHERE name = 'exit_survey_saved'")
        self.assertEqual(len(events), 2)
        self.assertNotIn("Долго ждать", events[0]["props_json"])  # свободный текст — не в журнал событий

    def test_values_are_checked_against_lists(self):
        self.answer(useful="9", chatgpt_replace="Взлом", if_closed="Ничего", if_closed_other="не должно сохраниться",
                    next_time="Снова воспользуюсь АктКреватором")
        row = self.q("SELECT * FROM exit_surveys")[0]
        self.assertEqual((row["useful"], row["chatgpt_replace"], row["if_closed_other"], row["next_time"]),
                         (None, None, None, "Снова воспользуюсь АктКреватором"))
        self.answer(if_closed="Другое", if_closed_other="Спросил(а) бы коллег")
        self.assertEqual(self.q("SELECT if_closed_other FROM exit_surveys")[0]["if_closed_other"],
                         "Спросил(а) бы коллег")

    def test_foreign_session_is_closed(self):
        self.post("/logout", {}, csrf_from="/me")
        self.register("other")
        r = self.c.post(f"/s/{self.sid}/done", data={"csrf": self.csrf("/me"), "action": "exit_survey",
                                                     "useful": "1"})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self.q("SELECT COUNT(*) AS n FROM exit_surveys")[0]["n"], 0)

    def test_export_has_survey_and_h4_by_participant(self):
        self.answer(useful="2", chatgpt_replace="Полностью заменит", if_closed="Ничего")
        sid2 = self.run_session()
        self.post(f"/s/{sid2}/done", {"action": "exit_survey", "useful": "4", "if_closed": "Стал(а) бы искать аналог"})
        with tempfile.TemporaryDirectory() as tmp:
            with closing(sqlite3.connect(self.db_path)) as conn, redirect_stdout(io.StringIO()) as out:
                conn.row_factory = sqlite3.Row
                export_csv.export_all(conn, Path(tmp), with_texts=True)
            files = {p.name.split("_")[0]: p for p in Path(tmp).iterdir()}
            with open(files["sessions"], encoding="utf-8-sig") as f:
                rows = {r["session"]: r for r in csv.DictReader(f)}
            with open(files["ideas"], encoding="utf-8-sig") as f:
                ideas = list(csv.DictReader(f))
        self.assertEqual((rows[str(self.sid)]["exit_useful"], rows[str(self.sid)]["h4_signal"]), ("2", "0"))
        self.assertEqual(rows[str(sid2)]["h4_signal"], "1")
        # по участнику берётся последний ответ: один человек, сигнал H4 — 100%
        self.assertIn("Сигнал H4", out.getvalue())
        self.assertIn("100% участников из 1 ответивших", out.getvalue())
        self.assertIn("Стенд на книжной ярмарке", [i["text"] for i in ideas if i["source"] == "участник"])
        self.assertTrue([i for i in ideas if i["source"] == "чат-бот"])  # ответы чат-бота по той же задаче


class AdminTest(TrainingCase):
    def setUp(self):
        super().setUp()
        self.app.config["ADMINS"] = "boss"

    def test_hidden_for_everyone_else(self):
        r = self.c.get("/admin")
        self.assertEqual(r.status_code, 302)  # гостя — на вход
        self.register("user1")
        self.assertEqual(self.c.get("/admin").status_code, 404)

    def test_status_check_export_backup(self):
        self.register("boss")
        page = self.page("/admin")
        self.assertIn("Данные исследования", page)
        self.assertIn("пока не видно", page)  # база создана при этом запуске
        r = self.post("/admin", {"action": "check"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("Запись и чтение прошли", self.page("/admin"))
        r = self.post("/admin", {"action": "export", "with_texts": "1"})
        self.assertEqual(r.mimetype, "application/zip")
        with zipfile.ZipFile(io.BytesIO(r.data)) as z:
            names = z.namelist()
            summary = z.read("summary.txt").decode("utf-8")
        for prefix in ("attempts_", "sessions_", "checkpoints_", "ideas_", "summary.txt"):
            self.assertTrue(any(n.startswith(prefix) for n in names), prefix)
        self.assertNotIn(tempfile.gettempdir(), summary)  # пути сервера наружу не уходят
        r = self.post("/admin", {"action": "backup"})
        self.assertTrue(r.data.startswith(b"SQLite format 3"))

    def test_restart_survival_is_visible(self):
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
        self.execute("UPDATE meta SET value = ? WHERE key = 'created_at'", (past,))
        self.register("boss")
        self.assertIn("да — база старше этого запуска сервера", self.page("/admin"))

    def test_data_survive_a_restart_on_the_same_file(self):
        self.register("boss")
        self.fill_card()
        sid = self.start()
        # «перезапуск»: новый процесс сервера открывает тот же файл базы позже
        later = datetime.now(timezone.utc) + timedelta(seconds=5)
        app2 = create_app({"TESTING": True, "DATABASE": self.db_path, "SECRET_KEY": "t", "ADMINS": "boss",
                           "AI_CLIENT": llm.NullClient(), "FULL_ACCESS": True, "STARTED_AT": later})
        c2 = app2.test_client()
        page = c2.get("/login").get_data(as_text=True)
        token = page.split('name="csrf" value="')[1].split('"')[0]
        c2.post("/login", data={"csrf": token, "login": "boss", "password": "password-123"})
        page = c2.get("/admin").get_data(as_text=True)
        self.assertIn("да — база старше этого запуска сервера", page)
        self.assertEqual(self.q("SELECT COUNT(*) AS n FROM sessions WHERE id = ?", (sid,))[0]["n"], 1)
        self.assertIn("<dt>Тренировок</dt><dd>1</dd>", page)

    def test_admin_page_is_accessible(self):
        self.register("boss")
        self.assertEqual(a11y_problems(self.page("/admin")), [])

