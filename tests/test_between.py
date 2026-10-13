import json
import re
import unittest
from datetime import datetime, timedelta, timezone

from helpers import TrainingCase
from library import agree_numbers


def ago(days, minutes=0):
    return (datetime.now(timezone.utc) - timedelta(days=days, minutes=minutes)).isoformat(timespec="seconds")


class BetweenCase(TrainingCase):
    def setUp(self):
        super().setUp()
        self.register()
        self.fill_card()

    def age_session(self, sid, days):
        self.execute("UPDATE sessions SET completed_at = ? WHERE id = ?", (ago(days), sid))

    def answer(self, sid, day, status, **extra):
        data = {"status": status, "next": "/me"}
        data.update(extra)
        return self.post(f"/outcome/{sid}/{day}", data, csrf_from="/me")


class OutcomesTest(BetweenCase):
    def test_day2_question_with_plan(self):
        sid = self.run_session(plan=True)
        self.assertNotIn("Вопросы о ваших идеях", self.page("/me"))
        self.age_session(sid, 3)
        page = self.page("/me")
        self.assertIn("Вопросы о ваших идеях (1)", page)
        self.assertIn("Удалось сделать проверку «Написать 3 редакторам»?", page)
        self.answer(sid, 2, "что-то своё")  # нет такого варианта — не сохраняется
        self.assertEqual(len(self.q("SELECT * FROM outcomes")), 0)
        self.answer(sid, 2, "Сделал(а): возражение снято", text="ответили двое")
        row = self.q("SELECT * FROM outcomes")[0]
        self.assertEqual((row["day"], row["status"], row["text"]), (2, "Сделал(а): возражение снято", "ответили двое"))
        self.assertEqual(self.q("SELECT status FROM ideas WHERE stage = 'final'")[0]["status"], "проверено")
        self.assertNotIn("Вопросы о ваших идеях", self.page("/me"))
        self.assertEqual(self.answer(sid, 2, "Частично").status_code, 404)  # повторно нельзя
        # следующий вход в тренировку помечается как пришедший из вопроса об исходе
        self.start()
        self.assertEqual(self.q("SELECT entry_source FROM sessions ORDER BY id DESC")[0]["entry_source"],
                         "outcome_link")

    def test_day7_without_plan_and_refusal_reason(self):
        sid = self.run_session(plan=False)
        self.age_session(sid, 8)
        page = self.page("/me")
        self.assertNotIn("Удалось сделать проверку", page)  # дня 2 нет без плана
        self.assertIn("Что сейчас с идеей «Стенд на книжной ярмарке»?", page)
        self.answer(sid, 7, "Отказались", reason="Нет времени", text="закрутился")
        row = self.q("SELECT * FROM outcomes")[0]
        self.assertEqual(row["text"], "Нет времени. закрутился")
        self.assertEqual(self.q("SELECT status FROM ideas WHERE stage = 'final'")[0]["status"], "отказались")

    def test_max_three_questions_and_day30(self):
        sids = [self.run_session(plan=True) for _ in range(2)]
        for sid in sids:
            self.age_session(sid, 31)
        page = self.page("/me")
        self.assertIn("Вопросы о ваших идеях (6)", page)
        self.assertEqual(len(re.findall(r'action="/outcome/', page)), 3)  # не больше 3 одновременно
        self.assertIn("в <a href=\"/bank\">банке идей</a>", page)
        self.answer(sids[0], 30, "Нет")
        self.assertEqual(self.q("SELECT status FROM ideas WHERE session_id = ? AND stage = 'final'",
                                (sids[0],))[0]["status"], "не сработало")

    def test_not_due_and_foreign_rejected(self):
        sid = self.run_session(plan=True)
        self.assertEqual(self.answer(sid, 2, "Не начал(а)").status_code, 404)  # срок не наступил
        self.assertEqual(self.answer(sid, 5, "Не начал(а)").status_code, 404)
        self.age_session(sid, 3)
        self.post("/logout", {}, csrf_from="/me")
        self.register("other")
        self.assertEqual(self.answer(sid, 2, "Не начал(а)").status_code, 404)

    def test_practice_has_no_outcomes(self):
        sid = self.start("practice")
        self.execute("UPDATE sessions SET state = 'done', completed_at = ? WHERE id = ?", (ago(40), sid))
        self.assertNotIn("Вопросы о ваших идеях", self.page("/me"))


class BankTest(BetweenCase):
    def test_empty_bank(self):
        self.assertIn("Банк идей пока пуст", self.page("/bank"))

    def test_groups_card_status_delete(self):
        sid = self.run_session(plan=True)
        page = self.page("/bank")
        self.assertIn("К проверке (1)", page)
        idea_id = self.q("SELECT id FROM ideas WHERE stage = 'final'")[0]["id"]
        card = self.page(f"/bank/{idea_id}")
        for part in ("Иллюстратор-фрилансер", "Первые идеи (3)", "Оператор преобразования", "Написать 3 редакторам",
                     "Продолжить прошлую задачу", "Возражения", "План проверки", f"/s/{sid}/journal",
                     'for="idea-status"', "data-confirm="):
            self.assertIn(part, card)
        self.post(f"/bank/{idea_id}", {"action": "status", "status": "отложено"})
        self.assertIn("Отложено (1)", self.page("/bank"))
        self.assertEqual(self.post(f"/bank/{idea_id}", {"action": "status", "status": "взлом"}).status_code, 400)
        self.post(f"/bank/{idea_id}", {"action": "delete"})
        row = self.q("SELECT text, status FROM ideas WHERE id = ?", (idea_id,))[0]
        self.assertEqual((row["text"], row["status"]), ("", "удалено"))
        self.assertIn("Банк идей пока пуст", self.page("/bank"))
        self.assertEqual(self.c.get(f"/bank/{idea_id}").status_code, 404)

    def test_drafts_from_abandoned_and_extras(self):
        sid = self.start()
        self.set_task(sid)
        self.submit_first(sid, ["Скидка 10%", "Скидка 20%"], 1)
        self.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (ago(2), sid))
        self.page(f"/s/{sid}")  # сессия брошена → идеи в черновики
        page = self.page("/bank")
        self.assertIn("Черновики (2)", page)
        sid2 = self.start()
        self.set_task(sid2)
        self.submit_first(sid2, ["Скидка 10% на первый заказ", "Скидка 20% постоянным", "Промокод для новых"], 1)
        self.page(f"/s/{sid2}/mirror")
        self.post(f"/s/{sid2}/mirror", {"action": "continue"})
        self.page(f"/s/{sid2}/tool")
        self.post(f"/s/{sid2}/tool", {"action": "go"})
        self.idea(sid2, "submit", "- Блог об обложках\n- Стенд на ярмарке")
        self.idea(sid2, "pick", chosen="1")
        self.assertIn("Дополнительные (1)", self.page("/bank"))
        extra = self.q("SELECT id FROM ideas WHERE stage = 'extra'")[0]["id"]
        card = self.page(f"/bank/{extra}")  # у дополнительной идеи нет ступени эволюции — карточка всё равно цела
        self.assertIn("дополнительная", card)
        self.assertNotIn("эта идея", card)

    def test_continue_task_from_bank(self):
        old = self.run_session()
        page = self.page(f"/train?prev={old}")
        self.assertIn('name="prev" value="%d"' % old, page)
        r = self.post("/train/start", {"mode": "task", "prev": str(old)}, csrf_from="/train")
        self.assertIn(f"/task?prev={old}", r.headers["Location"])
        page = self.page(r.headers["Location"])
        self.assertIn("Иллюстратор-фрилансер хочет найти новых заказчиков", page)

    def test_draft_has_no_card_and_dates_are_russian(self):
        sid = self.run_session(plan=True)
        draft = self.q("SELECT id FROM ideas WHERE stage = 'draft'")[0]["id"]
        self.assertEqual(self.c.get(f"/bank/{draft}").status_code, 404)
        due = self.q("SELECT due FROM experiments")[0]["due"]
        y, m, d = due.split("-")
        self.assertIn(f"(до {d}.{m}.{y})", self.page(f"/s/{sid}"))

    def test_foreign_card_404(self):
        self.run_session()
        idea_id = self.q("SELECT id FROM ideas WHERE stage = 'final'")[0]["id"]
        self.post("/logout", {}, csrf_from="/me")
        self.register("other")
        self.assertEqual(self.c.get(f"/bank/{idea_id}").status_code, 404)


class CheckpointTest(BetweenCase):
    def make_due(self):
        """Отсчёт — от первой завершённой сессии: 15 дней назад, и после неё была ещё одна."""
        if not self.q("SELECT id FROM sessions"):
            self.run_session()
        self.run_session()
        self.execute("UPDATE sessions SET completed_at = ? WHERE id = (SELECT MIN(id) FROM sessions)", (ago(15),))

    def do_checkpoint(self, ideas="Скидка новым гостям\nКонкурс в соцсетях\nПеть песни у входа"):
        self.post("/checkpoint/start", {}, csrf_from="/checkpoint")
        cid = self.q("SELECT id FROM checkpoints ORDER BY id DESC")[0]["id"]
        self.page(f"/checkpoint/{cid}/task")
        self.post(f"/checkpoint/{cid}/task", {"ideas": ideas})
        self.post(f"/checkpoint/{cid}/pick", {"pick": ["0", "2"]})
        return cid

    def test_due_invite_and_full_flow(self):
        self.run_session()
        self.assertNotIn("Пора на контрольную точку", self.page("/me"))
        self.make_due()
        self.assertIn("Пора на контрольную точку", self.page("/me"))
        self.post("/checkpoint/start", {}, csrf_from="/checkpoint")
        cp = self.q("SELECT * FROM checkpoints")[0]
        self.assertEqual(cp["task_id"], "S01")  # первая задача группы «Л»
        page = self.page(f"/checkpoint/{cp['id']}/task")
        self.assertIn("пекарня", page)
        r = self.post(f"/checkpoint/{cp['id']}/task", {"ideas": "   "})
        self.assertIn("хотя бы одну идею", r.get_data(as_text=True))
        self.post(f"/checkpoint/{cp['id']}/task", {"ideas": "Скидка новым гостям\nКонкурс в соцсетях\nПеть песни у входа"})
        r = self.post(f"/checkpoint/{cp['id']}/pick", {"pick": ["0"]})
        self.assertIn("ровно две", r.get_data(as_text=True))
        self.post(f"/checkpoint/{cp['id']}/pick", {"pick": ["0", "2"]})
        cp = self.q("SELECT * FROM checkpoints")[0]
        self.assertIsNotNone(cp["completed_at"])
        result = json.loads(cp["result_json"])
        self.assertEqual(result["metrics"]["n_ideas"], 3)
        self.assertEqual(result["common_source"], "seed")
        page = self.page(f"/checkpoint/{cp['id']}")
        self.assertIn(agree_numbers(f"3 идей → {result['metrics']['flexibility']} подходов"), page)
        self.assertIn("3 идеи →", page)
        self.assertIn("Пока точек меньше трёх — выводы делать рано", page)
        self.assertNotIn("Пора на контрольную точку", self.page("/me"))
        # не чаще раза в 7 дней
        self.post("/checkpoint/start", {}, csrf_from="/checkpoint")
        self.assertEqual(len(self.q("SELECT * FROM checkpoints")), 1)
        self.assertIn("не раньше", self.page("/checkpoint"))

    def test_groups_alternate_and_trend(self):
        self.run_session()
        for i, (task, ipm) in enumerate([("S01", 0), ("S02", 3)]):
            self.execute("INSERT INTO checkpoints (user_id, task_id, started_at, completed_at, result_json) "
                         "VALUES (1, ?, ?, ?, ?)", (task, ago(30 - i * 8), ago(30 - i * 8),
                                                    json.dumps({"metrics": {"rare_families": ipm, "n_ideas": 5}})))
        self.run_session()  # сессия после последней точки
        cid = self.do_checkpoint("Дегустация с соседним магазином\nКонкурс рисунков\nКлуб любителей\nСкидка")
        task = self.q("SELECT task_id FROM checkpoints WHERE id = ?", (cid,))[0]["task_id"]
        groups = {t["id"]: t["group"] for t in self.app.extensions["akt_lib"].standard_tasks}
        self.assertEqual(groups["S02"], "Ф")
        self.assertEqual(groups[task], "П")  # после «Ф» — «П»
        ipm = json.loads(self.q("SELECT result_json FROM checkpoints WHERE id = ?", (cid,))[0]["result_json"])
        page = self.page(f"/checkpoint/{cid}")
        expected = "стали разнообразнее" if (3 + ipm["metrics"]["rare_families"]) / 2 - 0 >= 2 else "Пока заметных изменений нет"
        self.assertIn(expected, page)

    def test_postpone_twice_explains(self):
        self.make_due()
        self.post("/checkpoint/postpone", {}, csrf_from="/me")
        self.assertNotIn("Пора на контрольную точку", self.page("/me"))
        self.execute("UPDATE events SET at = ? WHERE name = 'checkpoint_postponed'", (ago(8),))
        self.assertIn("Пора на контрольную точку", self.page("/me"))
        self.post("/checkpoint/postpone", {}, csrf_from="/me")
        self.assertIn("Контрольные точки — единственный способ", self.page("/me"))

    def test_late_answer_marked(self):
        self.make_due()
        self.post("/checkpoint/start", {}, csrf_from="/checkpoint")
        cid = self.q("SELECT id FROM checkpoints")[0]["id"]
        self.page(f"/checkpoint/{cid}/task")
        self.execute("UPDATE checkpoints SET task_started_at = ? WHERE id = ?", (ago(0, minutes=5), cid))
        self.post(f"/checkpoint/{cid}/task", {"ideas": "Скидка"})
        self.assertIn("позже 3 минут", self.page(f"/checkpoint/{cid}"))  # одна идея — выбор пропускается

    def test_entry_test_is_base_point(self):
        result = {"n_ideas": 4, "n_families": 3, "ideas": [
            {"family": "discounts", "common": True}, {"family": "events", "common": False},
            {"family": "mission", "common": False}, {"family": None, "common": False}]}
        self.execute("INSERT INTO attempts (user_id, task_id, step, started_at, completed_at, result_json) "
                     "VALUES (1, 'coffee_v1', 'done', ?, ?, ?)", (ago(20), ago(20), json.dumps(result)))
        self.run_session()
        self.assertIn("Пора на контрольную точку", self.page("/me"))
        page = self.page("/checkpoint")
        self.assertIn("Входной тест", page)


class CheckpointPaywallTest(BetweenCase):
    full_access = False

    def test_checkpoints_are_paid(self):
        r = self.c.get("/checkpoint")
        self.assertIn("/paywall", r.headers["Location"])


class LevelAndSummaryTest(BetweenCase):
    def test_manual_level_once_a_week(self):
        self.post("/level", {"direction": "down"}, csrf_from="/me")
        self.assertEqual(self.q("SELECT level FROM user_state")[0]["level"], 1)  # ниже 1 нельзя
        self.post("/level", {"direction": "up"}, csrf_from="/me")
        self.assertEqual(self.q("SELECT level FROM user_state")[0]["level"], 2)
        self.assertEqual(self.q("SELECT reason FROM scaffold_changes")[0]["reason"], "user_up")
        self.post("/level", {"direction": "up"}, csrf_from="/me")
        self.assertEqual(self.q("SELECT level FROM user_state")[0]["level"], 2)
        self.assertIn("раз в неделю", self.page("/me"))

    def test_summary_short_and_full(self):
        self.assertIn("слишком мало для выводов", self.page("/summary"))
        for _ in range(3):
            self.run_session(plan=True)
        page = self.page("/summary")
        self.assertIn("За месяц: 3 тренировки, из них со сдвигом — 3.", page)
        self.assertIn("Оператор преобразования — 3", page)
        self.post("/summary", {"action": "useful", "text": "увидел свои скидки"})
        self.post("/summary", {"action": "stop", "reason": "Дорого"})
        events = {r["name"]: json.loads(r["props_json"] or "{}") for r in self.q("SELECT name, props_json FROM events")}
        self.assertEqual(events["summary_feedback"]["text"], "увидел свои скидки")
        self.assertEqual(events["churned"]["reason"], "Дорого")

    def test_entry_bridge_on_me(self):
        self.execute("INSERT INTO attempts (user_id, task_id, step, started_at, completed_at, result_json) "
                     "VALUES (1, 'coffee_v1', 'done', ?, ?, '{}')", (ago(1), ago(1)))
        page = self.page("/me")
        self.assertIn("Это была чужая задача", page)
        self.assertIn("src=test", page)


if __name__ == "__main__":
    unittest.main()
