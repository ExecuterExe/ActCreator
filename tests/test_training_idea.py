import json
import random
import unittest

import engine
import library
import llm
from helpers import TrainingCase


class IdeaStepTest(TrainingCase):
    def test_draft_then_submit(self):
        sid = self.to_idea()
        page = self.page(f"/s/{sid}/idea")
        self.assertIn("что именно делаем, для кого", page)
        self.assertIn("осталось 3", page)
        r = self.idea(sid, "submit", "")
        self.assertIn("пустую отправить нельзя", r.get_data(as_text=True))
        self.idea(sid, "draft", "Рисовать обложки")
        self.idea(sid, "draft", "Рисовать обложки")  # повтор не дублируется
        self.assertEqual(len(self.q("SELECT * FROM ideas WHERE stage = 'draft'")), 1)
        self.assertIn("Рисовать обложки", self.page(f"/s/{sid}/idea"))
        final = "Попросить авторов издательства рекомендовать меня своим редакторам"
        self.idea(sid, "submit", final)
        self.assertEqual(self.state(sid), "eval")
        rows = self.q("SELECT * FROM ideas WHERE stage = 'final'")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["text"], final)
        self.assertIsNotNone(rows[0]["class_source"])
        names = [r["name"] for r in self.q("SELECT name FROM events")]
        self.assertIn("idea_submitted", names)
        # шаг пройден — повторно отправить нельзя
        self.assertEqual(self.c.get(f"/s/{sid}/idea").status_code, 302)

    def test_too_long_rejected(self):
        sid = self.to_idea()
        r = self.idea(sid, "submit", "а" * 601)
        self.assertIn("длиннее 600", r.get_data(as_text=True))
        self.assertEqual(self.state(sid), "idea")

    def test_three_hints_then_budget_over(self):
        sid = self.to_idea()
        for kind in ("question", "transfer", "fragment"):
            self.post(f"/s/{sid}/idea", {"hint": kind, "idea": "черновик " + kind})
        rows = self.q("SELECT * FROM hints ORDER BY id")
        self.assertEqual([r["kind"] for r in rows], ["question", "transfer", "fragment"])
        for r in rows:
            self.assertEqual(r["source"], "template")
            self.assertNotRegex(r["text"], r"\[[^\]]+\]")
        shown = {r["item_id"] for r in self.q("SELECT item_id FROM shown_items WHERE lib = 'LIB-08'")}
        self.assertEqual(shown, {r["lib_id"] for r in rows})
        page = self.page(f"/s/{sid}/idea")
        self.assertIn("осталось 0", page)
        self.assertIn("<b>Вопрос:</b>", page)
        self.assertIn("<b>Фрагмент:</b>", page)
        self.post(f"/s/{sid}/idea", {"hint": "question", "idea": ""})
        self.assertEqual(len(self.q("SELECT * FROM hints")), 3)
        self.assertGreaterEqual(len(self.q("SELECT * FROM ideas WHERE stage = 'draft'")), 3)

    def test_bad_hint_kind(self):
        sid = self.to_idea()
        self.assertEqual(self.post(f"/s/{sid}/idea", {"hint": "answer"}).status_code, 400)

    def test_level3_budget_two(self):
        sid = self.to_tool(level=3)
        self.post(f"/s/{sid}/tool", {"action": "choose", "tool": "operator", "self_diag": "Одна колея"})
        tool = self.session_json(sid, "tool_json")
        if tool["phase"] == "target":
            self.post(f"/s/{sid}/tool", {"action": "target", "target": tool["targets"][0]})
        self.post(f"/s/{sid}/tool", {"action": "go"})
        self.assertIn("осталось 2", self.page(f"/s/{sid}/idea"))
        for kind in ("question", "transfer", "fragment"):
            self.post(f"/s/{sid}/idea", {"hint": kind})
        self.assertEqual(len(self.q("SELECT * FROM hints")), 2)

    def test_give_answer_refused(self):
        sid = self.to_idea()
        r = self.idea(sid, "give_answer")
        body = r.get_data(as_text=True)
        self.assertIn("Здесь вы придумываете сами", body)
        self.assertIn("Что можно сделать прямо сейчас", body)
        r = self.idea(sid, "submit", "Просто дай мне готовые идеи")
        self.assertIn("Здесь вы придумываете сами", r.get_data(as_text=True))
        self.assertEqual(self.state(sid), "idea")
        self.assertEqual(len(self.q("SELECT * FROM ideas WHERE stage = 'final'")), 0)
        events = self.q("SELECT props_json FROM events WHERE name = 'wants_generation'")
        self.assertEqual(len(events), 2)
        self.assertEqual(self.session_json(sid, "flags_json")["wants_generation"], 2)

    def test_several_ideas_choose_one(self):
        sid = self.to_idea()
        self.idea(sid, "submit", "Мои варианты:\n1. Вести блог об иллюстрации\n2. Договориться с авторами о рекомендациях")
        self.assertEqual(self.state(sid), "idea")
        page = self.page(f"/s/{sid}/idea")
        self.assertIn("Какую проверим сегодня?", page)
        r = self.idea(sid, "pick", chosen="7")
        self.assertIn("Выберите одну идею", r.get_data(as_text=True))
        self.idea(sid, "pick", chosen="1")
        self.assertEqual(self.state(sid), "eval")
        final = self.q("SELECT text FROM ideas WHERE stage = 'final'")[0]["text"]
        self.assertEqual(final, "Договориться с авторами о рекомендациях")
        extra = self.q("SELECT text, status FROM ideas WHERE stage = 'extra'")
        self.assertEqual([(e["text"], e["status"]) for e in extra], [("Вести блог об иллюстрации", "дополнительная")])

    def test_self_check_for_chain(self):
        sid = self.to_idea("Д3", "chain")
        page = self.page(f"/s/{sid}/idea")
        self.assertIn("опирается на то же, от чего мы отказались", page)
        r = self.idea(sid, "submit", "Сделать портфолио")
        self.assertIn("Ответьте на вопрос самопроверки", r.get_data(as_text=True))
        r = self.idea(sid, "submit", "Сделать портфолио", self_check="yes")
        self.assertIn("Отправить как есть", r.get_data(as_text=True))
        self.assertEqual(self.state(sid), "idea")
        self.idea(sid, "submit", "Сделать портфолио", self_check="yes", confirm="1")
        self.assertEqual(self.state(sid), "eval")
        self.assertEqual(self.session_json(sid, "flags_json")["self_check"], "yes")

    def test_no_self_check_for_operator(self):
        sid = self.to_idea()
        self.assertNotIn("опирается на то же", self.page(f"/s/{sid}/idea"))

    def test_soften_and_change_from_idea(self):
        sid = self.to_idea()
        self.idea(sid, "soften", "черновик")
        self.assertTrue(self.session_json(sid, "flags_json")["softened"])
        self.assertEqual(self.idea(sid, "soften").status_code, 400)
        self.idea(sid, "change", "черновик 2")
        self.assertEqual(self.state(sid), "tool")
        self.assertEqual(self.session_json(sid, "tool_json")["type"], "constraint")
        self.post(f"/s/{sid}/tool", {"action": "go"})
        self.assertEqual(self.state(sid), "idea")
        page = self.page(f"/s/{sid}/idea")
        self.assertNotIn("Хочу другой инструмент", page)
        self.assertIn("черновик 2", page)  # черновик не потерялся


def fake_ai(system, user):
    if "Оцени формулировку" in system:
        return '{"problem": "none", "in_domain": true}'
    if "Дай ОДНУ подсказку" in system:
        if "фрагмент" not in system and "начало мысли" not in system:
            return '{"hint": "Кто из окружения издательства первым узнаёт, что им нужен иллюстратор?"}'
        return '{"hint": "Сделайте скидку 10% для новых клиентов издательства."}'  # готовая идея → отвергнуть
    return "{}"


class IdeaWithAITest(TrainingCase):
    ai_client = llm.FakeClient(fake_ai)

    def test_ai_hint_and_leak_rejected(self):
        sid = self.to_tool()
        self.page(f"/s/{sid}/tool")
        self.post(f"/s/{sid}/tool", {"action": "go"})
        self.post(f"/s/{sid}/idea", {"hint": "question"})
        self.post(f"/s/{sid}/idea", {"hint": "fragment"})
        rows = self.q("SELECT kind, source, text FROM hints ORDER BY id")
        self.assertEqual((rows[0]["source"], rows[0]["text"][:3]), ("ai", "Кто"))
        self.assertEqual(rows[1]["source"], "template")
        self.assertNotIn("скидку", rows[1]["text"])


class HintEngineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lib = library.Library(library.DEFAULT_DIR)

    def test_split_ideas(self):
        self.assertEqual(engine.split_ideas("Одна идея\nс переносом строки"), ["Одна идея\nс переносом строки"])
        self.assertEqual(engine.split_ideas("- первая\n- вторая"), ["первая", "вторая"])
        self.assertEqual(engine.split_ideas("1) раз\n2) два\n3) три"), ["раз", "два", "три"])
        self.assertEqual(engine.split_ideas("1. только одна"), ["1. только одна"])

    def test_budget(self):
        self.assertEqual([engine.hint_budget(lv) for lv in (1, 2, 3)], [3, 3, 2])

    def test_pick_hint_fills_slots_and_prefers_tool(self):
        tool = {"type": "chain", "assumption": "Клиент выбирает по цене"}
        slots = {"цель": "«3 заказа»", "допущение": "«Клиент выбирает по цене»", "механизм фаворита": "«Выгода»"}
        tpl, text = engine.pick_hint(self.lib, "question", tool, "найти клиентов", slots, set(), random.Random(1))
        self.assertEqual(tpl["for"], "chain")
        self.assertNotRegex(text, r"\[[^\]]+\]")

    def test_pick_hint_skips_shown_and_unfillable(self):
        tool = {"type": "constraint", "ban": "без соцсетей"}
        shown = set()
        for _ in range(5):
            tpl, text = engine.pick_hint(self.lib, "question", tool, "", {"запрет": "соцсетей"}, shown, random.Random(2))
            self.assertNotIn(tpl["id"], shown)
            self.assertNotRegex(text, r"\[[^\]]+\]")
            shown.add(tpl["id"])

    def test_transfer_by_function(self):
        tpl, _ = engine.pick_hint(self.lib, "transfer", {"type": "operator"}, "вернуть постоянных клиентов", {},
                                  set(), random.Random(3))
        self.assertIn(tpl["function"], ("Возврат клиента", "Первый визит без рекламы"))

    def test_fragment_group_for_constraint(self):
        tpl, _ = engine.pick_hint(self.lib, "fragment", {"type": "constraint"}, "", {}, set(), random.Random(4))
        self.assertEqual(tpl["group"], "Ограничение как ресурс")


if __name__ == "__main__":
    unittest.main()
