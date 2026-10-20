"""Эксперимент «обычный чат-бот против тренировки» (lab.py) и сокращённый сценарий C (training, flow = short).
Настоящий API не вызывается — только FakeClient."""

import csv
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

import eval_quality
import llm
from helpers import TrainingCase
from test_a11y import A11yMixin
from test_e2e import contract_ai

TASK = {"text": "Иллюстратор-фрилансер хочет найти новых заказчиков среди небольших издательств.",
        "goal": "3 новых заказа за месяц", "bans": "без платной рекламы", "in_domain": "Да"}
FIRST = {"idea1": "Скидка 10% на первый заказ", "idea2": "Промокод для новых", "favorite": "1",
         "why": "Скидка мотивирует попробовать"}


def lab_ai(system, user):
    if "Ты — полезный ассистент" in system:
        return "1. Обложка-образец к книге из плана издательства.\n2. Письма редакторам.\nРекомендую первый вариант."
    if "Оцени идею по рубрикам" in system:
        return json.dumps({"criteria": [{"id": k, "level": 3, "evidence": "x", "reason": "укладывается"}
                                        for k in ("RB1", "RB2", "RB3", "RB4")],
                           "needs": {"money_rub": 0, "hours_per_week": 3}}, ensure_ascii=False)
    return contract_ai(system, user)


class LabCase(TrainingCase):
    ai_client = llm.FakeClient(lab_ai)

    def setUp(self):
        super().setUp()
        self.app.config.update(LAB=True, ADMINS="user1")

    def new_run(self, arms):
        self.register()
        self.fill_card()
        option = next(o for o in __import__("lab").NEW_FIELDS[1]["options"] if o.startswith(arms + " "))
        r = self.post("/lab/new", {"code": "p01", "arms": option}, csrf_from="/lab/new")
        rid = int(r.headers["Location"].rstrip("/").split("/")[-1])
        self.post(f"/lab/{rid}/pre", {"occupation": "Фрилансер / самозанятый", "chatbot_use": "Реже",
                                      "usual": "Спросил бы коллег", "time_budget": "30–60 минут",
                                      "good_result": "Понять, с чего начать"}, csrf_from=f"/lab/{rid}/pre")
        self.post(f"/lab/{rid}/task", TASK, csrf_from=f"/lab/{rid}/task")
        self.post(f"/lab/{rid}/first", FIRST, csrf_from=f"/lab/{rid}/first")
        return rid

    def card(self, rid, pos, solution):
        return self.post(f"/lab/{rid}/card/{pos}", {
            "solution": solution, "why": "проще оценить работу", "constraints": "бесплатно, 3 часа",
            "step": "Выбрать книгу и сделать эскиз", "success": "ответ редактора", "fail": "тишина неделю",
            "confidence": "4", "useful": "4"}, csrf_from=f"/lab/{rid}/card/{pos}")

    def run_short_training(self, sid):
        """Сценарий C внутри эксперимента: задача и первые идеи уже заполнены — человек подтверждает их."""
        page = self.page(f"/s/{sid}/task")
        self.assertIn(TASK["text"], page)  # задача из эксперимента подставлена
        self.post(f"/s/{sid}/task", {"text": TASK["text"], "goal": TASK["goal"], "bans": TASK["bans"],
                                     "in_domain": "yes"})
        first = self.page(f"/s/{sid}/first")
        self.assertIn('value="Скидка 10% на первый заказ"', first)  # первые идеи — те же, что до обеих сессий
        self.post(f"/s/{sid}/first", {"idea1": FIRST["idea1"], "idea2": FIRST["idea2"], "favorite": "1",
                                      "why": FIRST["why"]})
        self.page(f"/s/{sid}/mirror")
        self.post(f"/s/{sid}/mirror", {"action": "continue"})
        self.page(f"/s/{sid}/tool")
        tool = self.session_json(sid, "tool_json")
        if tool["phase"] == "target":
            self.post(f"/s/{sid}/tool", {"action": "target", "target": tool["targets"][0]})
        if self.session_json(sid, "tool_json")["phase"] == "select":
            self.post(f"/s/{sid}/tool", {"action": "select", "chosen": tool["items"][0]["id"]})
        self.post(f"/s/{sid}/tool", {"action": "go"})
        self.idea(sid, "submit", "Нарисовать обложку-образец к книге из плана издательства", self_check="no")
        self.idea(sid, "accept_version")
        self.assertEqual(self.state(sid), "choose")
        return self.page(f"/s/{sid}/choose")


class LabFlowTest(A11yMixin, LabCase):
    def test_chatbot_then_short_training_then_blind_export(self):
        rid = self.new_run("A,C")
        # сессия 1 — обычный чат-бот той же модели, запрос собран из задачи, ограничений и первых идей
        r = self.post(f"/lab/{rid}/arm/1", {}, csrf_from=f"/lab/{rid}")
        self.assertIn(f"/lab/{rid}/arm/1/chat", r.headers["Location"])
        page = self.page(f"/lab/{rid}/arm/1/chat")
        self.assertIn("Ограничения: без платной рекламы", page)
        self.assertIn("Мои первые идеи: Скидка 10% на первый заказ; Промокод для новых", page)
        self.post(f"/lab/{rid}/arm/1/chat", {"action": "send", "message": "Помоги выбрать"},
                  csrf_from=f"/lab/{rid}/arm/1/chat")
        self.assertIn("Рекомендую первый вариант", self.page(f"/lab/{rid}/arm/1/chat"))
        chat_calls = [m for m in self.ai_client.calls if "Ты — полезный ассистент" in m[0]]
        self.assertEqual(len(chat_calls), 1)
        self.post(f"/lab/{rid}/arm/1/chat", {"action": "finish"}, csrf_from=f"/lab/{rid}/arm/1/chat")
        self.card(rid, 1, "Письма редакторам с эскизом")
        # сессия 2 — сокращённая тренировка с той же задачей
        r = self.post(f"/lab/{rid}/arm/2", {}, csrf_from=f"/lab/{rid}")
        sid = int(r.headers["Location"].rstrip("/").split("/")[-1])
        self.assertEqual(self.session_json(sid, "flags_json")["flow"], "short")
        choose = self.run_short_training(sid)
        self.assertIn("Ваша первая идея (фаворит)", choose)
        self.assertIn("Версия 1", choose)
        self.assertIn("Ресурсы:", choose)
        version = self.q("SELECT id FROM ideas WHERE stage = 'version'")[0]["id"]
        self.post(f"/s/{sid}/choose", {"chosen": str(version), "why": "Издательству проще оценить готовую работу",
                                       "wow": "Скорее нет"})
        self.assertEqual(self.state(sid), "check")
        ev = self.session_json(sid, "eval_json")
        self.assertEqual((ev["flow"], ev["chosen_from"], ev["shift_kind"]), ("short", "version", "механизм"))
        self.assertEqual(self.session_json(sid, "route_json")["wow"], "Скорее нет")
        self.post(f"/s/{sid}/check", {"action": "save", "plan": "Сделать эскиз", "when": "завтра",
                                      "ok": "ответ редактора", "fail": "тишина"})
        self.assertEqual(self.state(sid), "done")
        self.assertIn("Вернуться к эксперименту", self.page(f"/s/{sid}/done"))
        self.page(f"/lab/{rid}")  # тренировка завершена — время руки закрыто
        card2 = self.page(f"/lab/{rid}/card/2")
        self.assertIn("Нарисовать обложку-образец", card2)  # карточка подставлена из итога тренировки
        self.card(rid, 2, "Обложка-образец к книге из плана")
        self.post(f"/lab/{rid}/post", {"choice": "Из сессии 2: Обложка-образец к книге из плана",
                                       "better": "Сессия 2", "again": "Как в сессии 2", "new_way": "Скорее да",
                                       "return": "Да", "return_day": "четверг", "pay": "Не знаю"},
                  csrf_from=f"/lab/{rid}/post")
        self.assertIn("Спасибо!", self.page(f"/lab/{rid}"))
        # выгрузка: карточки вслепую — без руки, порядка и кода участника; ключ — отдельно
        z = zipfile.ZipFile(io.BytesIO(self.c.get("/lab/export").data))
        self.assertEqual(sorted(z.namelist()), ["cards_blind.csv", "cards_key.csv", "evaluator_sheet.csv", "runs.csv"])
        blind = list(csv.DictReader(io.StringIO(z.read("cards_blind.csv").decode("utf-8-sig")), delimiter=";"))
        self.assertEqual(len(blind), 2)
        self.assertNotIn("arm", blind[0])
        self.assertIn("Скидка 10% на первый заказ (фаворит)", blind[0]["first_ideas"])
        key = list(csv.DictReader(io.StringIO(z.read("cards_key.csv").decode("utf-8-sig")), delimiter=";"))
        self.assertEqual(sorted(k["arm"] for k in key), ["A", "C"])
        runs = list(csv.DictReader(io.StringIO(z.read("runs.csv").decode("utf-8-sig")), delimiter=";"))
        self.assertEqual((runs[0]["s1_arm"], runs[0]["s1_chat_turns"], runs[0]["post_return"]), ("A", "1", "Да"))
        self.assertAccessible(min_pages=12)


class LabAccessTest(LabCase):
    def test_disabled_by_default(self):
        self.app.config.update(LAB=False)
        self.register()
        self.assertEqual(self.c.get("/lab/new").status_code, 404)

    def test_export_only_for_admins(self):
        self.app.config.update(ADMINS="someone_else")
        self.register()
        self.assertEqual(self.c.get("/lab/export").status_code, 404)

    def test_other_users_run_hidden(self):
        rid = self.new_run("B,A")
        self.post("/logout", {}, csrf_from="/me")
        self.register("user2")
        self.assertEqual(self.c.get(f"/lab/{rid}").status_code, 404)

    def test_full_training_arm_keeps_full_flow(self):
        rid = self.new_run("B,A")
        r = self.post(f"/lab/{rid}/arm/1", {}, csrf_from=f"/lab/{rid}")
        sid = int(r.headers["Location"].rstrip("/").split("/")[-1])
        flags = self.session_json(sid, "flags_json")
        self.assertEqual(flags["flow"], "full")  # рука B — полный путь, какой бы путь ни был по умолчанию
        self.assertEqual(flags["lab_run"], rid)


class ShortFlowWithoutAITest(TrainingCase):
    """Сценарий C работает и без ИИ: ресурсы — по правилам, выбрать можно и первую идею."""

    def test_choose_first_idea(self):
        sid = self.to_tool()
        self.execute("UPDATE sessions SET flags_json = json_set(flags_json, '$.flow', 'short') WHERE id = ?", (sid,))
        self.page(f"/s/{sid}/tool")
        tool = self.session_json(sid, "tool_json")
        if tool["phase"] == "target":
            self.post(f"/s/{sid}/tool", {"action": "target", "target": tool["targets"][0]})
        if self.session_json(sid, "tool_json")["phase"] == "select":
            self.post(f"/s/{sid}/tool", {"action": "select", "chosen": tool["items"][0]["id"]})
        self.post(f"/s/{sid}/tool", {"action": "go"})
        self.submit_final(sid, "Стенд на книжной ярмарке")
        self.assertEqual(self.state(sid), "choose")
        page = self.page(f"/s/{sid}/choose")
        self.assertIn("Шаг 6 из 7", page)  # в сценарии C нет оценки, пре-мортема и маршрута
        fav = self.q("SELECT id FROM ideas WHERE stage = 'first' AND is_favorite = 1")[0]["id"]
        r = self.post(f"/s/{sid}/choose", {"chosen": str(fav), "why": "дешевле", "wow": "Да"})
        self.assertEqual(self.state(sid), "check", r.get_data(as_text=True)[:300])
        final = self.q("SELECT text, stage FROM ideas WHERE stage = 'final'")
        self.assertEqual(final[0]["text"], "Скидка 10% на первый заказ")  # копия первой идеи
        self.assertEqual(len(self.q("SELECT id FROM ideas WHERE stage = 'first'")), 3)  # первые идеи на месте
        self.post(f"/s/{sid}/check", {"action": "skip"})
        self.assertEqual(self.state(sid), "done")

    def test_choose_requires_answers(self):
        sid = self.to_tool()
        self.execute("UPDATE sessions SET flags_json = json_set(flags_json, '$.flow', 'short'), state = 'choose' "
                     "WHERE id = ?", (sid,))
        page = self.post(f"/s/{sid}/choose", {}).get_data(as_text=True)
        self.assertIn("Выберите вариант", page)
        self.assertEqual(self.state(sid), "choose")


class ReviewScoreTest(unittest.TestCase):
    def test_agreement_and_kappa(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "filled.csv"
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f, delimiter=";")
                w.writerow(["id", "проверка", "разметчик 1", "разметчик 2"])
                w.writerows([["chatbot:1", "chatbot", "нет", "нет"], ["chatbot:2", "chatbot", "3", "нет"],
                             ["chatbot:3", "chatbot", "Нет", "нет "], ["rb2:1", "rb2", "2", ""]])
            s = eval_quality.score_review(path)
        self.assertEqual((s["chatbot"]["n"], s["chatbot"]["agree"]), (3, 0.67))
        self.assertEqual(s["chatbot"]["disagree"], ["chatbot:2"])
        self.assertNotIn("rb2", s)  # один разметчик — согласие не считается


if __name__ == "__main__":
    unittest.main()
