import json
import re
import unittest

import llm
from helpers import TrainingCase


class ToolLevel1Test(TrainingCase):
    """Уровень 1: инструмент выдаёт система (B2 §4.5, B3)."""

    def test_one_rut_gives_operator_and_go_to_idea(self):
        sid = self.to_tool()
        self.assertEqual(self.state(sid), "tool")
        page = self.page(f"/s/{sid}/tool")
        self.assertIn("Оператор преобразования", page)
        self.assertIn("Все ваши идеи работают через одно и то же", page)
        self.assertNotIn("Д1:", page)
        tool = self.session_json(sid, "tool_json")
        self.assertEqual((tool["type"], tool["phase"]), ("operator", "ready"))
        self.assertTrue(tool["task_text"])
        self.assertNotRegex(tool["task_text"], r"\[клиент")
        shown = self.q("SELECT lib, item_id FROM shown_items WHERE session_id = ?", (sid,))
        self.assertIn(("LIB-06", tool["item_id"]), [(r["lib"], r["item_id"]) for r in shown])
        # идти дальше нельзя, пока шаг не пройден
        self.assertEqual(self.c.get(f"/s/{sid}/idea").status_code, 302)
        self.post(f"/s/{sid}/tool", {"action": "go"})
        self.assertEqual(self.state(sid), "idea")
        names = [r["name"] for r in self.q("SELECT name FROM events")]
        self.assertIn("tool_shown", names)

    def test_tool_is_stable_on_reload(self):
        sid = self.to_tool()
        self.page(f"/s/{sid}/tool")
        first = self.session_json(sid, "tool_json")
        self.page(f"/s/{sid}/tool")
        self.assertEqual(first, self.session_json(sid, "tool_json"))

    def test_soften_only_once(self):
        sid = self.to_tool()
        self.page(f"/s/{sid}/tool")
        before = self.session_json(sid, "tool_json")
        self.post(f"/s/{sid}/tool", {"action": "soften"})
        after = self.session_json(sid, "tool_json")
        self.assertEqual(after["strength"], max(1, before["strength"] - 1))
        self.assertTrue(self.session_json(sid, "flags_json")["softened"])
        page = self.page(f"/s/{sid}/tool")
        self.assertIn("Задание облегчено", page)
        self.assertNotIn("Облегчить задание", page)
        r = self.post(f"/s/{sid}/tool", {"action": "soften"})
        self.assertEqual(r.status_code, 400)

    def test_change_tool_only_once(self):
        sid = self.to_tool()
        self.page(f"/s/{sid}/tool")
        self.post(f"/s/{sid}/tool", {"action": "change"})
        tool = self.session_json(sid, "tool_json")
        self.assertEqual(tool["type"], "constraint")  # первая альтернатива оператору
        self.assertTrue(self.session_json(sid, "diagnosis_json")["overridden_by_user"])
        self.assertNotIn("Хочу другой инструмент", self.page(f"/s/{sid}/tool"))
        self.assertEqual(self.post(f"/s/{sid}/tool", {"action": "change"}).status_code, 400)

    def test_unknown_action_rejected(self):
        sid = self.to_tool()
        self.page(f"/s/{sid}/tool")
        self.assertEqual(self.post(f"/s/{sid}/tool", {"action": "hack"}).status_code, 400)


class ToolTypesTest(TrainingCase):
    """Каждый из шести инструментов собирается без ИИ из библиотек."""

    def tool_for(self, code, ttype, ideas=None, favorite=1):
        sid = self.to_tool(**({"ideas": ideas} if ideas else {}), favorite=favorite)
        self.force_diagnosis(sid, code, ttype)
        page = self.page(f"/s/{sid}/tool")
        return sid, page, self.session_json(sid, "tool_json")

    def test_assumptions_select_and_own(self):
        sid, page, tool = self.tool_for("Д2", "assumptions")
        self.assertEqual(tool["phase"], "select")
        self.assertEqual(len(tool["items"]), 4)
        self.assertIn("Это про меня", page)
        r = self.post(f"/s/{sid}/tool", {"action": "select"})
        self.assertIn("Отметьте допущение", r.get_data(as_text=True))
        a = tool["items"][0]
        self.post(f"/s/{sid}/tool", {"action": "select", "chosen": a["id"], "about_me": [a["id"], tool["items"][1]["id"]]})
        tool = self.session_json(sid, "tool_json")
        self.assertEqual(tool["phase"], "ready")
        self.assertIn(a["text"], tool["task_text"])
        self.assertEqual(len(tool["about_me"]), 2)
        self.post(f"/s/{sid}/tool", {"action": "go"})
        self.assertEqual(self.state(sid), "idea")

    def test_assumptions_own_text(self):
        sid, _, tool = self.tool_for("Д0", "assumptions")
        self.post(f"/s/{sid}/tool", {"action": "select", "own": "Издательства не отвечают незнакомцам"})
        tool = self.session_json(sid, "tool_json")
        self.assertEqual(tool["chosen"]["id"], "own")
        self.assertIn("Издательства не отвечают незнакомцам", tool["task_text"])

    def test_go_blocked_until_ready(self):
        sid, _, tool = self.tool_for("Д2", "assumptions")
        self.assertEqual(self.post(f"/s/{sid}/tool", {"action": "go"}).status_code, 400)
        self.assertEqual(self.state(sid), "tool")

    def test_chain_uses_why_and_library(self):
        sid, page, tool = self.tool_for("Д3", "chain")
        self.assertEqual(tool["phase"], "ready")
        self.assertIn("Скидка 10% на первый заказ", tool["header"])
        self.assertTrue(tool["assumption"])
        self.assertNotIn("[цель]", tool["task_text"])
        self.assertIn("Это не то допущение", page)
        self.post(f"/s/{sid}/tool", {"action": "own_assumption", "own": "клиенту важна цена"})
        tool = self.session_json(sid, "tool_json")
        self.assertEqual(tool["source"], "user")
        self.assertIn("клиенту важна цена", tool["task_text"])

    def test_chain_asks_why_when_missing(self):
        sid, page, tool = self.tool_for("Д3", "chain")
        self.execute("UPDATE sessions SET flags_json = '{}', tool_json = NULL WHERE id = ?", (sid,))
        self.page(f"/s/{sid}/tool")
        tool = self.session_json(sid, "tool_json")
        self.assertEqual(tool["phase"], "why")
        r = self.post(f"/s/{sid}/tool", {"action": "why", "why": ""})
        self.assertIn("одной фразой", r.get_data(as_text=True))
        self.post(f"/s/{sid}/tool", {"action": "why", "why": "Людям нравится экономить"})
        tool = self.session_json(sid, "tool_json")
        self.assertEqual(tool["phase"], "ready")

    def test_constraint_text_has_no_codes(self):
        sid, page, tool = self.tool_for("Д—", "constraint")
        self.assertEqual(tool["type"], "constraint")
        self.assertNotRegex(tool["task_text"], r"\(F\d\d")
        self.assertTrue(tool["why"])
        self.assertTrue(re.match(r"^(Условие|Ограничение|Решите задачу для|Как эту задачу)", tool["task_text"]))

    def test_contradiction(self):
        sid, page, tool = self.tool_for("Д4", "contradiction")
        self.assertIn("Нужно:", tool["task_text"])
        self.assertIn("Найдите решение, в котором выполняются оба условия", tool["task_text"])
        self.post(f"/s/{sid}/tool", {"action": "soften"})
        self.assertIn("Облегчение:", self.session_json(sid, "tool_json")["task_text"])

    def test_crossing_falls_back_without_rare_pair(self):
        sid, page, tool = self.tool_for("Д5", "crossing")
        self.assertEqual(tool["type"], "operator")

    def test_crossing_with_two_rare_ideas(self):
        sid = self.to_tool(ideas=["Скидка новым клиентам", "Петь песни у входа"])
        tax_common = self.session_json(sid, "mirror_json")["common"]
        # две идеи из редких семейств: подставляем коды напрямую
        rare = [f for f in ["F12", "F14", "F13", "F11", "F15"] if f not in tax_common][:2]
        self.execute("UPDATE ideas SET family = ?, subfamily = ? WHERE ord = 1", (rare[0], rare[0] + ".1"))
        self.execute("UPDATE ideas SET family = ?, subfamily = ? WHERE ord = 2", (rare[1], rare[1] + ".1"))
        self.force_diagnosis(sid, "Д5", "crossing")
        page = self.page(f"/s/{sid}/tool")
        tool = self.session_json(sid, "tool_json")
        self.assertEqual(tool["type"], "crossing")
        self.assertNotIn("[мА]", tool["task_text"])
        self.assertIn("Идея A", page)


class ToolLevelsTest(TrainingCase):
    def test_level2_self_diag_then_choice(self):
        sid = self.to_tool(level=2)
        page = self.page(f"/s/{sid}/tool")
        self.assertIn("В чём вы, по-вашему, застряли?", page)
        self.assertNotIn("Наш диагноз", page)
        self.assertIsNone(self.session_json(sid, "tool_json"))
        r = self.post(f"/s/{sid}/tool", {"action": "self_diag"})
        self.assertIn("Выберите вариант", r.get_data(as_text=True))
        self.post(f"/s/{sid}/tool", {"action": "self_diag", "self_diag": "Одна колея"})
        d = self.session_json(sid, "diagnosis_json")
        self.assertEqual((d["self_diag"], d["match"]), ("Д1", True))
        page = self.page(f"/s/{sid}/tool")
        self.assertIn("Наш диагноз", page)
        self.assertIn("Операторы помогают", page)
        r = self.post(f"/s/{sid}/tool", {"action": "choose", "tool": "chain"})  # не из предложенных
        self.assertIn("Выберите инструмент", r.get_data(as_text=True))
        self.post(f"/s/{sid}/tool", {"action": "choose", "tool": "assumptions"})
        tool = self.session_json(sid, "tool_json")
        d = self.session_json(sid, "diagnosis_json")
        self.assertEqual(tool["type"], "assumptions")
        self.assertTrue(d["overridden_by_user"])
        self.assertEqual(d["tool_type"], "operator")  # рекомендация системы сохранена

    def test_level2_operator_target_choice(self):
        sid = self.to_tool(level=2)
        self.post(f"/s/{sid}/tool", {"action": "self_diag", "self_diag": "Не знаю"})
        self.assertEqual(self.session_json(sid, "diagnosis_json")["self_diag"], None)
        self.post(f"/s/{sid}/tool", {"action": "choose", "tool": "operator"})
        tool = self.session_json(sid, "tool_json")
        self.assertEqual(tool["phase"], "target")
        page = self.page(f"/s/{sid}/tool")
        self.assertIn("Выберите направление", page)
        r = self.post(f"/s/{sid}/tool", {"action": "target", "target": "F99"})
        self.assertIn("Выберите направление", r.get_data(as_text=True))
        target = tool["targets"][0]
        self.post(f"/s/{sid}/tool", {"action": "target", "target": target})
        tool = self.session_json(sid, "tool_json")
        self.assertEqual((tool["phase"], tool["target"]), ("ready", target))

    def test_level3_choose_then_see_diagnosis(self):
        sid = self.to_tool(level=3)
        page = self.page(f"/s/{sid}/tool")
        self.assertIn("Выберите инструмент сами", page)
        self.assertNotIn("Все ваши идеи работают через одно и то же", page)
        r = self.post(f"/s/{sid}/tool", {"action": "choose", "tool": "contradiction"})
        self.assertIn("Сначала отметьте", r.get_data(as_text=True))
        self.post(f"/s/{sid}/tool", {"action": "choose", "tool": "contradiction", "self_diag": "Всё привычное"})
        page = self.page(f"/s/{sid}/tool")
        self.assertIn("Совпало с вашим: нет", page)
        self.assertEqual(self.session_json(sid, "tool_json")["type"], "contradiction")
        names = [r["name"] for r in self.q("SELECT name FROM events")]
        self.assertIn("tool_chosen_by_user", names)


def fake_ai(system, user):
    if "Предложи 6 идей" in user:
        return '{"ideas": ["Вести соцсети", "Скидка новым клиентам", "Попросить отзывы", "Профиль на бирже"]}'
    if "Оцени формулировку" in system:
        return '{"problem": "none", "in_domain": true}'
    if "Перепиши шаблон" in system:
        return '{"task_text": "Пусть издательство само сделает то, что сейчас делаете вы. Что это должно быть?"}'
    if "причинное допущение" in system:
        return '{"assumption": "Издательства выбирают по цене", "task": "Сохраните цель — 3 заказа. Как добиться её, если цена не важна?"}'
    return "{}"


class ToolWithAITest(TrainingCase):
    ai_client = llm.FakeClient(fake_ai)

    def test_operator_adapted_by_ai(self):
        sid = self.to_tool()
        self.page(f"/s/{sid}/tool")
        tool = self.session_json(sid, "tool_json")
        self.assertEqual(tool["source"], "ai")
        self.assertIn("издательство", tool["task_text"])

    def test_chain_assumption_by_ai(self):
        sid = self.to_tool()
        self.force_diagnosis(sid, "Д3", "chain")
        self.page(f"/s/{sid}/tool")
        tool = self.session_json(sid, "tool_json")
        self.assertEqual((tool["source"], tool["assumption"]), ("ai", "Издательства выбирают по цене"))


def bad_ai(system, user):
    if "Перепиши шаблон" in system:
        return '{"task_text": "Например, сделайте скидку [клиент]"}'  # пример решения и слот — отвергаем
    if "Оцени формулировку" in system:
        return '{"problem": "none", "in_domain": true}'
    return "{}"


class ToolBadAITest(TrainingCase):
    ai_client = llm.FakeClient(bad_ai)

    def test_bad_adaptation_falls_back_to_template(self):
        sid = self.to_tool()
        self.page(f"/s/{sid}/tool")
        tool = self.session_json(sid, "tool_json")
        self.assertEqual(tool["source"], "template")
        self.assertNotIn("Например", tool["task_text"])
        errors = self.q("SELECT props_json FROM events WHERE name = 'ai_error'")
        self.assertTrue(any("adapt_tool" in r["props_json"] for r in errors))


if __name__ == "__main__":
    unittest.main()
