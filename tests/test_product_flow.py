"""Путь по умолчанию с 18.10: задача → что общего у первых идей (Р16) → одно задание «смена механизма» → своя идея →
выбор под ограничения → план проверки на 48 часов. Настоящий API не вызывается — только FakeClient."""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ai_roles  # noqa: E402
import engine  # noqa: E402
import library  # noqa: E402
import llm  # noqa: E402
import training  # noqa: E402
from helpers import TrainingCase  # noqa: E402
from test_a11y import A11yMixin  # noqa: E402
from test_e2e import failing_ai  # noqa: E402

LIB = library.Library()
FIRST = ["Скидка 10% на первый заказ", "Промокод для новых", "Скидка 20% постоянным"]
NEW = "Нарисовать обложку-образец к книге из плана издательства и отправить редактору"


BECAUSE = {"price": "издательство закажет, потому что так дешевле",
           "trial": "редактор закажет, потому что уже увидел работу к своей книге",
           "referral": "издательство придёт, потому что его привёл знакомый автор",
           "reach": "редактор увидит объявление и, может быть, напишет",
           "habit": "клиент вернётся ради следующего уровня, а не ради экономии"}
LIMIT = "Скидка помогает, только если издательство останавливает цена, а не то, что оно вас не знает."


def pattern_reply(levers=("price", "price", "price"), confidence="high", clear=None, limit=LIMIT):
    """Ответ Р16 v2: разбор каждой идеи; группы, вердикт и уверенность считает код (ai_roles.pattern_v2)."""
    clear = list(clear) if clear is not None else [True] * len(levers)
    return json.dumps({"ideas": [{"result": "новый заказ", "tool": f"инструмент {i}", "because": BECAUSE.get(lv, ""),
                                  "lever": lv if ok else "unclear", "clear": ok,
                                  "missing": "" if ok else "не сказано, что получит клиент и почему"}
                                 for i, (lv, ok) in enumerate(zip(levers, clear), 1)],
                       "limit": limit, "confidence": confidence}, ensure_ascii=False)


def product_ai(system, user, pattern=None):
    if "Оцени формулировку" in system:
        return '{"problem": "none", "in_domain": true}'
    if "Найди общее в первых идеях" in system:
        return pattern or pattern_reply()
    if "Разбери идею" in system:
        idea = user.split("Идея:")[-1]
        if "обложк" in idea:
            x = {"actor": "редактор издательства", "action": "оценивает образец", "driver": "видит работу к своей книге",
                 "barrier": "непонятно, подойдёт ли стиль", "value": "уверенность в выборе",
                 "condition": "Книга действительно есть в плане издательства", "family_id": "F06.4"}
        else:
            x = {"actor": "редактор издательства", "action": "заказывает", "driver": "денежная выгода",
                 "barrier": "цена", "value": "экономия", "condition": "Цена решает выбор", "family_id": "F07.1"}
        return json.dumps(dict(x, confidence="high"), ensure_ascii=False)
    if "аналитик механизмов" in system:
        return json.dumps({"new_driver": "оценить работу до заказа", "new_barrier": "непонятно, подойдёт ли стиль",
                           "shift": "механизм", "changed": ["механизм"], "kept": ["аудитория"], "same_as": None,
                           "confidence": "medium", "levers": ["trial"], "goal_kept": True,
                           "opens": "Редактор оценивает работу к своей книге, а не портфолио вообще.",
                           "risk": "Нужно угадать стиль книги без брифа.",
                           "explanation": "Раньше издательство получало выгоду, теперь видит работу к своей книге."},
                          ensure_ascii=False)
    if "через три разные модели" in system:
        item = {"barrier": "Редактор не знает автора.", "question": "Что редактор видит первым?"}
        return json.dumps({"system": item, "behavior": item, "value": item}, ensure_ascii=False)
    if "Предложи 6 идей" in user:
        return json.dumps({"ideas": ["Вести соцсети", "Скидка новым клиентам", "Попросить отзывы", "Профиль на бирже",
                                     "Партнёрство с типографией", "Нетворкинг"]}, ensure_ascii=False)
    if "отнести идею" in system:
        return '{"family_id": "F03.1", "confidence": "medium", "explanation": "партнёр"}'
    if "Оцени идею по рубрикам" in system:
        return json.dumps({"criteria": [{"id": k, "level": 3, "evidence": "x", "reason": "укладывается"}
                                        for k in ("RB1", "RB2", "RB3", "RB4")],
                           "needs": {"money_rub": 0, "hours_per_week": 3}}, ensure_ascii=False)
    if "ответы обычного чат-бота на ту же задачу" in system:
        return '{"same": null, "explanation": "У чат-бота — скидки и соцсети."}'
    if "Составь план самой дешёвой проверки" in system:
        return json.dumps({"step": "Выбрать 2 книги из плана одного издательства и отправить редактору эскиз обложки",
                           "success": "Редактор ответил хотя бы на одно письмо за неделю",
                           "fail": "Ни одного ответа за 7 дней"}, ensure_ascii=False)
    return "{}"


class PatternRoleTest(unittest.TestCase):
    def ask(self, reply, ideas=FIRST):
        return ai_roles.pattern(llm.AI(llm.FakeClient(lambda s, u: reply)), "задача", "цель", list(ideas))

    def test_verdict_comes_from_levers_not_from_words(self):
        self.assertEqual(self.ask(pattern_reply(("price", "price", "trial")))["verdict"], "partly")
        self.assertEqual(self.ask(pattern_reply(("price", "price", "price")))["verdict"], "one")
        self.assertEqual(self.ask(pattern_reply(("price", "trial", "referral")))["verdict"], "diverse")

    def test_unknown_lever_is_unclear_not_grouped(self):
        r = self.ask(pattern_reply(("price", "price", "magic")))
        self.assertEqual((r["verdict"], [u["n"] for u in r["unclear"]]), ("partly", [3]))

    def test_advice_in_limit_dropped(self):
        r = self.ask(pattern_reply(limit="Сделайте бесплатный пробный урок вместо скидки."))
        self.assertEqual((r["verdict"], r["limit"]), ("one", ""))  # наблюдение остаётся, совет — нет

    def test_one_idea_is_unclear(self):
        self.assertEqual(self.ask(pattern_reply(("price",)), ideas=FIRST[:1])["verdict"], "unclear")

    def test_rules_fallback(self):
        name, mech = LIB.family_name, (lambda c: LIB.families[c]["mechanism"])
        self.assertEqual(engine.pattern_by_families(["F07", "F07", "F07"], name, mech)["verdict"], "one")
        self.assertEqual(engine.pattern_by_families(["F07", "F07", "F02"], name, mech)["verdict"], "partly")
        self.assertEqual(engine.pattern_by_families(["F07", "F02", "F10"], name, mech)["verdict"], "diverse")
        self.assertEqual(engine.pattern_by_families(["F07", None], name, mech)["verdict"], "unclear")


class ChoiceAdviceTest(unittest.TestCase):
    def view(self, origin, feas, rb1=3, kind=None, text="x"):
        return {"origin": origin, "feasibility": feas, "rb1": rb1, "shift_kind": kind, "text": text}

    def test_first_idea_already_strong(self):
        a = training.choice_advice([self.view("first", "высокая"), self.view("version", "средняя", kind="механизм")])
        self.assertTrue(a["first_strong"])
        self.assertEqual(a["novel_risky"]["origin"], "version")  # самый новый — но рискованнее по ресурсам

    def test_feasible_new_mechanism_wins(self):
        a = training.choice_advice([self.view("first", "средняя"), self.view("version", "высокая", kind="механизм")])
        self.assertEqual((a["best"]["origin"], a["first_strong"], a["novel_risky"]), ("version", False, None))

    def test_no_scores_no_advice(self):
        self.assertIsNone(training.choice_advice([self.view("first", None), self.view("version", None)]))

    def test_condition_without_double_if(self):
        # живой прогон 18.10: «сработает, если если издательство ищет иллюстратора»
        for raw in ("если издательство ищет иллюстратора.", "Если издательство ищет иллюстратора",
                    "сработает, если издательство ищет иллюстратора", "издательство ищет иллюстратора"):
            self.assertEqual(training.condition_of({"condition": raw}), "издательство ищет иллюстратора")
        self.assertEqual(training.condition_of(None), "")


class DefaultFlowTest(TrainingCase):
    flow = None  # как в сервисе: путь по умолчанию

    def test_new_sessions_take_short_path(self):
        self.register()
        self.fill_card()
        sid = self.start()
        self.assertEqual(self.session_json(sid, "flags_json")["flow"], "short")
        self.assertIn("Шаг 1 из 7", self.page(f"/s/{sid}/task"))


class ProductFlowTest(A11yMixin, TrainingCase):
    flow = "short"
    ai_client = llm.FakeClient(product_ai)

    def to_idea_short(self, ideas=FIRST):
        self.register()
        self.fill_card()
        sid = self.start()
        self.set_task(sid, bans="без платной рекламы")
        self.submit_first(sid, list(ideas))
        return sid

    def test_rut_switch_choice_and_plan(self):
        sid = self.to_idea_short()
        mirror = self.page(f"/s/{sid}/mirror")
        self.assertIn("Одна колея", mirror)
        self.assertIn("Все 3 идеи держатся на одном способе — ценовое поощрение", mirror)
        self.assertIn("Идеи 1, 2, 3 — один способ: ценовое поощрение.", mirror)
        self.assertIn("«Промокод для новых» — <span class=\"muted\">издательство закажет, потому что так дешевле", mirror)
        self.assertIn("Уточнить главное препятствие — необязательно", mirror)  # линзы свёрнуты в коротком пути
        self.assertIn("По каталогу направлений — грубее, чем разбор выше:", mirror)  # счёт направлений не спорит с колеёй
        self.post(f"/s/{sid}/mirror", {"action": "continue"})
        self.assertEqual(self.session_json(sid, "diagnosis_json")["tool_type"], "switch")
        tool = self.page(f"/s/{sid}/tool")
        self.assertIn("Смена механизма", tool)
        self.assertIn("Ваши первые идеи держатся на одном способе — ценовое поощрение", tool)
        self.assertIn("Другие способы — вопросы для поиска", tool)
        self.assertEqual(tool.count("держатся на одном"), 1)  # вывод один раз, без повтора названием направления
        self.assertNotIn("Новые каналы и модели продаж", tool)  # задание просит не менять канал — его и не предлагаем
        self.assertIn("Ограничения остаются в силе: бюджет — 0 ₽; время — 2–5 ч в неделю; нельзя: без платной рекламы",
                      tool)
        self.post(f"/s/{sid}/tool", {"action": "go"})
        self.idea(sid, "submit", NEW, self_check="no")
        version = self.page(f"/s/{sid}/idea")
        self.assertIn("Уверенность в выводе: средняя", version)
        self.idea(sid, "accept_version")
        self.assertEqual(self.state(sid), "choose")
        choose = self.page(f"/s/{sid}/choose")
        self.assertIn("Держится на допущении: сработает, если книга действительно есть в плане издательства", choose)
        self.assertIn("Чем отличается от первых идей: Раньше издательство получало выгоду", choose)
        self.assertIn("По ресурсам и цели сильнее всего выглядит: «" + NEW, choose)
        new_id = self.q("SELECT id FROM ideas WHERE stage = 'version'")[0]["id"]
        self.post(f"/s/{sid}/choose", {"chosen": str(new_id), "why": "Редактору проще оценить готовую работу",
                                       "wow": "Скорее нет", "action": "check"})
        self.assertEqual(self.state(sid), "check")
        check = self.page(f"/s/{sid}/check")
        self.assertIn('value="Идея сработает, если книга действительно есть в плане издательства"', check)
        self.assertIn('value="редактор издательства"', check)
        self.assertIn("Выбрать 2 книги из плана одного издательства", check)
        self.assertIn('value="Редактор ответил хотя бы на одно письмо за неделю"', check)
        self.assertIn("проверить «Идея сработает, если книга действительно есть в плане издательства»", check)
        self.assertNotIn("формат:", check)  # шаг составлен под идею, а не по шаблону LIB-10
        plan = self.session_json(sid, "flags_json")["check_plan"]
        self.post(f"/s/{sid}/check", {"action": "save", "plan": plan["action"], "when": "завтра",
                                      "hypothesis": plan["hypothesis"], "audience": plan["audience"],
                                      "ok": plan["ok"], "fail": plan["fail"]})
        self.assertEqual(self.state(sid), "done")
        exp = self.q("SELECT hypothesis, audience, success_signal FROM experiments")[0]
        self.assertEqual((exp["audience"], exp["success_signal"]),
                         ("редактор издательства", "Редактор ответил хотя бы на одно письмо за неделю"))
        done = self.page(f"/s/{sid}/done")
        self.assertIn("Проверяем:", done)
        self.assertIn("Пересмотреть, если:", done)
        self.assertAccessible(min_pages=8)

    def test_plan_ready_while_choosing(self):
        sid = self.to_idea_short()
        self.page(f"/s/{sid}/mirror")
        self.post(f"/s/{sid}/mirror", {"action": "continue"})
        self.page(f"/s/{sid}/tool")
        self.post(f"/s/{sid}/tool", {"action": "go"})
        self.idea(sid, "submit", NEW, self_check="no")
        self.idea(sid, "accept_version")
        self.page(f"/s/{sid}/choose")
        new_id = self.q("SELECT id FROM ideas WHERE stage = 'version'")[0]["id"]
        self.assertIn(("plan", sid, new_id), training._PREFETCHED)  # план готовится, пока человек выбирает
        for key, (_, future) in list(training._PREFETCHED.items()):
            if key[:2] == ("plan", sid):
                future.result(timeout=10)
        planned = len([1 for s, u in self.ai_client.calls if "Составь план самой дешёвой проверки" in s])
        self.post(f"/s/{sid}/choose", {"chosen": str(new_id), "why": "проще оценить", "wow": "Да", "action": "check"})
        self.assertIn("Выбрать 2 книги из плана одного издательства", self.page(f"/s/{sid}/check"))
        self.assertEqual(planned, len([1 for s, u in self.ai_client.calls if "Составь план самой дешёвой проверки" in s]))
        self.assertLessEqual(planned, training.PLAN_PREFETCH)

    def test_premortem_on_request_returns_to_check(self):
        sid = self.to_idea_short()
        self.page(f"/s/{sid}/mirror")
        self.post(f"/s/{sid}/mirror", {"action": "continue"})
        self.page(f"/s/{sid}/tool")
        self.post(f"/s/{sid}/tool", {"action": "go"})
        self.idea(sid, "submit", NEW, self_check="no")
        self.idea(sid, "accept_version")
        self.page(f"/s/{sid}/choose")
        new_id = self.q("SELECT id FROM ideas WHERE stage = 'version'")[0]["id"]
        self.post(f"/s/{sid}/choose", {"chosen": str(new_id), "why": "проще оценить", "wow": "Да",
                                       "action": "premortem"})
        self.assertEqual(self.state(sid), "premortem")
        self.assertIn("Проверка на прочность", self.page(f"/s/{sid}/premortem"))
        self.post(f"/s/{sid}/premortem", {"action": "skip"})
        self.assertEqual(self.state(sid), "check")  # в коротком пути после пре-мортема — сразу план


class TestPlanRoleTest(unittest.TestCase):
    """Р5 v2 сам называет допущение и аудиторию: живой прогон 18.10 дал из разбора идеи «сработает, если отправка
    редактору» и «на ком: иллюстратор» — автора идеи вместо клиента."""

    def plan(self, reply):
        return ai_roles.test_plan(llm.AI(llm.FakeClient(lambda s, u: reply)), "Обложка-образец редактору", "Задача",
                                  "3 заказа", "Идея сработает, если отправка редактору", "иллюстратор", "0 ₽", "шаблон")

    def test_assumption_and_audience_from_role(self):
        r = self.plan(json.dumps({"assumption": "Если редактору проще оценить готовую работу.",
                                  "audience": "редакторы малых издательств", "step": "отправить эскиз 3 редакторам",
                                  "success": "ответил хотя бы один", "fail": "ни одного ответа"}, ensure_ascii=False))
        self.assertEqual(r["assumption"], "редактору проще оценить готовую работу")
        self.assertEqual(r["audience"], "редакторы малых издательств")
        self.assertEqual((r["step"][:1], r["success"][:1]), ("О", "О"))  # поля — отдельные строки на экране

    def test_old_reply_still_valid(self):
        r = self.plan(json.dumps({"step": "Отправить эскиз", "success": "Ответ", "fail": "Нет ответа"},
                                 ensure_ascii=False))
        self.assertEqual((r["assumption"], r["audience"]), ("", ""))


def own_plan_ai(system, user):
    if "Составь план самой дешёвой проверки" in system:
        return json.dumps({"assumption": "редактору проще оценить готовую работу к своей книге",
                           "audience": "редакторы малых издательств", "step": "Отправить эскиз обложки 3 редакторам",
                           "success": "Ответил хотя бы один редактор", "fail": "Ни одного ответа за неделю"},
                          ensure_ascii=False)
    return product_ai(system, user)


class OwnPlanInputsTest(TrainingCase):
    flow = "short"
    ai_client = llm.FakeClient(own_plan_ai)

    def test_plan_names_assumption_and_audience(self):
        self.register()
        self.fill_card()
        sid = self.start()
        self.set_task(sid)
        self.submit_first(sid, FIRST)
        self.page(f"/s/{sid}/mirror")
        self.post(f"/s/{sid}/mirror", {"action": "continue"})
        self.post(f"/s/{sid}/tool", {"action": "go"})
        self.idea(sid, "submit", NEW, self_check="no")
        self.idea(sid, "accept_version")
        new_id = self.q("SELECT id FROM ideas WHERE stage = 'version'")[0]["id"]
        self.post(f"/s/{sid}/choose", {"chosen": str(new_id), "why": "проще", "wow": "Да", "action": "check"})
        check = self.page(f"/s/{sid}/check")
        self.assertIn('value="Идея сработает, если редактору проще оценить готовую работу к своей книге"', check)
        self.assertIn('value="редакторы малых издательств"', check)


PAID = "Заказать посты у книжных блогеров, которых читают редакторы"


def paid_version_ai(system, user):
    if "Оцени идею по рубрикам" in system and "блогер" in user:  # новый механизм, но нужен бюджет
        return json.dumps({"criteria": [{"id": k, "level": 3, "evidence": "x", "reason": "x"}
                                        for k in ("RB1", "RB2", "RB3", "RB4")],
                           "needs": {"money_rub": 20000, "hours_per_week": 2}}, ensure_ascii=False)
    return product_ai(system, user)


class ConstraintsAtChoiceTest(TrainingCase):
    """Сценарий 8: вариант новее по механизму, но противоречит ресурсам (бюджет 0 ₽) — выбор честно говорит об этом."""
    flow = "short"
    ai_client = llm.FakeClient(paid_version_ai)

    def test_novel_but_unaffordable(self):
        self.register()
        self.fill_card()
        sid = self.start()
        self.set_task(sid)
        self.submit_first(sid, FIRST)
        self.page(f"/s/{sid}/mirror")
        self.post(f"/s/{sid}/mirror", {"action": "continue"})
        self.post(f"/s/{sid}/tool", {"action": "go"})
        self.idea(sid, "submit", PAID, self_check="no")
        self.idea(sid, "accept_version")
        choose = self.page(f"/s/{sid}/choose")
        self.assertIn("нужно около 20000 ₽, а бюджет — 0 ₽", choose)
        self.assertIn("Самый новый по механизму — «" + PAID, choose)
        self.assertNotIn("По ресурсам и цели сильнее всего выглядит: «" + PAID, choose)


class SwitchSoftenTest(TrainingCase):
    """Сценарий 9: человек не понял задание «смена механизма» — «Облегчить задание» даёт один принцип для старта."""
    flow = "short"
    ai_client = llm.FakeClient(product_ai)

    def test_soften_switch(self):
        self.register()
        self.fill_card()
        sid = self.start()
        self.set_task(sid)
        self.submit_first(sid, FIRST)
        self.page(f"/s/{sid}/mirror")
        self.post(f"/s/{sid}/mirror", {"action": "continue"})
        self.page(f"/s/{sid}/tool")
        self.assertEqual(self.session_json(sid, "tool_json")["type"], "switch")
        self.post(f"/s/{sid}/tool", {"action": "soften"})
        tool = self.page(f"/s/{sid}/tool")
        self.assertIn("Задание облегчено", tool)
        self.assertIn("Начните с одного направления — «", tool)


class DiverseIdeasTest(TrainingCase):
    flow = "short"
    ai_client = llm.FakeClient(lambda s, u: product_ai(s, u, pattern=pattern_reply(("price", "trial", "reach"))))

    def test_no_fake_rut(self):
        self.register()
        self.fill_card()
        sid = self.start()
        self.set_task(sid)
        self.submit_first(sid, ["Скидка на первый месяц", "Бесплатный первый урок", "Объявления на Авито"])
        mirror = self.page(f"/s/{sid}/mirror")
        self.assertIn("Разные механизмы", mirror)
        self.assertNotIn("Одна колея", mirror)
        self.post(f"/s/{sid}/mirror", {"action": "continue"})
        self.assertNotEqual(self.session_json(sid, "diagnosis_json")["tool_type"], "switch")


class LowConfidenceTest(TrainingCase):
    flow = "short"
    ai_client = llm.FakeClient(lambda s, u: product_ai(s, u, pattern=pattern_reply(confidence="low")))

    def test_unsure_rut_does_not_drive_the_exercise(self):
        self.register()
        self.fill_card()
        sid = self.start()
        self.set_task(sid)
        self.submit_first(sid, ["скидка", "промо", "акция"])
        self.assertIn("уверенность низкая", self.page(f"/s/{sid}/mirror"))
        self.post(f"/s/{sid}/mirror", {"action": "continue"})
        self.assertNotEqual(self.session_json(sid, "diagnosis_json")["tool_type"], "switch")


def slow_pattern_ai(system, user):
    if "Найди общее в первых идеях" in system:
        import time
        time.sleep(training.PATTERN_WAIT_PAGE + 1)  # разбор дольше, чем зеркало его ждёт
    return product_ai(system, user)


class SlowPatternTest(TrainingCase):
    """Разбор «что общего» не держит зеркало: оно открывается сразу, вывод подгружается, задание ждёт его."""
    flow = "short"
    ai_client = llm.FakeClient(slow_pattern_ai)

    def test_mirror_first_then_insight(self):
        self.register()
        self.fill_card()
        sid = self.start()
        self.set_task(sid)
        self.submit_first(sid, FIRST)
        page = self.page(f"/s/{sid}/mirror")
        self.assertIn("Разбор по механизму ещё идёт", page)
        self.assertIn(f'data-poll="/s/{sid}/mirror/pattern"', page)
        fragment = self.c.get(f"/s/{sid}/mirror/pattern")
        self.assertEqual(fragment.status_code, 200)
        self.assertIn("Все 3 идеи держатся на одном способе — ценовое поощрение", fragment.get_data(as_text=True))
        self.assertIn("Одна колея", self.page(f"/s/{sid}/mirror"))  # теперь — в самой странице
        self.post(f"/s/{sid}/mirror", {"action": "continue"})
        self.assertEqual(self.session_json(sid, "diagnosis_json")["tool_type"], "switch")


class FailingAIShortPathTest(TrainingCase):
    """ИИ недоступен на каждом шаге — путь всё равно доходит до плана: всё на правилах и библиотеках."""
    flow = "short"
    ai_client = llm.FakeClient(failing_ai)

    def test_completes_on_fallbacks(self):
        self.register()
        self.fill_card()
        sid = self.start()
        self.set_task(sid)
        self.submit_first(sid, FIRST)
        mirror = self.page(f"/s/{sid}/mirror")
        self.assertIn("Грубо, по направлениям", mirror)
        self.post(f"/s/{sid}/mirror", {"action": "continue"})
        self.page(f"/s/{sid}/tool")
        tool = self.session_json(sid, "tool_json")
        if tool["phase"] == "target":
            self.post(f"/s/{sid}/tool", {"action": "target", "target": tool["targets"][0]})
        if self.session_json(sid, "tool_json")["phase"] == "select":
            self.post(f"/s/{sid}/tool", {"action": "select", "chosen": tool["items"][0]["id"]})
        self.post(f"/s/{sid}/tool", {"action": "go"})
        self.submit_final(sid, "Стенд на книжной ярмарке")
        self.assertEqual(self.state(sid), "choose")
        fav = self.q("SELECT id FROM ideas WHERE stage = 'first' AND is_favorite = 1")[0]["id"]
        self.post(f"/s/{sid}/choose", {"chosen": str(fav), "why": "дешевле", "wow": "Да", "action": "check"})
        self.assertEqual(self.state(sid), "check")
        check = self.page(f"/s/{sid}/check")
        self.assertIn('value="Что идея вызовет нужное действие клиента"', check)  # разбора нет — вопрос задан
        self.post(f"/s/{sid}/check", {"action": "skip"})
        self.assertEqual(self.state(sid), "done")


if __name__ == "__main__":
    unittest.main()
