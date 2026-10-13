"""Исправления по итогам живого прогона на DeepSeek (2026-10-11)."""

import json
import unittest

import ai_roles
import library
import llm
from helpers import TrainingCase

LIB = library.Library(library.DEFAULT_DIR)


class CriticValidationTest(unittest.TestCase):
    def run_critic(self, objections):
        ai = llm.AI(llm.FakeClient(lambda s, u: json.dumps({"objections": objections}, ensure_ascii=False)))
        return ai_roles.critic(ai, LIB, "идея", "задача", "цель", "R01 Бюджет: 0 ₽", ["причина"])

    def test_concrete_condition_without_link_is_kept(self):
        out = self.run_critic([
            {"category": "К5", "card_field": None, "mechanism_link": None,
             "condition": "Не сработает, если учитель не найдёт времени на разговорный клуб в своём графике",
             "check": "Позвонить учителю", "severity": 2},
            {"category": "К1", "card_field": None, "mechanism_link": None,
             "condition": "Не сработает, если не понравится", "check": "—", "severity": 2},
            {"category": "К5", "card_field": "R99", "mechanism_link": "время на образцы",
             "condition": "Не сработает, если на образцы уйдёт больше 5 часов", "check": "Засечь время",
             "severity": 3}])
        self.assertEqual(len(out), 2)  # общая фраза без связи отброшена
        self.assertEqual(out[0]["mechanism_link"], "механизм идеи")
        self.assertIsNone(out[1]["card_field"])  # несуществующее поле карточки не принимаем
        self.assertEqual(out[1]["mechanism_link"], "время на образцы")

    def test_roles_note_in_adaptation_and_hint(self):
        calls = []
        ai = llm.AI(llm.FakeClient(lambda s, u: calls.append((s, u)) or '{"task_text": "x", "hint": "y"}'))
        ai_roles.adapt_tool(ai, "задача", "цель", "шаблон", "оператор", idea="моя идея")
        ai_roles.hint(ai, LIB, "fragment", "шаблон", "задача", "задание", "")
        self.assertIn("а НЕ самого пользователя", calls[0][0])
        self.assertIn("Идея пользователя: <<<моя идея>>>", calls[0][1])
        self.assertIn("Подумайте о", calls[1][0])


def judge_ai(system, user):
    if "Оцени формулировку" in system:
        return '{"problem": "none", "in_domain": true}'
    if "Ты критик" in system:
        return json.dumps({"objections": [
            {"category": "К5", "card_field": "R02", "mechanism_link": None,
             "condition": "Не сработает, если на образцы уйдёт больше 5 часов в неделю", "check": "Засечь время",
             "severity": 3}]}, ensure_ascii=False)
    if "Ты судья" in system:
        return '{"verdict": "ослаблено", "basis": "аргумент", "reason": "довод есть"}'
    return "{}"


class AgreeSkipsJudgeTest(TrainingCase):
    ai_client = llm.FakeClient(judge_ai)

    def test_agree_without_ai(self):
        sid = self.to_eval()
        self.post(f"/s/{sid}/eval", {"action": "next"})
        self.post(f"/s/{sid}/premortem", {"action": "reasons", "reason1": "Не ответят"})
        o = self.q("SELECT id FROM objections WHERE author = 'critic'")[0]
        before = len([c for c in self.ai_client.calls if "Ты судья" in c[0]])
        self.post(f"/s/{sid}/premortem", {"action": "defend", f"about_{o['id']}": "1", f"type_{o['id']}": "agree"})
        after = len([c for c in self.ai_client.calls if "Ты судья" in c[0]])
        self.assertEqual(before, after)
        row = self.q("SELECT verdict, reason, verdict_source FROM objections WHERE id = ?", (o["id"],))[0]
        self.assertEqual((row["verdict"], row["reason"], row["verdict_source"]), ("в силе", "Риск принят.", "rules"))
        critic_user = [u for s, u in self.ai_client.calls if "Ты критик" in s][-1]
        self.assertIn("R01 ", critic_user)  # карточка для критика — с кодами полей


def temp_ai(system, user):
    if "Оцени формулировку" in system:
        return '{"problem": "none", "in_domain": false}'
    if "Построй 8–12 семейств" in system:
        return json.dumps({"families": [{"name": f"Семейство {i}", "mechanism": f"механизм {i}"}
                                        for i in range(1, 11)]}, ensure_ascii=False)
    if "Предложи 6 идей" in user:
        return '{"ideas": ["а1", "а2", "а3", "а4", "а5", "а6"]}'
    if "отнести идею" in system:
        if "<<<а" in user:  # ответы чат-бота — в T02 и T03
            return '{"family_id": "T02", "confidence": "high", "explanation": "x"}' if "а1" in user or "а2" in user \
                else '{"family_id": "T03", "confidence": "high", "explanation": "x"}'
        return '{"family_id": "T01", "confidence": "high", "explanation": "x"}'
    if "Разбери идею" in system:
        if "сломать" in user:
            return "не JSON"  # ошибка в рабочем потоке должна попасть в журнал
        return ('{"actor": "ученик", "action": "встаёт в очередь", "driver": "быстрее", "barrier": "очередь", '
                '"value": "время", "condition": "если касса свободна", "family_id": "T01", "confidence": "high"}')
    return "{}"


class TempTaxonomyMirrorTest(TrainingCase):
    ai_client = llm.FakeClient(temp_ai)

    def test_untouched_prefers_rare_and_worker_errors_logged(self):
        self.register()
        self.fill_card()
        sid = self.start()
        self.set_task(sid, in_domain="no")
        self.submit_first(sid, ["Поставить кассу", "сломать очередь"], 1)
        rows = self.q("SELECT family, class_source FROM ideas WHERE stage = 'first' ORDER BY ord")
        self.assertEqual((rows[0]["family"], rows[1]["family"]), ("T01", None))
        self.page(f"/s/{sid}/mirror")
        m = json.loads(self.q("SELECT mirror_json FROM sessions")[0]["mirror_json"])
        self.assertEqual(set(m["common"]), {"T02", "T03"})
        self.assertTrue(set(m["untouched"]).isdisjoint(m["common"]))
        errors = [json.loads(r["props_json"]) for r in self.q("SELECT props_json FROM events WHERE name = 'ai_error'")]
        self.assertTrue(any(e["role"] == "xray" for e in errors))


if __name__ == "__main__":
    unittest.main()
