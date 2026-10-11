import json
import unittest

import llm
from helpers import TrainingCase

ALL_CATEGORIES = ("Не захотят и не увидят; не поверят новичку; не поймут; не хватит времени и денег; "
                  "не умею; дорого; долго; зависит от партнёров; закон")


class PremortemTest(TrainingCase):
    def to_premortem(self, **kw):
        sid = self.to_eval(**kw)
        self.post(f"/s/{sid}/eval", {"action": "next"})
        self.assertEqual(self.state(sid), "premortem")
        return sid

    def critic_rows(self):
        return self.q("SELECT * FROM objections WHERE author = 'critic' ORDER BY id")

    def defend_all(self, sid, answers):
        data = {"action": "defend"}
        for o, (about, rtype, text) in zip(self.critic_rows(), answers):
            data.update({f"about_{o['id']}": about, f"type_{o['id']}": rtype, f"text_{o['id']}": text})
        return self.post(f"/s/{sid}/premortem", data)

    def test_reasons_then_library_critic(self):
        sid = self.to_premortem()
        page = self.page(f"/s/{sid}/premortem")
        self.assertIn("Представьте: прошёл месяц. Идея не сработала.", page)  # цель «…за месяц»
        r = self.post(f"/s/{sid}/premortem", {"action": "reasons"})
        self.assertIn("Назовите хотя бы одну причину", r.get_data(as_text=True))
        self.post(f"/s/{sid}/premortem", {"action": "reasons", "reason1": "Редакторы не ответят на письма"})
        mine = self.q("SELECT * FROM objections WHERE author = 'user'")
        self.assertEqual([m["condition"] for m in mine], ["Редакторы не ответят на письма"])
        rows = self.critic_rows()
        self.assertTrue(1 <= len(rows) <= 3)
        for o in rows:
            self.assertTrue(o["lib_id"])  # из LIB-09
            self.assertTrue(o["condition"].startswith("Не сработает, если"))
            self.assertTrue(o["check_text"])
        self.assertEqual(len({o["category"] for o in rows}), len(rows))  # категории не повторяются
        page = self.page(f"/s/{sid}/premortem")
        self.assertIn("Вот что ещё может помешать", page)
        self.assertIn("Проверить можно так:", page)
        self.assertNotIn("Не сработает, если Не сработает", page)

    def test_period_from_goal(self):
        sid = self.to_premortem(task={"goal": "5 заказов за 3 недели"})
        self.assertIn("прошло 3 недели", self.page(f"/s/{sid}/premortem"))

    def test_period_default(self):
        sid = self.to_premortem(task={"goal": "Найти 3 новых издательства"})
        self.assertIn("прошло 2 месяца", self.page(f"/s/{sid}/premortem"))

    def test_defense_and_verdicts_level1(self):
        sid = self.to_premortem()
        self.post(f"/s/{sid}/premortem", {"action": "reasons", "reason1": "Редакторы не ответят"})
        n = len(self.critic_rows())
        r = self.defend_all(sid, [("", "argument", "x")] * n)
        self.assertIn("Отметьте: про вас это возражение или нет", r.get_data(as_text=True))
        answers = [("1", "argument", "Точно сработает"),
                   ("0", "fact", "У меня уже есть пять знакомых редакторов в двух издательствах"),
                   ("1", "agree", "")][:n]
        self.defend_all(sid, answers)
        rows = self.critic_rows()
        self.assertEqual(rows[0]["verdict"], "в силе")  # настойчивость без довода
        if n >= 2:
            self.assertEqual((rows[1]["verdict"], rows[1]["basis"]), ("ослаблено", "факт"))
        self.assertTrue(all(o["verdict_source"] == "rules" and o["round"] == 1 for o in rows))
        page = self.page(f"/s/{sid}/premortem")
        self.assertIn("В силе:", page)
        self.assertIn("Самое сильное открытое возражение", page)
        self.assertNotIn("Ответить на вердикты ещё раз", page)  # уровень 1 — один раунд
        self.assertEqual(self.post(f"/s/{sid}/premortem", {"action": "round2"}).status_code, 400)
        self.post(f"/s/{sid}/premortem", {"action": "finish"})
        self.assertEqual(self.state(sid), "route")
        strongest = self.session_json(sid, "flags_json")["strongest"]
        open_rows = [o for o in rows if o["verdict"] == "в силе"]
        best = max(open_rows, key=lambda o: (o["severity"], o["about_me"] == 1))
        self.assertEqual(strongest, best["id"])

    def test_round_two_on_level2(self):
        sid = self.to_premortem(level=2)
        self.post(f"/s/{sid}/premortem", {"action": "reasons", "reason1": "Редакторы не ответят"})
        n = len(self.critic_rows())
        self.defend_all(sid, [("1", "argument", "Точно")] * n)
        self.assertIn("Ответить на вердикты ещё раз", self.page(f"/s/{sid}/premortem"))
        self.post(f"/s/{sid}/premortem", {"action": "round2"})
        page = self.page(f"/s/{sid}/premortem")
        self.assertIn("Ответ без новых доводов не меняет вердикт", page)
        first = self.critic_rows()[0]
        self.post(f"/s/{sid}/premortem", {"action": "defend", f"type_{first['id']}": "change",
                                           f"text_{first['id']}": "Сначала сделаю 3 образца бесплатно и покажу редакторам"})
        rows = self.critic_rows()
        self.assertEqual((rows[0]["verdict"], rows[0]["round"]), ("ослаблено", 2))
        self.assertEqual(rows[0]["about_me"], 1)  # отметка первого раунда сохранилась
        self.assertTrue(all(o["round"] == 1 for o in rows[1:]))
        self.assertNotIn("Ответить на вердикты ещё раз", self.page(f"/s/{sid}/premortem"))
        self.post(f"/s/{sid}/premortem", {"action": "finish"})
        self.assertEqual(self.state(sid), "route")

    def test_user_named_everything(self):
        sid = self.to_premortem()
        self.post(f"/s/{sid}/premortem", {"action": "reasons", "reason1": ALL_CATEGORIES})
        self.assertEqual(len(self.critic_rows()), 0)
        page = self.page(f"/s/{sid}/premortem")
        self.assertIn("Вы назвали главное сами", page)
        self.post(f"/s/{sid}/premortem", {"action": "finish"})
        user_row = self.q("SELECT id FROM objections WHERE author = 'user'")[0]
        self.assertEqual(self.session_json(sid, "flags_json")["strongest"], user_row["id"])

    def test_no_reasons_allowed_with_mark(self):
        sid = self.to_premortem()
        self.post(f"/s/{sid}/premortem", {"action": "reasons", "none": "1"})
        self.assertTrue(self.session_json(sid, "flags_json")["pm_no_reasons"])
        self.assertGreaterEqual(len(self.critic_rows()), 1)

    def test_skip(self):
        sid = self.to_premortem()
        self.post(f"/s/{sid}/premortem", {"action": "skip"})
        self.assertEqual(self.state(sid), "route")
        self.assertTrue(self.session_json(sid, "flags_json")["premortem_skipped"])


def fake_ai(system, user):
    if "Оцени формулировку" in system:
        return '{"problem": "none", "in_domain": true}'
    if "Ты критик пре-мортема" in system:
        return json.dumps({"objections": [
            {"category": "К5", "card_field": "R02", "mechanism_link": None,
             "condition": "Не сработает, если на 3 образца уйдёт больше 5 часов в неделю",
             "check": "Сделать один образец и засечь время", "severity": 3},
            {"category": "К1", "card_field": None, "mechanism_link": None,
             "condition": "Может не сработать", "check": "—", "severity": 2},  # пустое — отбросить
        ]}, ensure_ascii=False)
    if "Ты судья" in system:
        return '{"verdict": "снято", "basis": "факт", "reason": "Есть опыт, но он не проверен."}'
    return "{}"


class PremortemWithAITest(TrainingCase):
    ai_client = llm.FakeClient(fake_ai)

    def test_ai_critic_filters_empty_and_fact_capped(self):
        sid = self.to_eval()
        self.post(f"/s/{sid}/eval", {"action": "next"})
        self.post(f"/s/{sid}/premortem", {"action": "reasons", "reason1": "Не ответят"})
        rows = self.q("SELECT * FROM objections WHERE author = 'critic'")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["card_field"], "R02")
        page = self.page(f"/s/{sid}/premortem")
        self.assertIn("Связано с:", page)
        o = rows[0]
        self.post(f"/s/{sid}/premortem", {"action": "defend", f"about_{o['id']}": "1", f"type_{o['id']}": "fact",
                                           f"text_{o['id']}": "Один образец у меня занимает 2 часа"})
        o = self.q("SELECT * FROM objections WHERE author = 'critic'")[0]
        self.assertEqual((o["verdict"], o["verdict_source"]), ("ослаблено", "ai"))
        self.assertIn("Факт принят", self.page(f"/s/{sid}/premortem"))
        judge_calls = [c for c in self.ai_client.calls if "Ты судья" in c[0]]
        self.assertNotIn("Ты критик", judge_calls[-1][0])  # судья — отдельный вызов без истории критика


if __name__ == "__main__":
    unittest.main()
