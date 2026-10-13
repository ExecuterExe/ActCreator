import json
import unittest
from datetime import date, timedelta

import llm
from helpers import TrainingCase


class RouteAndCheckTest(TrainingCase):
    def to_route(self, **kw):
        sid = self.to_eval(final="Стенд на книжной ярмарке", **kw)
        self.post(f"/s/{sid}/eval", {"action": "next"})
        self.post(f"/s/{sid}/premortem", {"action": "reasons", "reason1": "Редакторы не придут на стенд"})
        rows = self.q("SELECT id FROM objections WHERE author = 'critic'")
        data = {"action": "defend"}
        for o in rows:
            data.update({f"about_{o['id']}": "1", f"type_{o['id']}": "agree"})
        self.post(f"/s/{sid}/premortem", data)
        self.post(f"/s/{sid}/premortem", {"action": "finish"})
        self.assertEqual(self.state(sid), "route")
        return sid

    def test_route_text_and_choice(self):
        sid = self.to_route()
        page = self.page(f"/s/{sid}/route")
        self.assertIn("Начали с «Материальная выгода» → все идеи шли одной колеёй → «Оператор преобразования»", page)
        self.assertIn("Самое сильное открытое возражение — «не сработает, если", page)
        self.assertIn("Что помогло больше всего?", page)
        r = self.post(f"/s/{sid}/route", {})
        self.assertIn("Выберите один вариант", r.get_data(as_text=True))
        self.post(f"/s/{sid}/route", {"helped": "mirror", "wow": "Нет", "correction": "помогло увидеть скидки"})
        self.assertEqual(self.state(sid), "check")
        route = self.session_json(sid, "route_json")
        self.assertEqual((route["helped"], route["correction"]), ("mirror", "помогло увидеть скидки"))

    def test_check_plan_and_completion(self):
        sid = self.to_route()
        self.post(f"/s/{sid}/route", {"helped": "tool", "wow": "Скорее нет"})
        page = self.page(f"/s/{sid}/check")
        self.assertIn("Самый дешёвый способ проверить «не сработает, если", page)
        plan = self.session_json(sid, "flags_json")["check_plan"]
        strongest = self.session_json(sid, "flags_json")["strongest"]
        self.assertEqual(plan["objection"]["id"], strongest)
        r = self.post(f"/s/{sid}/check", {"action": "save", "plan": "Позвонить организаторам", "ok": "", "fail": ""})
        body = r.get_data(as_text=True)
        self.assertIn("Выберите срок", body)
        self.assertIn("Запишите оба сигнала", body)
        self.post(f"/s/{sid}/check", {"action": "save", "plan": "Позвонить организаторам ярмарки", "when": "завтра",
                                      "ok": "есть бесплатное место", "fail": "только платные стенды"})
        self.assertEqual(self.state(sid), "done")
        exp = self.q("SELECT * FROM experiments")[0]
        self.assertEqual(exp["due"], (date.today() + timedelta(days=1)).isoformat())
        self.assertEqual(exp["objection_id"], strongest)
        statuses = {r["stage"]: r["status"] for r in self.q("SELECT stage, status FROM ideas")}
        self.assertEqual(statuses["final"], "к проверке")
        self.assertEqual(statuses["first"], "первая")
        self.assertEqual(statuses["draft"], "черновик")
        page = self.page(f"/s/{sid}")
        self.assertIn("Сессия завершена", page)
        self.assertIn("Через 2 дня мы спросим, что получилось", page)
        names = [r["name"] for r in self.q("SELECT name FROM events")]
        self.assertIn("session_completed", names)
        self.assertIsNotNone(self.q("SELECT completed_at FROM sessions")[0]["completed_at"])
        # завершённую сессию не открыть на прошлом шаге и не «отправить» повторно
        self.assertEqual(self.c.get(f"/s/{sid}/check").status_code, 302)
        self.assertEqual(self.post(f"/s/{sid}/done", {}).status_code, 405)

    def test_check_skip_after_premortem_skip(self):
        sid = self.to_eval()
        self.post(f"/s/{sid}/eval", {"action": "take"})
        self.post(f"/s/{sid}/eval", {"action": "next"})
        self.post(f"/s/{sid}/premortem", {"action": "skip"})
        self.assertIn("проверку на прочность пропустили", self.page(f"/s/{sid}/route"))
        self.post(f"/s/{sid}/route", {"helped": "nothing", "wow": "Да"})
        self.assertIn("Самый дешёвый способ проверить", self.page(f"/s/{sid}/check"))
        self.post(f"/s/{sid}/check", {"action": "skip"})
        self.assertEqual(self.state(sid), "done")
        self.assertEqual(self.q("SELECT status FROM ideas WHERE stage = 'final'")[0]["status"], "в работе")
        self.assertEqual(len(self.q("SELECT * FROM experiments")), 0)


class CompletionEffectsTest(TrainingCase):
    def setUp(self):
        super().setUp()
        self.register()
        self.fill_card()

    def strengths(self):
        return json.loads(self.q("SELECT strengths_json FROM user_state")[0]["strengths_json"] or "{}")

    def level(self):
        return self.q("SELECT level FROM user_state")[0]["level"]

    def test_softened_lowers_strength(self):
        self.run_session(soften=True)
        self.assertEqual(self.strengths()["operator"], 1)

    def test_too_weak_raises_strength(self):
        # финал снова в «Скидках», как фаворит → в следующий раз сила оператора +1
        self.run_session(final="Скидка 30% новым клиентам")
        self.assertEqual(self.strengths()["operator"], 3)

    def test_level_up_after_three_shifts_and_down_after_two_misses(self):
        for _ in range(3):
            self.run_session(final="Стенд на книжной ярмарке")
        self.assertEqual(self.level(), 2)
        change = self.q("SELECT * FROM scaffold_changes")
        self.assertEqual([(c["from_level"], c["to_level"], c["reason"]) for c in change], [(1, 2, "rule_up")])
        self.assertIn("Теперь вы будете сами выбирать инструмент", self.page("/train"))
        # уровень 2: две сессии подряд без сдвига → назад на 1
        sid = self.run_session(final="Скидка 30% новым клиентам")
        self.assertEqual(self.q("SELECT level FROM sessions WHERE id = ?", (sid,))[0]["level"], 2)
        self.assertEqual(self.level(), 2)
        self.run_session(final="Скидка 40% постоянным")
        self.assertEqual(self.level(), 1)
        self.assertEqual(self.q("SELECT reason FROM scaffold_changes ORDER BY id DESC")[0]["reason"], "rule_down")

    def test_hints_block_level_up(self):
        for _ in range(3):
            self.run_session(final="Стенд на книжной ярмарке", hints=2)
        self.assertEqual(self.level(), 1)

    def test_helped_history_from_third_session(self):
        self.run_session(helped="tool")
        self.run_session(helped="tool")
        sid = self.start()
        self.set_task(sid)
        self.submit_first(sid, ["Скидка 10%", "Скидка 20%"], 1)
        self.page(f"/s/{sid}/mirror")
        self.post(f"/s/{sid}/mirror", {"action": "continue"})
        self.page(f"/s/{sid}/tool")
        self.post(f"/s/{sid}/tool", {"action": "go"})
        self.submit_final(sid, "Стенд на книжной ярмарке")
        self.page(f"/s/{sid}/eval")
        self.post(f"/s/{sid}/eval", {"action": "next"})
        self.post(f"/s/{sid}/premortem", {"action": "skip"})
        self.assertIn("В прошлые разы вам чаще помогало: сам инструмент", self.page(f"/s/{sid}/route"))


def fake_ai(system, user):
    if "Оцени формулировку" in system:
        return '{"problem": "none", "in_domain": true}'
    if "Перепиши шаблон" in system and "проверка за 48 часов" in user:
        return '{"task_text": "Оплатить 500 ₽ за объявление в чате авторов и посчитать отклики."}'
    return "{}"


class CheckWithAITest(TrainingCase):
    ai_client = llm.FakeClient(fake_ai)

    def test_adapted_check_and_paid_warning(self):
        sid = self.to_eval()
        self.post(f"/s/{sid}/eval", {"action": "next"})
        self.post(f"/s/{sid}/premortem", {"action": "skip"})
        self.post(f"/s/{sid}/route", {"helped": "tool", "wow": "Скорее нет"})
        page = self.page(f"/s/{sid}/check")
        plan = self.session_json(sid, "flags_json")["check_plan"]
        self.assertEqual(plan["source"], "ai")
        self.assertTrue(plan["paid"])
        self.assertIn("Эту проверку не сделать без затрат", page)
        n = len([c for c in self.ai_client.calls if "проверка за 48 часов" in c[1]])
        self.page(f"/s/{sid}/check")
        self.assertEqual(n, len([c for c in self.ai_client.calls if "проверка за 48 часов" in c[1]]))


class JournalTest(TrainingCase):
    """Лабораторный журнал идеи: только для завершённой сессии и только её владельцу."""

    def setUp(self):
        super().setUp()
        self.register()
        self.fill_card()

    def test_unfinished_session_redirects_to_its_step(self):
        sid = self.start()
        self.set_task(sid)
        r = self.c.get(f"/s/{sid}/journal")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith(f"/s/{sid}"))

    def test_journal_collects_the_session(self):
        sid = self.run_session(final="Стенд на книжной ярмарке", plan=True)
        self.assertIn(f"/s/{sid}/journal", self.page(f"/s/{sid}"))
        journal = self.page(f"/s/{sid}/journal")
        for text in ("Стенд на книжной ярмарке", "Скидка 10% на первый заказ", "Что меняли",
                     "Проверку на прочность пропустили.", "Написать 3 редакторам", "Оценки предварительные."):
            self.assertIn(text, journal)
        self.assertEqual(self.q("SELECT COUNT(*) AS n FROM events WHERE name = 'journal_opened'")[0]["n"], 1)

    def test_other_user_cannot_open_journal(self):
        sid = self.run_session()
        self.c = self.app.test_client()
        self.register("user2")
        self.assertEqual(self.c.get(f"/s/{sid}/journal").status_code, 404)


if __name__ == "__main__":
    unittest.main()
