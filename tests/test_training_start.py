import json
import unittest

import llm
from helpers import TrainingCase


class StartTest(TrainingCase):
    def test_requires_login(self):
        r = self.c.get("/train")
        self.assertIn("/login", r.headers["Location"])

    def test_card_required_then_start(self):
        self.register()
        r = self.c.get("/train")
        self.assertIn("/train/card", r.headers["Location"])
        r = self.post("/train/card?next=/train", {"R02": "2–5 ч"})
        self.assertIn("Выберите вариант", r.get_data(as_text=True))
        r = self.fill_card()
        self.assertEqual(r.status_code, 302)
        card = json.loads(self.q("SELECT card_json FROM user_state")[0]["card_json"])
        self.assertEqual(card["R01"], "0 ₽")
        self.assertEqual(card["R10"], "посты в соцсетях — не помогли")
        self.assertIn("Как будем тренироваться сегодня", self.page("/train"))

    def test_full_card_has_all_fields(self):
        self.register()
        page = self.page("/train/card?kind=full")
        for label in ("Что вы умеете сами", "Чего вы точно не хотите делать", "Где вы работаете"):
            self.assertIn(label, page)
        self.fill_card(kind="full", R05=["писать тексты"], R15="только онлайн")
        card = json.loads(self.q("SELECT card_json FROM user_state")[0]["card_json"])
        self.assertEqual(card["R05"], ["писать тексты"])
        self.assertEqual(card["_kind"], "full")

    def test_task_validation_and_warning(self):
        self.register()
        self.fill_card()
        sid = self.start()
        self.assertEqual(self.state(sid), "task")
        r = self.set_task(sid, text="коротко")
        self.assertIn("от 20 до 600", r.get_data(as_text=True))
        # без цифр и сроков — предупреждение «слишком общая», но можно продолжить
        r = self.set_task(sid, goal="стать успешнее и известнее")
        self.assertIn("слишком общая", r.get_data(as_text=True))
        self.assertEqual(self.state(sid), "task")
        r = self.set_task(sid, goal="стать успешнее и известнее", confirm="1")
        self.assertEqual(self.state(sid), "first")

    def test_task_ok_moves_to_first_and_cannot_skip(self):
        self.register()
        self.fill_card()
        sid = self.start()
        self.set_task(sid)
        self.assertEqual(self.state(sid), "first")
        r = self.c.get(f"/s/{sid}/eval")
        self.assertIn(f"/s/{sid}/first", r.headers["Location"])
        task = json.loads(self.q("SELECT task_json FROM sessions")[0]["task_json"])
        self.assertEqual(task["goal"], "3 новых заказа за месяц")

    def test_out_of_domain_without_ai_low_precision(self):
        self.register()
        self.fill_card()
        sid = self.start()
        self.set_task(sid, in_domain="no", text="Как сделать курс по истории интереснее для студентов колледжа?")
        task = json.loads(self.q("SELECT task_json FROM sessions")[0]["task_json"])
        self.assertTrue(task["low_precision"])

    def test_practice_mode_uses_library_task_and_legend(self):
        self.register()
        self.fill_card()
        sid = self.start("practice")
        s = self.q("SELECT task_json, card_json FROM sessions WHERE id = ?", (sid,))[0]
        task = json.loads(s["task_json"])
        self.assertTrue(task["practice_id"].startswith("T"))
        self.assertIn("R01", json.loads(s["card_json"]))
        self.post(f"/s/{sid}/task", {})
        self.assertEqual(self.state(sid), "first")

    def test_other_user_cannot_open_session(self):
        self.register("a1")
        self.fill_card()
        sid = self.start()
        self.post("/logout", {}, csrf_from="/train")
        self.register("b2")
        self.assertEqual(self.c.get(f"/s/{sid}/task").status_code, 404)


class TrialTest(TrainingCase):
    full_access = False

    def test_third_session_goes_to_paywall(self):
        self.register()
        self.fill_card()
        self.assertIsNotNone(self.start())
        self.assertIsNotNone(self.start())
        r = self.post("/train/start", {"mode": "task"}, csrf_from="/train")
        self.assertIn("/paywall", r.headers["Location"])
        page = self.page("/paywall")
        self.assertIn("Пробные тренировки закончились", page)
        r = self.post("/paywall", {}, csrf_from="/paywall")
        self.assertIn("Платная версия пока готовится", r.get_data(as_text=True))
        self.assertEqual(len(self.q("SELECT * FROM events WHERE name = 'paywall_click'")), 1)


class AITaskCheckTest(TrainingCase):
    ai_client = llm.FakeClient(lambda s, u: '{"problem": "unethical", "in_domain": true}')

    def test_unethical_blocked_and_ai_notice_required(self):
        self.register()
        r = self.post("/train/card?next=/train", {"R01": "0 ₽", "R02": "2–5 ч"})
        self.assertIn("Подтвердите", r.get_data(as_text=True))
        self.fill_card()
        sid = self.start()
        r = self.set_task(sid)
        self.assertIn("может навредить", r.get_data(as_text=True))
        self.assertEqual(self.state(sid), "task")


if __name__ == "__main__":
    unittest.main()
