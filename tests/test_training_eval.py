import unittest

import llm
from helpers import TrainingCase


class EvalNoAITest(TrainingCase):
    def test_no_shift_retry_once(self):
        sid = self.to_eval(final="Скидка 30% новым клиентам")
        ev = self.session_json(sid, "eval_json")
        self.assertEqual((ev["family"], ev["shift"], ev["rare"] is not None), ("F07", False, True))
        page = self.page(f"/s/{sid}/eval")
        self.assertIn("Сдвига пока нет: это снова", page)
        self.assertIn("Ещё 3 минуты", page)
        self.post(f"/s/{sid}/eval", {"action": "retry"})
        self.assertEqual(self.state(sid), "idea")
        stages = {r["stage"] for r in self.q("SELECT stage FROM ideas WHERE text = 'Скидка 30% новым клиентам'")}
        self.assertEqual(stages, {"draft", "retry"})
        self.assertIsNone(self.session_json(sid, "eval_json"))
        self.idea(sid, "submit", "Скидка 40% постоянным")
        page = self.page(f"/s/{sid}/eval")
        self.assertNotIn("Ещё 3 минуты", page)  # повтор — один раз
        self.assertEqual(self.post(f"/s/{sid}/eval", {"action": "retry"}).status_code, 400)
        self.assertEqual(len(self.q("SELECT * FROM ideas WHERE stage = 'final'")), 1)

    def test_shift_and_rules_rubric(self):
        sid = self.to_eval()
        ev = self.session_json(sid, "eval_json")
        final = self.q("SELECT family FROM ideas WHERE stage = 'final'")[0]["family"]
        if final:
            self.assertEqual(ev["shift"], final != "F07")
        else:
            self.assertIsNone(ev["shift"])
        self.assertEqual(ev["rubric_source"], "rules")
        self.assertTrue(set(ev["rubric"]) <= {"RB2"})
        self.assertGreaterEqual(ev["flexibility"], 1)
        page = self.page(f"/s/{sid}/eval")
        self.assertIn("ИИ не подключён", page)
        self.assertIn("За сессию вы рассмотрели", page)
        self.assertIn("Подсказок: 0. Идея ваша.", page)
        names = [r["name"] for r in self.q("SELECT name FROM events")]
        self.assertIn("evaluated", names)

    def test_resources_conflict_shown(self):
        # стенд на ярмарке (F11.3) при бюджете 0 ₽ не укладывается в карточку (правило П1)
        sid = self.to_eval(final="Стенд на книжной ярмарке")
        ev = self.session_json(sid, "eval_json")
        self.assertEqual(ev["rubric"]["RB2"]["level"], 1)
        self.assertIn("Ресурсы: не укладывается — нужны деньги, а бюджета почти нет (оценка по типу решения)",
                      self.page(f"/s/{sid}/eval"))

    def test_take_feedback_and_next(self):
        sid = self.to_eval()
        self.post(f"/s/{sid}/eval", {"action": "take"})
        self.assertEqual(self.q("SELECT status FROM ideas WHERE stage = 'final'")[0]["status"], "в работе")
        self.assertIn("Идея отмечена: в работе", self.page(f"/s/{sid}/eval"))
        self.post(f"/s/{sid}/eval", {"action": "too_easy"})
        self.assertEqual(self.post(f"/s/{sid}/eval", {"action": "too_hard"}).status_code, 400)
        self.assertEqual(self.session_json(sid, "eval_json")["feedback"], "too_easy")
        self.post(f"/s/{sid}/eval", {"action": "next"})
        self.assertEqual(self.state(sid), "premortem")

    def test_authorship_from_hint(self):
        sid = self.to_idea()
        self.post(f"/s/{sid}/idea", {"hint": "fragment"})
        hint = self.q("SELECT text FROM hints")[0]["text"]
        self.idea(sid, "submit", hint)
        page = self.page(f"/s/{sid}/eval")
        ev = self.session_json(sid, "eval_json")
        self.assertEqual(ev["hints"], 1)
        self.assertLess(ev["authorship"], 0.5)
        self.assertIn("Идея во многом повторяет подсказку", page)

    def test_practice_skips_premortem_unless_enabled(self):
        sid = self.to_eval(mode="practice")
        self.assertIn("проверка на прочность выключена", self.page(f"/s/{sid}/eval"))
        self.post(f"/s/{sid}/eval", {"action": "next"})
        self.assertEqual(self.state(sid), "route")
        names = [r["name"] for r in self.q("SELECT name FROM events")]
        self.assertIn("premortem_skipped", names)

    def test_practice_premortem_enabled(self):
        sid = self.to_eval(mode="practice")
        self.post(f"/s/{sid}/eval", {"action": "next", "premortem_on": "1"})
        self.assertEqual(self.state(sid), "premortem")


def fake_ai(system, user):
    if "Оцени формулировку" in system:
        return '{"problem": "none", "in_domain": true}'
    if "Оцени идею по рубрикам" in system:
        return ('{"criteria": [{"id": "RB1", "level": 3, "reason": "прямо ведёт к заказам"},'
                '{"id": "RB2", "level": 2, "reason": "нужно ~10 часов при 2–5 в неделю"},'
                '{"id": "RB3", "level": 1, "reason": "непонятно, с кого начать"},'
                '{"id": "RB4", "level": 2, "reason": "причина угадывается"}]}')
    return "{}"


class EvalWithAITest(TrainingCase):
    ai_client = llm.FakeClient(fake_ai)

    def test_ai_rubric_lines(self):
        sid = self.to_eval()
        ev = self.session_json(sid, "eval_json")
        self.assertEqual(ev["rubric_source"], "ai")
        page = self.page(f"/s/{sid}/eval")
        self.assertIn("Связь с целью: прямая.", page)
        self.assertIn("Ресурсы: на пределе — нужно ~10 часов", page)
        self.assertIn("Первый шаг: пока размыт — уточним на этапе проверки.", page)
        self.assertIn("Механизм: частично.", page)
        self.assertIn("Оценка полезности предварительная", page)
        # рубрика считается один раз, повторное открытие не вызывает ИИ заново
        calls = len([c for c in self.ai_client.calls if "Оцени идею по рубрикам" in c[0]])
        self.page(f"/s/{sid}/eval")
        self.assertEqual(calls, len([c for c in self.ai_client.calls if "Оцени идею по рубрикам" in c[0]]))


if __name__ == "__main__":
    unittest.main()
