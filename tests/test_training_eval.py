import unittest

import llm
from helpers import TrainingCase


class EvalNoAITest(TrainingCase):
    def test_false_shift_and_improve_once(self):
        """Та же «выгода» в новой форме: тип «новая по форме», доработка — один раз, версии растут (C1 §2, §7)."""
        sid = self.to_eval(final="Скидка 30% новым клиентам")
        ev = self.session_json(sid, "eval_json")
        self.assertEqual((ev["family"], ev["shift"], ev["shift_kind"]), ("F07", False, "форма"))
        self.assertEqual((ev["novelty"], ev["type_ui"], ev["improve"]), ("низкая", "UI-198", "mechanism"))
        page = self.page(f"/s/{sid}/eval")
        self.assertIn("Новая по форме, старая по механизму", page)
        self.assertIn("Доработать слабую сторону", page)
        self.post(f"/s/{sid}/eval", {"action": "improve"})
        self.assertEqual(self.state(sid), "idea")
        self.assertIsNone(self.session_json(sid, "eval_json"))
        page = self.page(f"/s/{sid}/idea")
        self.assertIn("Доработка:", page)
        self.assertIn("Скидка 30% новым клиентам", page)  # доработка начинается с текста прошлой версии
        self.submit_final(sid, "Стенд на книжной ярмарке")
        versions = self.q("SELECT version, stage FROM ideas WHERE stage IN ('version', 'final') ORDER BY version")
        self.assertEqual([(v["version"], v["stage"]) for v in versions], [(1, "version"), (2, "final")])
        ev = self.session_json(sid, "eval_json") or {}
        self.page(f"/s/{sid}/eval")
        ev = self.session_json(sid, "eval_json")
        self.assertEqual((ev["versions"], ev["shift_kind"]), (2, "механизм"))
        self.assertNotIn("Доработать слабую сторону", self.page(f"/s/{sid}/eval"))  # один раз за сессию
        self.assertEqual(self.post(f"/s/{sid}/eval", {"action": "improve"}).status_code, 400)

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
        self.submit_final(sid, hint)
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
