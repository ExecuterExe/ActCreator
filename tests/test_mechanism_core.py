"""Ядро продукта (19.10): диагностика колеи, смена механизма, «До → После», план на 48 часов — 12 сценариев ТЗ.

Ответы модели здесь заданы вручную (FakeClient) так, как ответила бы верная модель, — тесты проверяют, что сервис
делает с этими ответами: как считает колею, когда даёт и не даёт задание, как отвечает на поправку человека, что
пишет о результате. Ожидания взяты из требований, а не из текущих ответов модели: например, «переименованная скидка
не называется сменой механизма», «сбой модели не превращается в успех». Это синтетические проверки логики, а не
проверка качества модели: её качество — eval pattern / shift (разметка Claude, не люди). Настоящий API не вызывается."""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ai_roles  # noqa: E402
import llm  # noqa: E402
from helpers import TrainingCase  # noqa: E402
from test_product_flow import product_ai  # noqa: E402

PRICE_IDEAS = ["Скидка на первый заказ", "Скидка по программе лояльности", "Промокод на следующую покупку"]
SAMPLE = "Нарисовать обложку-образец к книге из плана издательства и отправить редактору"
REWORDED = "Бонусные баллы за первый заказ вместо скидки"
PAID = "Заказать платную рекламу у книжных блогеров, которых читают редакторы"


def part(lever, because, tool="", clear=True, missing=""):
    return {"result": "новые заказы", "tool": tool, "because": because, "lever": lever if clear else "unclear",
            "clear": clear, "missing": missing}


PRICE_PARTS = [part("price", "издательство закажет, потому что первый заказ дешевле", "скидка"),
               part("price", "издательство вернётся, потому что постоянным дешевле", "скидка лояльности"),
               part("price", "издательство купит снова, потому что промокод даёт выгоду", "промокод")]


def pattern_json(parts, confidence="high", limit="Ценовое поощрение помогает, только если издательство "
                                                 "останавливает цена, а не то, что оно вас не знает.", **extra):
    return json.dumps(dict({"ideas": parts, "limit": limit, "confidence": confidence}, **extra), ensure_ascii=False)


def shift_reply(kind, levers, confidence="high", same_as=None, **extra):
    data = {"new_driver": "редактор видит работу к своей книге" if kind == "механизм" else "денежная выгода",
            "new_barrier": "непонятно, подойдёт ли стиль" if kind == "механизм" else "цена",
            "shift": kind, "same_as": same_as, "changed": ["механизм"] if kind == "механизм" else [],
            "kept": [] if kind == "механизм" else ["механизм"], "confidence": confidence,
            "explanation": "Клиента побуждает готовый образец, а не выгода." if kind == "механизм"
            else "Принцип тот же — выгода, поменялась только форма.",
            "levers": levers, "goal_kept": True,
            "opens": "Редактор оценивает работу к своей книге, а не портфолио вообще." if kind == "механизм" else "",
            "risk": "Нужно угадать стиль книги без брифа." if kind == "механизм" else ""}
    data.update(extra)
    return json.dumps(data, ensure_ascii=False)


def strong_rubric(money=0):
    return json.dumps({"criteria": [{"id": k, "level": 3, "evidence": "x", "reason": "укладывается"}
                                    for k in ("RB1", "RB2", "RB3", "RB4")],
                       "needs": {"money_rub": money, "hours_per_week": 2}}, ensure_ascii=False)


def weak_rubric():
    return json.dumps({"criteria": [{"id": "RB1", "level": 2, "evidence": "x", "reason": "косвенно"},
                                    {"id": "RB2", "level": 2, "evidence": "x", "reason": "почти весь ресурс"},
                                    {"id": "RB3", "level": 1, "evidence": "x", "reason": "шаг размыт"},
                                    {"id": "RB4", "level": 2, "evidence": "x", "reason": "x"}],
                       "needs": {"money_rub": 0, "hours_per_week": 4}}, ensure_ascii=False)


def scripted(pattern=None, shift=None, rubric=None, plan=None):
    """Ответы ролей по сценарию; всё остальное — как в test_product_flow. shift(новая идея) и rubric(идея) — функции;
    ответ-исключение имитирует сбой модели."""
    def respond(system, user):
        if pattern is not None and "Найди общее в первых идеях" in system:
            return pattern
        if shift is not None and "аналитик механизмов" in system:
            return shift(user.split("Новая идея:")[-1])
        if rubric is not None and "Оцени идею по рубрикам" in system:
            return rubric(user.split("Идея:")[-1] if "Идея:" in user else user)
        if plan is not None and "Составь план самой дешёвой проверки" in system:
            return plan(user)
        return product_ai(system, user)
    return respond


def by_text(changed, same=None, default=None):
    """Р12 по тексту новой идеи: обложка — смена механизма, переименованная скидка — та же ветка."""
    def answer(new):
        if "обложк" in new:
            return changed
        if same is not None and ("Бонус" in new or "скидк" in new.lower()):
            return same
        return default if default is not None else changed
    return answer


class CoreCase(TrainingCase):
    flow = "short"

    def begin(self, ideas, task=None, card=None):
        self.register()
        self.fill_card(**(card or {}))
        sid = self.start()
        self.set_task(sid, **(task or {}))
        self.submit_first(sid, list(ideas))
        return sid

    def pattern(self, sid):
        return self.session_json(sid, "flags_json").get("pattern") or {}

    def through_mirror(self, sid, **form):
        mirror = self.page(f"/s/{sid}/mirror")
        self.post(f"/s/{sid}/mirror", dict({"action": "continue"}, **form))
        return mirror

    def version(self, sid, text):
        self.idea(sid, "submit", text, self_check="no")
        return self.page(f"/s/{sid}/idea")


# --- 1. Один механизм при разных формулировках ---

class SameMechanismTest(CoreCase):
    ai_client = llm.FakeClient(scripted(pattern=pattern_json(PRICE_PARTS)))

    def test_rut_is_explained_by_the_ideas_and_drives_a_targeted_exercise(self):
        sid = self.begin(PRICE_IDEAS)
        mirror = self.through_mirror(sid)
        p = self.pattern(sid)
        self.assertEqual((p["verdict"], p["confidence"]), ("one", "high"))
        self.assertIn("Одна колея", mirror)
        self.assertNotIn("Возможно, одна колея", mirror)
        self.assertIn("ценовое поощрение", mirror)
        for idea in PRICE_IDEAS:  # объяснение опирается на сами идеи, а не только называет их похожими
            self.assertIn(f"«{idea}»", mirror)
        self.assertIn("Чем ограничен этот путь:", mirror)
        self.assertEqual(self.session_json(sid, "diagnosis_json")["tool_type"], "switch")
        tool = self.page(f"/s/{sid}/tool")
        self.assertIn("скидки, промокоды", tool)  # запрещён весь способ, а не одно слово
        self.assertIn("Заменить одно из них другим — это не смена механизма", tool)
        self.assertIn("Цель та же — 3 новых заказа за месяц", tool)
        self.assertIn("Ограничения остаются в силе: бюджет — 0 ₽", tool)
        directions = self.session_json(sid, "tool_json")["switch_principles"]
        self.assertTrue(directions)
        self.assertNotIn("price", {d["code"] for d in directions})  # направления — другие способы


# --- 2. Похожие слова — разные механизмы ---

class SimilarWordsTest(CoreCase):
    # модель разобрала причины верно, но сверху всё равно приписала «одна колея» — сервис ей не верит
    ai_client = llm.FakeClient(scripted(pattern=pattern_json(
        [part("price", "новичок придёт, потому что первая тренировка вдвое дешевле", "акция"),
         part("habit", "участник ходит, потому что не хочет прервать серию и потерять уровень", "акция"),
         part("referral", "друг придёт, потому что его позвал знакомый", "акция")],
        verdict="one", groups=[{"ideas": [1, 2, 3], "shared": "акции"}])))

    def test_shared_words_do_not_make_a_rut(self):
        sid = self.begin(["Акция для новых: первая тренировка за полцены",
                          "Акция «30 дней подряд»: каждая тренировка открывает новый уровень",
                          "Акция с партнёрами: приведи друга — оба получаете неделю бесплатно"])
        mirror = self.through_mirror(sid)
        self.assertEqual(self.pattern(sid)["verdict"], "diverse")
        self.assertNotIn("Одна колея", mirror)
        self.assertIn("Разные механизмы", mirror)
        self.assertNotEqual(self.session_json(sid, "diagnosis_json")["tool_type"], "switch")
        tool = self.page(f"/s/{sid}/tool")
        self.assertNotIn("работают через одно и то же", tool)  # правила по словарю не спорят с разбором


# --- 3. Одна цель и канал — разные способы ---

class SameGoalDifferentWaysTest(CoreCase):
    ai_client = llm.FakeClient(scripted(pattern=pattern_json(
        [part("proof", "клиентка запишется, потому что видит результат других", "Instagram"),
         part("trial", "клиентка запишется, потому что уже попробовала бесплатно", "Instagram"),
         part("referral", "клиентка придёт, потому что её привела подруга", "Instagram")])))

    def test_same_goal_audience_and_channel_is_not_a_rut(self):
        sid = self.begin(["Пост в Instagram с фото работ и отзывами клиенток",
                          "Сторис в Instagram: бесплатная мини-консультация по уходу",
                          "Розыгрыш в Instagram: приведи подругу — обе получите уход"])
        mirror = self.through_mirror(sid)
        p = self.pattern(sid)
        self.assertEqual((p["verdict"], p["groups"]), ("diverse", []))
        self.assertIn("Идеи работают по-разному", mirror)
        self.assertNotEqual(self.session_json(sid, "diagnosis_json")["tool_type"], "switch")


# --- 4. Слишком кратко ---

class TooBriefTest(CoreCase):
    ai_client = llm.FakeClient(scripted(pattern=pattern_json(
        [part("price", "", clear=False, missing="не сказано, что за акция и кому"),
         part("reach", "", clear=False, missing="не сказано, что публиковать и почему клиент придёт"),
         part("referral", "", clear=False, missing="не сказано, какие партнёры и зачем им это")],
        confidence="high")))

    def test_unclear_ideas_give_no_rut_and_say_what_is_missing(self):
        sid = self.begin(["Акция", "Соцсети", "Партнёры"])
        mirror = self.through_mirror(sid)
        p = self.pattern(sid)
        self.assertEqual((p["verdict"], p["confidence"]), ("unclear", "low"))  # «high» модели не выживает
        self.assertIn("вывод о колее делать рано", mirror)
        self.assertIn("не хватает: не сказано, что публиковать и почему клиент придёт", mirror)
        self.assertNotEqual(self.session_json(sid, "diagnosis_json")["tool_type"], "switch")


class OneBriefIdeaTest(CoreCase):
    ai_client = llm.FakeClient(scripted(pattern=pattern_json(
        PRICE_PARTS[:2] + [part("reach", "", clear=False, missing="не сказано, что публиковать")])))

    def test_brief_idea_is_left_out_and_lowers_confidence(self):
        sid = self.begin(["Скидка 10% на первый заказ", "Скидка 20% постоянным", "Соцсети"])
        mirror = self.through_mirror(sid)
        p = self.pattern(sid)
        self.assertEqual((p["verdict"], p["confidence"]), ("partly", "medium"))
        self.assertEqual([u["n"] for u in p["unclear"]], [3])
        self.assertIn("Идея 3 в вывод не вошла", mirror)
        self.assertNotEqual(self.session_json(sid, "diagnosis_json")["tool_type"], "switch")


# --- 5. Несколько механизмов не сводятся к одному ---

class SeveralMechanismsTest(CoreCase):
    ai_client = llm.FakeClient(scripted(pattern=pattern_json(
        PRICE_PARTS[:2] + [part("trial", "редактор закажет, потому что увидел образец", "образец"),
                           part("trial", "редактор закажет, потому что получил бесплатный эскиз", "эскиз")])))

    def test_two_groups_stay_two(self):
        sid = self.begin(["Скидка 10% на первый заказ", "Скидка 20% постоянным",
                          "Образец обложки к книге из плана", "Бесплатный эскиз одной иллюстрации"])
        mirror = self.through_mirror(sid)
        p = self.pattern(sid)
        self.assertEqual(p["verdict"], "partly")
        self.assertEqual(sorted(g["lever"] for g in p["groups"]), ["price", "trial"])
        self.assertIn("ценовое поощрение", mirror)
        self.assertIn("попробовать до решения", mirror)
        self.assertNotEqual(self.session_json(sid, "diagnosis_json")["tool_type"], "switch")


# --- 6. Человек отвергает вывод о колее ---

class UserCorrectionTest(CoreCase):
    ai_client = llm.FakeClient(scripted(pattern=pattern_json(PRICE_PARTS)))

    def test_rejected_rut_changes_what_happens_next(self):
        sid = self.begin(PRICE_IDEAS)
        self.page(f"/s/{sid}/mirror")
        self.post(f"/s/{sid}/mirror", {"action": "pattern_verdict", "verdict": "diverse"})
        mirror = self.page(f"/s/{sid}/mirror")
        self.assertIn("Разные механизмы — по вашей оценке", mirror)
        self.assertIn("задание «смена механизма» не предлагаем", mirror)
        self.post(f"/s/{sid}/mirror", {"action": "continue"})
        diag = self.session_json(sid, "diagnosis_json")
        self.assertNotEqual(diag["tool_type"], "switch")
        self.assertEqual(diag["pattern"]["user"], "diverse")
        tool = self.page(f"/s/{sid}/tool")
        self.assertNotIn("держатся на одном способе", tool)
        self.assertNotIn("работают через одно и то же", tool)
        events = [r["name"] for r in self.q("SELECT name FROM events")]
        self.assertIn("pattern_corrected", events)

    def test_unclear_answer_also_stops_the_exercise(self):
        sid = self.begin(PRICE_IDEAS)
        self.page(f"/s/{sid}/mirror")
        self.post(f"/s/{sid}/mirror", {"action": "pattern_verdict", "verdict": "unclear"})
        self.post(f"/s/{sid}/mirror", {"action": "continue"})
        self.assertNotEqual(self.session_json(sid, "diagnosis_json")["tool_type"], "switch")

    def test_rejection_on_the_exercise_page_rebuilds_the_exercise(self):
        sid = self.begin(PRICE_IDEAS)
        self.through_mirror(sid)
        self.page(f"/s/{sid}/tool")
        self.assertEqual(self.session_json(sid, "tool_json")["type"], "switch")
        self.post(f"/s/{sid}/tool", {"action": "not_rut"})
        self.assertNotEqual(self.session_json(sid, "tool_json")["type"], "switch")
        self.assertEqual(self.session_json(sid, "flags_json")["pattern_user"], "diverse")
        self.assertNotIn("держатся на одном способе", self.page(f"/s/{sid}/tool"))


class UnsureRutTest(CoreCase):
    ai_client = llm.FakeClient(scripted(pattern=pattern_json(PRICE_PARTS, confidence="medium")))

    def test_possible_rut_asks_and_confirmation_starts_the_exercise(self):
        sid = self.begin(PRICE_IDEAS)
        mirror = self.page(f"/s/{sid}/mirror")
        self.assertIn("Возможно, одна колея", mirror)
        self.assertIn("разбор не уверен. Как по-вашему?", mirror)
        self.post(f"/s/{sid}/mirror", {"action": "pattern_verdict", "verdict": "one"})
        self.assertIn("Одна колея — по вашей оценке", self.page(f"/s/{sid}/mirror"))
        self.post(f"/s/{sid}/mirror", {"action": "continue"})
        self.assertEqual(self.session_json(sid, "diagnosis_json")["tool_type"], "switch")

    def test_no_answer_means_no_mandatory_exercise(self):
        sid = self.begin(PRICE_IDEAS)
        self.through_mirror(sid)
        self.assertNotEqual(self.session_json(sid, "diagnosis_json")["tool_type"], "switch")


# --- 7–8. Новая формулировка без нового механизма и настоящая смена ---

class ExerciseOutcomeTest(CoreCase):
    ai_client = llm.FakeClient(scripted(
        pattern=pattern_json(PRICE_PARTS),
        shift=by_text(changed=shift_reply("механизм", ["trial"]),
                      same=shift_reply("форма", ["price"], same_as=1))))

    def start_exercise(self):
        sid = self.begin(PRICE_IDEAS)
        self.through_mirror(sid)
        self.page(f"/s/{sid}/tool")
        self.post(f"/s/{sid}/tool", {"action": "go"})
        return sid

    def test_reworded_discount_is_not_a_mechanism_shift(self):
        sid = self.start_exercise()
        page = self.version(sid, REWORDED)
        self.assertNotIn("Сдвиг механизма", page)
        self.assertIn("Та же ветка", page)
        self.assertIn("механизм по существу остался прежним", page)
        self.assertIn("Повторялся способ — <b>ценовое поощрение</b>", page)
        self.assertIn("«переход пока не состоялся» — тоже честный результат", page)
        self.assertIn('value="improve" class="primary"', page)  # главная кнопка — ещё одна попытка
        outcome = json.loads(self.q("SELECT change_json FROM ideas WHERE stage = 'version'")[0]["change_json"])["outcome"]
        self.assertEqual(outcome["verdict"], "same")

    def test_real_shift_is_shown_before_and_after(self):
        sid = self.start_exercise()
        page = self.version(sid, SAMPLE)
        self.assertIn("Сдвиг механизма", page)
        self.assertIn("механизм действительно изменился", page)
        self.assertIn("Был способ «ценовое поощрение», стал «попробовать до решения»", page)
        self.assertIn("Что это открывает:", page)
        self.assertIn("Новые риски и допущения:", page)
        self.assertIn("не обещание результата", page)  # смена механизма ≠ коммерческий успех
        self.idea(sid, "accept_version")
        self.assertIn("· сдвиг механизма", self.page(f"/s/{sid}/choose"))


class ConflictingSignalsTest(CoreCase):
    # детектор говорит «механизм», но способ воздействия новой идеи — тот же, что у первых идей
    ai_client = llm.FakeClient(scripted(pattern=pattern_json(PRICE_PARTS),
                                        shift=lambda new: shift_reply("механизм", ["price"])))

    def test_conflict_is_partial_not_success(self):
        sid = self.begin(PRICE_IDEAS)
        self.through_mirror(sid)
        self.post(f"/s/{sid}/tool", {"action": "go"})
        page = self.version(sid, "Платите столько, сколько считаете справедливым за первую обложку")
        self.assertNotIn("Сдвиг механизма", page)
        self.assertIn("Частичный сдвиг", page)
        self.assertIn("способ воздействия тот же, что у первых идей, — «ценовое поощрение»", page)


# --- 9. Другой механизм, но нарушено ограничение ---

class ShiftAgainstConstraintsTest(CoreCase):
    ai_client = llm.FakeClient(scripted(
        pattern=pattern_json(PRICE_PARTS), shift=lambda new: shift_reply("механизм", ["proof"]),
        rubric=lambda idea: strong_rubric(money=20000 if "блогер" in idea else 0)))

    def test_mechanism_changed_but_constraint_flagged_and_not_recommended(self):
        sid = self.begin(PRICE_IDEAS, task={"bans": "без платной рекламы"})
        self.through_mirror(sid)
        self.post(f"/s/{sid}/tool", {"action": "go"})
        page = self.version(sid, PAID)
        self.assertIn("Сдвиг механизма", page)  # механизм действительно другой — это не скрываем
        self.assertIn("Проверьте ограничения:", page)
        self.assertIn("бюджет — 0 ₽", page)
        self.assertIn("«без платной рекламы»", page)
        self.idea(sid, "accept_version")
        choose = self.page(f"/s/{sid}/choose")
        self.assertIn("нужно около 20000 ₽, а бюджет — 0 ₽", choose)
        self.assertNotIn("По ресурсам и цели сильнее всего выглядит: «" + PAID, choose)


# --- 10. Первая идея уже сильна — упражнение не навязывается ---

class StrongFirstIdeaTest(CoreCase):
    ai_client = llm.FakeClient(scripted(pattern=pattern_json(PRICE_PARTS), rubric=lambda idea: strong_rubric()))

    def test_exercise_is_optional_and_can_be_skipped(self):
        sid = self.begin(PRICE_IDEAS)
        self.through_mirror(sid)
        tool = self.page(f"/s/{sid}/tool")
        self.assertIn("Ваша первая идея уже выглядит выполнимой", tool)
        self.assertIn('value="skip_switch" class="primary"', tool)
        self.post(f"/s/{sid}/tool", {"action": "skip_switch"})
        self.assertEqual(self.state(sid), "choose")
        self.assertEqual(self.q("SELECT COUNT(*) AS n FROM ideas WHERE stage = 'version'")[0]["n"], 0)
        choose = self.page(f"/s/{sid}/choose")
        self.assertIn(PRICE_IDEAS[0], choose)
        first = self.q("SELECT id FROM ideas WHERE stage = 'first' AND is_favorite = 1")[0]["id"]
        self.post(f"/s/{sid}/choose", {"chosen": str(first), "why": "уже работает", "wow": "Нет", "action": "check"})
        self.assertEqual(self.state(sid), "check")


class WeakFirstIdeaTest(CoreCase):
    ai_client = llm.FakeClient(scripted(pattern=pattern_json(PRICE_PARTS), rubric=lambda idea: weak_rubric()))

    def test_no_strong_note_for_a_weak_idea(self):
        sid = self.begin(PRICE_IDEAS)
        self.through_mirror(sid)
        tool = self.page(f"/s/{sid}/tool")
        self.assertNotIn("Ваша первая идея уже выглядит выполнимой", tool)
        self.assertIn('value="skip_switch" class="link"', tool)  # возможность есть, но не подталкиваем


# --- 11. Сбой модели не превращается в успех ---

class ShiftCheckFailureTest(CoreCase):
    ai_client = llm.FakeClient(scripted(pattern=pattern_json(PRICE_PARTS),
                                        shift=lambda new: llm.LLMError("тайм-аут")))

    def test_failed_check_says_nothing_about_success(self):
        sid = self.begin(PRICE_IDEAS)
        self.through_mirror(sid)
        self.post(f"/s/{sid}/tool", {"action": "go"})
        page = self.version(sid, SAMPLE)  # по словарю — другое направление: раньше это давало «сдвиг»
        self.assertNotIn("Сдвиг механизма", page)
        self.assertIn("Вывод не сделан", page)
        self.assertIn("ничего не утверждаем", page)
        self.idea(sid, "accept_version")
        self.assertNotIn("· сдвиг механизма", self.page(f"/s/{sid}/choose"))


class IncompleteShiftAnswerTest(CoreCase):
    ai_client = llm.FakeClient(scripted(
        pattern=pattern_json(PRICE_PARTS),
        shift=lambda new: json.dumps({"new_driver": "образец", "new_barrier": "стиль", "shift": "механизм",
                                      "changed": ["механизм"], "explanation": "Другой механизм."},
                                     ensure_ascii=False)))  # без уверенности и способа

    def test_incomplete_answer_is_not_a_success(self):
        sid = self.begin(PRICE_IDEAS)
        self.through_mirror(sid)
        self.post(f"/s/{sid}/tool", {"action": "go"})
        page = self.version(sid, SAMPLE)
        self.assertNotIn("Сдвиг механизма", page)
        self.assertIn("Вывод не сделан", page)
        self.assertIn("Разбор механизма сейчас не получился", page)


class VagueNewIdeaTest(CoreCase):
    # живой eval 19.10: на «Сделать что-то интересное» модель ответила «механизм» с уверенностью medium
    ai_client = llm.FakeClient(scripted(pattern=pattern_json(PRICE_PARTS),
                                        shift=lambda new: shift_reply("механизм", ["experience"], confidence="medium")))

    def test_vague_idea_gets_no_verdict(self):
        sid = self.begin(PRICE_IDEAS)
        self.through_mirror(sid)
        self.post(f"/s/{sid}/tool", {"action": "go"})
        page = self.version(sid, "Сделать что-то интересное")
        self.assertNotIn("Сдвиг механизма", page)
        self.assertIn("По описанию не видно, почему клиент сделает шаг", page)


class OutcomeTableTest(unittest.TestCase):
    """Вывод по сверке способов: успех — только когда способ новой идеи не встречался в первых идеях."""
    import training

    def verdict(self, kind, levers, old=("price",), **vf):
        return self.training.outcome_verdict(dict({"source": "ai", "shift": kind, "levers": levers,
                                                   "confidence": "high"}, **vf), set(old))[0]

    def test_table(self):
        self.assertEqual(self.verdict("механизм", ["trial"]), "changed")
        self.assertEqual(self.verdict("форма", ["price"]), "same")
        self.assertEqual(self.verdict("механизм", ["price"]), "partial")  # детектор и способы расходятся
        self.assertEqual(self.verdict("механизм", ["price", "risk"]), "partial")  # прежний способ остался
        self.assertEqual(self.verdict("механизм", ["trial"], confidence="low"), "unknown")
        self.assertEqual(self.verdict("механизм", ["trial"], clear=False), "unknown")
        self.assertEqual(self.training.outcome_verdict(None, {"price"})[0], "unknown")


class PatternFailureTest(CoreCase):
    ai_client = llm.FakeClient(scripted(pattern='{"ideas": []}'))  # неполный ответ Р16

    def test_no_rut_claim_without_analysis(self):
        sid = self.begin(PRICE_IDEAS)
        mirror = self.through_mirror(sid)
        self.assertIn("разбор по механизму недоступен", mirror)
        self.assertNotEqual(self.session_json(sid, "diagnosis_json")["tool_type"], "switch")


# --- 12. План на 48 часов: конкретное действие и наблюдаемый сигнал ---

VAGUE_PLAN = json.dumps({"assumption": "редакторам интересен образец", "audience": "редакторы",
                         "step": "Изучить аудиторию издательств и собрать обратную связь",
                         "success": "Редакторы проявят интерес", "fail": "Интереса не будет",
                         "today": "Изучить рынок"}, ensure_ascii=False)
CONCRETE_PLAN = json.dumps({"assumption": "редактор ответит на образец к своей книге", "audience": "редакторы",
                            "step": "Отправить эскиз обложки к книге из плана 3 редакторам малых издательств",
                            "success": "Хотя бы 1 редактор ответит за 48 часов",
                            "fail": "Ни одного ответа за 48 часов",
                            "today": "Выписать 3 книги из планов издательств и найти почту редакторов"},
                           ensure_ascii=False)
PAID_PLAN = json.dumps({"assumption": "реклама приведёт заказы", "audience": "редакторы",
                        "step": "Запустить таргетированную рекламу на 3 дня", "success": "5 заявок",
                        "fail": "0 заявок", "today": "Настроить кабинет"}, ensure_ascii=False)


class PlanRoleTest(unittest.TestCase):
    def ask(self, replies, no_money=False):
        answers = iter(replies)
        client = llm.FakeClient(lambda s, u: next(answers))
        r = ai_roles.test_plan(llm.AI(client), SAMPLE, "Задача", "3 заказа", "", "", "бюджет 0 ₽", "шаблон",
                               no_money=no_money)
        return r, len(client.calls)

    def test_vague_advice_is_rejected_and_concrete_plan_kept(self):
        r, calls = self.ask([VAGUE_PLAN, CONCRETE_PLAN])
        self.assertEqual(calls, 2)
        self.assertIn("3 редакторам", r["step"])
        self.assertRegex(r["success"], r"\d")
        self.assertTrue(r["today"])

    def test_two_vague_answers_give_no_ai_plan(self):
        self.assertIsNone(self.ask([VAGUE_PLAN, VAGUE_PLAN])[0])

    def test_paid_step_rejected_when_budget_is_zero(self):
        self.assertIsNone(self.ask([PAID_PLAN, PAID_PLAN], no_money=True)[0])
        self.assertIsNotNone(self.ask([PAID_PLAN])[0])  # бюджет есть — платный шаг допустим


class PlanInFlowTest(CoreCase):
    ai_client = llm.FakeClient(scripted(pattern=pattern_json(PRICE_PARTS),
                                        shift=lambda new: shift_reply("механизм", ["trial"]),
                                        plan=lambda user: CONCRETE_PLAN))

    def test_check_page_has_action_signals_and_first_step_today(self):
        sid = self.begin(PRICE_IDEAS)
        self.through_mirror(sid)
        self.post(f"/s/{sid}/tool", {"action": "go"})
        self.version(sid, SAMPLE)
        self.idea(sid, "accept_version")
        new_id = self.q("SELECT id FROM ideas WHERE stage = 'version'")[0]["id"]
        self.post(f"/s/{sid}/choose", {"chosen": str(new_id), "why": "проще оценить", "wow": "Да", "action": "check"})
        check = self.page(f"/s/{sid}/check")
        self.assertIn("Отправить эскиз обложки к книге из плана 3 редакторам малых издательств", check)
        self.assertIn('value="Хотя бы 1 редактор ответит за 48 часов"', check)
        self.assertIn("С чего начать уже сегодня (15–30 минут): Выписать 3 книги", check)
        self.assertNotIn("Изучить аудиторию", check)


class ShortPathStillDefaultTest(TrainingCase):
    flow = None  # как в сервисе

    def test_default_is_short(self):
        self.register()
        self.fill_card()
        sid = self.start()
        self.assertEqual(self.session_json(sid, "flags_json")["flow"], "short")
