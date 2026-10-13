"""Методология v0.5 (C1): рентген идеи, три линзы, перевёрнутая задача, эволюция идеи с детектором
ложного сдвига, лестница выхода из тупика, две шкалы, «вау»-вопрос, второе открытие, профиль."""

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import db
import llm
from helpers import TrainingCase


def ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


class EvolutionTest(TrainingCase):
    def test_versions_false_shift_and_limit(self):
        sid = self.to_idea()
        self.idea(sid, "submit", "Скидка 15% на второй заказ")
        page = self.page(f"/s/{sid}/idea")
        self.assertIn("Версия 1", page)
        self.assertIn("Изменилась форма, но не принцип", page)  # без ИИ — то же семейство «выгоды»
        self.idea(sid, "improve")
        self.assertIn("Скидка 15% на второй заказ", self.page(f"/s/{sid}/idea"))  # текст версии в поле
        self.idea(sid, "submit", "Стенд на книжной ярмарке")
        page = self.page(f"/s/{sid}/idea")
        self.assertIn("Версия 2", page)
        self.assertIn("Сдвиг механизма", page)
        self.assertIn("Как менялась идея", page)  # прошлая версия видна в истории
        for text in ("Блог об обложках", "Конкурс обложек"):
            self.idea(sid, "improve")
            self.idea(sid, "submit", text)
        self.assertNotIn("Доработать идею", self.page(f"/s/{sid}/idea"))  # 4 версии — предел
        self.assertEqual(self.idea(sid, "improve").status_code, 400)
        self.idea(sid, "accept_version")
        rows = self.q("SELECT version, stage FROM ideas WHERE stage IN ('version', 'final') ORDER BY version")
        self.assertEqual([r["stage"] for r in rows], ["version"] * 3 + ["final"])
        self.assertEqual(self.state(sid), "eval")
        names = [r["name"] for r in self.q("SELECT name FROM events")]
        self.assertEqual(names.count("version_created"), 4)

    def test_pending_version_blocks_other_actions(self):
        sid = self.to_idea()
        self.idea(sid, "submit", "Скидка 15%")
        self.assertEqual(self.post(f"/s/{sid}/idea", {"hint": "question"}).status_code, 400)
        self.assertEqual(self.idea(sid, "ladder_start").status_code, 400)


class LadderTest(TrainingCase):
    def test_ladder_builds_fragment(self):
        sid = self.to_idea()
        self.idea(sid, "ladder_start", "наброс")
        page = self.page(f"/s/{sid}/idea")
        self.assertIn("Что мешает сейчас?", page)
        self.assertEqual(self.post(f"/s/{sid}/idea", {"action": "ladder_1", "obstacle": "что-то"}).status_code, 400)
        self.post(f"/s/{sid}/idea", {"action": "ladder_1", "obstacle": "Все идеи кажутся одинаковыми"})
        self.assertIn("какой один барьер мешает вашему клиенту", self.page(f"/s/{sid}/idea"))
        self.post(f"/s/{sid}/idea", {"action": "ladder_2", "barrier": "редактор не знает меня"})
        self.post(f"/s/{sid}/idea", {"action": "ladder_3", "operation": "участника"})
        ladder = self.session_json(sid, "flags_json")["ladder"]
        self.assertEqual((ladder["step"], ladder["operation"]), (4, "участника"))
        self.assertTrue(ladder["op_text"])
        self.assertEqual(self.post(f"/s/{sid}/idea", {"action": "ladder_4", "what": "", "easier": "x"}).status_code,
                         400)
        self.post(f"/s/{sid}/idea", {"action": "ladder_4", "what": "редактор открывает письмо",
                                     "easier": "в теме письма — название его книги"})
        page = self.page(f"/s/{sid}/idea")
        self.assertIn("Получилось начало идеи", page)
        self.assertIn("Редактор открывает письмо — в теме письма", page)
        self.assertIsNone(self.session_json(sid, "flags_json").get("ladder"))
        self.assertEqual(self.q("SELECT COUNT(*) AS n FROM hints")[0]["n"], 0)  # лестница не тратит подсказки

    def test_finish_without_idea_is_honest(self):
        sid = self.to_idea()
        self.idea(sid, "ladder_start")
        self.post(f"/s/{sid}/idea", {"action": "ladder_1", "obstacle": "Не вижу вариантов"})
        self.idea(sid, "ladder_quit")
        self.assertEqual(self.state(sid), "route")
        page = self.page(f"/s/{sid}/route")
        self.assertIn("идея не сложилась; затруднение: «не вижу вариантов»", page)
        self.assertNotIn("Пришла бы вам эта идея", page)
        self.post(f"/s/{sid}/route", {"helped": "nothing"})
        self.assertEqual(self.state(sid), "done")
        self.assertEqual(len(self.q("SELECT * FROM ideas WHERE stage = 'final'")), 0)
        names = [r["name"] for r in self.q("SELECT name FROM events")]
        self.assertIn("ladder_failed", names)
        self.assertIn("session_completed", names)


class LensesAndReframeNoAITest(TrainingCase):
    def test_lens_fallback_and_own_barrier(self):
        self.register()
        self.fill_card()
        sid = self.start()
        self.set_task(sid)
        self.submit_first(sid, ["Скидка 10% на первый заказ", "Скидка 20% постоянным"], 1)
        page = self.page(f"/s/{sid}/mirror")
        self.assertIn("Посмотрим на задачу с трёх сторон", page)
        self.assertIn("Как поведение клиента: что мешает ему сделать первый шаг?", page)
        self.post(f"/s/{sid}/mirror", {"action": "continue", "lens": "behavior",
                                       "own_barrier": "редактору лень разбираться в портфолио"})
        barrier = self.session_json(sid, "flags_json")["barrier"]
        self.assertEqual((barrier["lens"], barrier["source"]), ("behavior", "user"))
        self.assertIn("Ищем решение вокруг препятствия: «редактору лень разбираться в портфолио»",
                      self.page(f"/s/{sid}/tool"))

    def test_reframe_without_ai(self):
        sid = self.to_tool()
        self.force_diagnosis(sid, "Д3", "reframe")
        page = self.page(f"/s/{sid}/tool")
        self.assertIn("Перевёрнутая задача", page)
        self.assertIn("Ради чего вам нужна цель «3 новых заказа за месяц»?", page)
        r = self.post(f"/s/{sid}/tool", {"action": "reframe_own", "goal_behind": ""})
        self.assertIn("ради чего вам нужна эта цель", r.get_data(as_text=True))
        self.post(f"/s/{sid}/tool", {"action": "reframe_own", "goal_behind": "стабильный доход без простоев"})
        tool = self.session_json(sid, "tool_json")
        self.assertEqual(tool["phase"], "ready")
        self.assertEqual(tool["task_text"], "Как добиться «стабильный доход без простоев» другим путём — "
                                            "не обязательно через «3 новых заказа за месяц»?")
        self.post(f"/s/{sid}/tool", {"action": "go"})
        self.assertEqual(self.state(sid), "idea")

    def test_reframe_offered_as_alternative(self):
        sid = self.to_tool(level=2)
        self.force_diagnosis(sid, "Д3", "chain")
        self.post(f"/s/{sid}/tool", {"action": "self_diag", "self_diag": "Держусь за любимую идею"})
        self.assertIn("Перевёрнутая задача", self.page(f"/s/{sid}/tool"))


def discovery_ai(system, user):
    if "Оцени формулировку" in system:
        return '{"problem": "none", "in_domain": true}'
    if "Разбери идею" in system:
        return json.dumps({"actor": "редактор", "action": "смотрит образец", "driver": "снижение неопределённости",
                           "barrier": "непонятно, подойдёт ли стиль", "value": "уверенность",
                           "condition": "если книга в плане", "family_id": "F06.2", "confidence": "high"},
                          ensure_ascii=False)
    if "через три разные модели" in system:
        return json.dumps({k: {"barrier": f"Препятствие {k}: редактору сложно оценить стиль по портфолио.",
                               "question": f"Вопрос {k}: что упростит оценку?"} for k in ("system", "behavior",
                                                                                         "value")},
                          ensure_ascii=False)
    if "цель на уровень выше" in system:
        return '{"goal_behind": "стабильный поток проектов"}'
    return "{}"


class DiscoveryWithAITest(TrainingCase):
    ai_client = llm.FakeClient(discovery_ai)

    def test_xray_lenses_fix_and_reframe(self):
        self.register()
        self.fill_card()
        sid = self.start()
        self.set_task(sid)
        self.submit_first(sid, ["Отправить образец обложки", "Скидка"], 1)
        x = json.loads(self.q("SELECT xray_json FROM ideas WHERE ord = 1")[0]["xray_json"])
        self.assertEqual(x["barrier"], "непонятно, подойдёт ли стиль")
        page = self.page(f"/s/{sid}/mirror")
        self.assertIn("Работает за счёт: снижение неопределённости. Снимает барьер: непонятно, подойдёт ли стиль.",
                      page)
        self.assertIn("Препятствие behavior", page)
        idea_id = self.q("SELECT id FROM ideas WHERE ord = 1")[0]["id"]
        self.post(f"/s/{sid}/mirror", {"action": "fix_mechanism", "idea_id": idea_id,
                                       "driver": "редактор видит свою книгу"})
        x = json.loads(self.q("SELECT xray_json FROM ideas WHERE ord = 1")[0]["xray_json"])
        self.assertEqual(x["driver_user"], "редактор видит свою книгу")
        self.assertIn("Работает за счёт: редактор видит свою книгу", self.page(f"/s/{sid}/mirror"))
        self.post(f"/s/{sid}/mirror", {"action": "continue", "lens": "value"})
        barrier = self.session_json(sid, "flags_json")["barrier"]
        self.assertEqual((barrier["lens"], barrier["source"]), ("value", "ai"))
        self.force_diagnosis(sid, "Д3", "reframe")
        page = self.page(f"/s/{sid}/tool")
        self.assertIn("Похоже, за вашей целью стоит другая: «стабильный поток проектов»", page)
        self.post(f"/s/{sid}/tool", {"action": "reframe_yes"})
        tool = self.session_json(sid, "tool_json")
        self.assertEqual(tool["task_text"], "Как добиться «стабильный поток проектов» другим путём — "
                                            "не обязательно через «3 новых заказа за месяц»?")
        # препятствие из линзы уходит в адаптацию задания как данные
        self.force_diagnosis(sid, "Д1", "operator")
        self.page(f"/s/{sid}/tool")
        adapt_calls = [u for s, u in self.ai_client.calls if "Перепиши шаблон" in s]
        self.assertIn("решение ищем вокруг препятствия: <<<Препятствие value", adapt_calls[-1])


class RouteWowAndIncubationTest(TrainingCase):
    def to_route(self):
        sid = self.to_eval(final="Стенд на книжной ярмарке")
        self.post(f"/s/{sid}/eval", {"action": "next"})
        self.post(f"/s/{sid}/premortem", {"action": "skip"})
        return sid

    def test_wow_required_and_genealogy(self):
        sid = self.to_route()
        page = self.page(f"/s/{sid}/route")
        self.assertIn("Как менялась идея", page)
        self.assertIn("— первая идея (фаворит)", page)
        r = self.post(f"/s/{sid}/route", {"helped": "tool"})
        self.assertIn("пришла бы эта идея вам в голову", r.get_data(as_text=True))
        self.post(f"/s/{sid}/route", {"helped": "tool", "wow": "Нет"})
        self.assertEqual(self.state(sid), "check")
        self.assertEqual(self.session_json(sid, "route_json")["wow"], "Нет")

    def test_incubation_opt_in(self):
        sid = self.to_route()
        self.post(f"/s/{sid}/route", {"helped": "tool", "wow": "Скорее нет"})
        self.post(f"/s/{sid}/check", {"action": "skip"})
        page = self.page(f"/s/{sid}")
        self.assertIn("Вернуться к задаче завтра", page)
        self.post(f"/s/{sid}/done", {"action": "incubate"})
        self.assertEqual(len(self.q("SELECT * FROM incubations")), 1)
        self.assertNotIn("Второе открытие", self.page("/me"))  # ещё рано
        self.execute("UPDATE incubations SET due = ?", (ago(0.1),))
        self.assertIn("Второе открытие", self.page("/me"))
        r = self.post(f"/incubation/{sid}", {"reframe": "", "idea": "x"}, csrf_from="/me")
        self.assertEqual(len(self.q("SELECT * FROM ideas WHERE stage = 'incubation'")), 0)
        self.post(f"/incubation/{sid}", {"reframe": "Как стать заметным для редакторов детских книг",
                                         "barrier": "меня не знают", "idea": "Блог с разборами обложек"},
                  csrf_from="/me")
        row = self.q("SELECT * FROM ideas WHERE stage = 'incubation'")[0]
        self.assertEqual((row["text"], row["status"]), ("Блог с разборами обложек", "второе открытие"))
        self.assertTrue(json.loads(row["change_json"])["vs_prev"])
        self.assertNotIn("Второе открытие", self.page("/me"))
        self.assertEqual(self.post(f"/incubation/{sid}", {"reframe": "ещё"}, csrf_from="/me").status_code, 404)
        final = self.q("SELECT id FROM ideas WHERE stage = 'final'")[0]["id"]
        self.assertIn("(второе открытие)", self.page(f"/bank/{final}"))


class ProfileTest(TrainingCase):
    def test_profile_after_three_sessions(self):
        self.register()
        self.fill_card()
        self.run_session()
        self.run_session()
        self.assertNotIn("Ваш профиль", self.page("/me"))
        self.run_session()
        page = self.page("/me")
        self.assertIn("Ваши привычные механизмы: «Материальная выгода» (9)", page)
        self.assertIn("Это наблюдение по 3 тренировкам", page)
        self.post("/profile/dispute", {"comment": "я не всегда про скидки"}, csrf_from="/me")
        self.assertIn("profile_disputed", [r["name"] for r in self.q("SELECT name FROM events")])


class LiveRunFixesTest(unittest.TestCase):
    """Исправления по живому прогону v0.5 на DeepSeek (2026-10-12)."""

    def test_shift_explanation_drops_ready_solutions(self):
        import ai_roles
        reply = json.dumps({"shift": "форма", "changed": ["момент"], "same_as": 1,
                            "explanation": "Идея работает по тому же принципу, что и скидка. Чтобы сменить принцип, "
                                           "стоит подумать о других способах снижения риска, например, гарантии "
                                           "возврата средств."}, ensure_ascii=False)
        ai = llm.AI(llm.FakeClient(lambda s, u: reply))
        r = ai_roles.shift_check(ai, [{"text": "Скидка", "xray": None}], "Скидка 30%", None)
        self.assertEqual(r["explanation"], "Идея работает по тому же принципу, что и скидка.")

    def test_reframe_rejects_same_goal(self):
        import ai_roles
        import library
        lib = library.Library(library.DEFAULT_DIR)
        same = llm.AI(llm.FakeClient(lambda s, u: '{"goal_behind": "3 новых заказа за месяц"}'))
        self.assertIsNone(ai_roles.reframe(same, lib, "задача", "3 новых заказа за месяц", None))
        ok = llm.AI(llm.FakeClient(lambda s, u: '{"goal_behind": "стабильный доход."}'))
        self.assertEqual(ai_roles.reframe(ok, lib, "задача", "3 новых заказа за месяц", None),
                         {"goal_behind": "стабильный доход"})


def rubric_ai(system, user):
    if "Оцени формулировку" in system:
        return '{"problem": "none", "in_domain": true}'
    if "Оцени идею по рубрикам" in system:
        return ('{"criteria": [{"id": "RB1", "level": 3, "reason": "а"}, {"id": "RB2", "level": 3, "reason": "б"},'
                '{"id": "RB3", "level": 1, "reason": "в"}]}')
    return "{}"


class RubricPrefetchTest(TrainingCase):
    ai_client = llm.FakeClient(rubric_ai)

    def test_rubric_computed_with_version(self):
        sid = self.to_idea()
        self.idea(sid, "submit", "Стенд на книжной ярмарке")
        ch = json.loads(self.q("SELECT change_json FROM ideas WHERE stage = 'version'")[0]["change_json"])
        self.assertEqual(ch["rubric"]["RB3"]["level"], 1)
        before = len([c for c in self.ai_client.calls if "Оцени идею по рубрикам" in c[0]])
        self.idea(sid, "accept_version")
        self.page(f"/s/{sid}/eval")
        after = len([c for c in self.ai_client.calls if "Оцени идею по рубрикам" in c[0]])
        self.assertEqual(before, after)  # на оценке модель не вызывается — рубрика уже есть
        ev = self.session_json(sid, "eval_json")
        self.assertEqual((ev["rubric_source"], ev["feasibility"]), ("ai", "низкая"))


class MigrationTest(unittest.TestCase):
    def test_old_database_gets_new_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "old.db")
            with closing(sqlite3.connect(path)) as conn:
                conn.execute("CREATE TABLE ideas (id INTEGER PRIMARY KEY, session_id INTEGER, stage TEXT, "
                             "ord INTEGER, text TEXT, is_favorite INTEGER, family TEXT, subfamily TEXT, "
                             "class_source TEXT, confidence TEXT, explanation TEXT, disputed INTEGER, status TEXT, "
                             "created_at TEXT)")
                conn.commit()
            db.init_db(path)
            db.init_db(path)  # повторный запуск не падает
            with closing(sqlite3.connect(path)) as conn:
                cols = {r[1] for r in conn.execute("PRAGMA table_info(ideas)")}
            self.assertTrue({"xray_json", "version", "change_json"} <= cols)


if __name__ == "__main__":
    unittest.main()
