import json
import unittest

import llm
from helpers import TrainingCase


class FirstAndMirrorTest(TrainingCase):
    def prepare(self, **card):
        self.register()
        self.fill_card(**card)
        sid = self.start()
        self.set_task(sid)
        return sid

    def submit_first(self, sid, ideas, favorite=1, **extra):
        data = {f"idea{i}": t for i, t in enumerate(ideas, 1)}
        data.update({"favorite": str(favorite), "why": "Скидка мотивирует вернуться"})
        data.update(extra)
        return self.post(f"/s/{sid}/first", data)

    def test_first_validation(self):
        sid = self.prepare()
        r = self.submit_first(sid, [])
        self.assertIn("хотя бы одну идею", r.get_data(as_text=True))
        r = self.submit_first(sid, ["Скидка", "Посты"], favorite=4)
        self.assertIn("скорее всего сделали бы", r.get_data(as_text=True))
        self.assertEqual(self.state(sid), "first")

    def test_single_idea_becomes_favorite(self):
        sid = self.prepare()
        self.submit_first(sid, ["Скидка на первый заказ"], favorite=0)
        rows = self.q("SELECT * FROM ideas")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["is_favorite"], 1)

    def test_mirror_classifies_and_diagnoses_one_rut(self):
        sid = self.prepare()
        self.submit_first(sid, ["Скидка 10% на первый заказ", "Скидка 20% постоянным", "Промокод для новых"])
        self.assertEqual(self.state(sid), "mirror")
        page = self.page(f"/s/{sid}/mirror")
        self.assertIn("Скидки и акции", page)
        self.assertIn("частое направление", page)
        self.assertIn("Направления, которых вы не коснулись", page)
        self.assertIn("Что обычно предлагает чат-бот", page)  # примеры из LIB-14 без ИИ
        m = json.loads(self.q("SELECT mirror_json FROM sessions")[0]["mirror_json"])
        self.assertEqual(m["baseline_source"], "library")
        self.assertNotIn("F07", m["untouched"])
        self.post(f"/s/{sid}/mirror", {"action": "continue"})
        self.assertEqual(self.state(sid), "tool")
        d = json.loads(self.q("SELECT diagnosis_json FROM sessions")[0]["diagnosis_json"])
        self.assertEqual((d["code"], d["tool_type"]), ("Д1", "operator"))

    def test_dispute_changes_family_and_recomputes(self):
        sid = self.prepare()
        self.submit_first(sid, ["Петь песни у входа", "Скидка"], favorite=2)
        idea = self.q("SELECT * FROM ideas WHERE ord = 1")[0]
        self.assertIsNone(idea["family"])
        self.page(f"/s/{sid}/mirror")
        self.post(f"/s/{sid}/mirror", {"action": "dispute", "idea_id": idea["id"], "family": "F11.4"})
        idea = self.q("SELECT * FROM ideas WHERE ord = 1")[0]
        self.assertEqual((idea["family"], idea["subfamily"], idea["disputed"], idea["class_source"]),
                         ("F11", "F11.4", 1, "user"))
        m = json.loads(self.q("SELECT mirror_json FROM sessions")[0]["mirror_json"])
        self.assertNotIn("F11", m["untouched"])
        self.assertEqual(self.state(sid), "mirror")

    def test_card_bans_respected_in_navigator(self):
        sid = self.prepare(kind="full", R15="только онлайн", R06=["давать скидки"])
        self.submit_first(sid, ["Вести блог", "Писать компаниям"])
        self.page(f"/s/{sid}/mirror")
        m = json.loads(self.q("SELECT mirror_json FROM sessions")[0]["mirror_json"])
        self.assertNotIn("F11", m["untouched"])
        self.assertNotIn("F07", m["untouched"])


def fake_ai(system, user):
    if "Предложи 6 идей" in user:
        return '{"ideas": ["Вести соцсети", "Скидка новым клиентам", "Попросить отзывы", "Профиль на бирже", "Партнёрство", "Нетворкинг"]}'
    if "отнести идею" in system:
        return '{"family_id": "F15.4", "confidence": "medium", "explanation": "позиция против нормы"}'
    if "Оцени формулировку" in system:
        return '{"problem": "none", "in_domain": true}'
    return "{}"


class MirrorWithAITest(TrainingCase):
    ai_client = llm.FakeClient(fake_ai)

    def test_ai_baseline_and_unknown_classified_by_ai(self):
        self.register()
        self.fill_card()
        sid = self.start()
        self.set_task(sid)
        self.post(f"/s/{sid}/first", {"idea1": "Петь песни у входа", "idea2": "Скидка новым",
                                     "favorite": "1"})
        rows = self.q("SELECT family, class_source FROM ideas ORDER BY ord")
        self.assertEqual((rows[0]["family"], rows[0]["class_source"]), ("F15", "llm"))
        self.assertEqual(rows[1]["class_source"], "dict")
        self.page(f"/s/{sid}/mirror")
        m = json.loads(self.q("SELECT mirror_json FROM sessions")[0]["mirror_json"])
        self.assertEqual(m["baseline_source"], "ai")
        self.assertEqual(len(m["baseline"]), 6)


if __name__ == "__main__":
    unittest.main()
