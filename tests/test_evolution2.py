"""Design Evolution 2.0: хирургия идеи, паспорт происхождения, «чистый лист» и честные состояния ожидания.

Главное, что проверяем: всё показанное выводится из сохранённых данных — «осталось прежним» только когда это
назвал ИИ, слова «из подсказок» — ровно основа доли авторства (A4 §2.7), время — из меток сохранения."""

import json
import unittest
from datetime import timedelta

import ai_roles
import llm
import training
from helpers import TrainingCase
from test_e2e import contract_ai


def kept_ai(system, user):
    """Контрактные ответы, где детектор сдвига называет и изменившееся, и оставшееся."""
    if "Сравни новую идею" in system or "аналитик механизмов" in system:
        if "обложк" in user.split("Новая идея:")[-1]:
            return json.dumps({"new_driver": "уверенность в стиле", "new_barrier": "неопределённость",
                               "shift": "механизм", "changed": ["источник ценности", "чепуха"],
                               "kept": ["аудитория", "источник ценности"], "same_as": None, "confidence": "high",
                               "explanation": "Теперь редактор может оценить работу до заказа."}, ensure_ascii=False)
        return json.dumps({"new_driver": "выгода", "new_barrier": "цена",
                           "shift": "форма", "changed": ["механизм", "момент"], "kept": ["ресурсы"], "same_as": 1,
                           "confidence": "high", "explanation": "Принцип тот же — выгода."}, ensure_ascii=False)
    return contract_ai(system, user)


class ShiftCheckKeptTest(unittest.TestCase):
    def check(self, reply):
        ai = llm.AI(llm.FakeClient(lambda s, u: json.dumps(reply, ensure_ascii=False)))
        return ai_roles.shift_check(ai, [{"text": "Скидка", "xray": None}], "Новая идея", None)

    def test_kept_is_filtered_and_disjoint(self):
        r = self.check({"shift": "механизм", "changed": ["источник ценности", "чепуха"],
                        "kept": ["аудитория", "источник ценности", "что-то"], "same_as": None, "explanation": "x"})
        self.assertEqual(r["changed"], ["механизм", "источник ценности"])  # вид сдвига = измерение «механизм»
        self.assertEqual(r["kept"], ["аудитория"])

    def test_same_branch_keeps_mechanism(self):
        r = self.check({"shift": "форма", "changed": ["механизм", "момент"], "same_as": 1, "explanation": "x"})
        self.assertEqual((r["changed"], r["kept"]), (["момент"], ["механизм"]))

    def test_old_answers_without_kept_still_valid(self):
        r = self.check({"shift": "механизм", "changed": ["механизм"], "same_as": None, "explanation": "x"})
        self.assertEqual(r["kept"], [])


class ShiftCheckV2Test(unittest.TestCase):
    """Кандидат Р12 v2 (сравнивается с рабочим промптом в eval_prompts.py): разбор ответа модели."""

    def test_parses_mechanism_confidence_and_strips_ready_ideas(self):
        reply = {"new_driver": "уверенность в стиле", "new_barrier": "непонятно, подойдёт ли стиль",
                 "shift": "механизм", "same_as": None, "changed": ["аудитория"], "kept": ["механизм", "момент"],
                 "confidence": "medium",
                 "explanation": "Теперь клиент видит работу до заказа. Например, можно сделать образец обложки."}
        ai = llm.AI(llm.FakeClient(lambda s, u: json.dumps(reply, ensure_ascii=False)))
        r = ai_roles.shift_check_v2(ai, [{"text": "Скидка", "xray": None}], "Образец обложки", "задача", "цель")
        self.assertEqual((r["shift"], r["changed"], r["kept"]), ("механизм", ["механизм", "аудитория"], ["момент"]))
        self.assertEqual((r["new_driver"], r["confidence"]), ("уверенность в стиле", "medium"))
        self.assertEqual(r["explanation"], "Теперь клиент видит работу до заказа.")

    def test_rejects_answer_without_restored_mechanism(self):
        reply = {"shift": "форма", "same_as": 1, "changed": [], "kept": [], "confidence": "high", "explanation": "x"}
        ai = llm.AI(llm.FakeClient(lambda s, u: json.dumps(reply, ensure_ascii=False)))
        self.assertIsNone(ai_roles.shift_check_v2(ai, [{"text": "Скидка", "xray": None}], "Скидка 5%"))


class SurgeryTest(unittest.TestCase):
    X_OLD = {"actor": "клиент", "action": "покупает", "driver": "скидка", "driver_user": "экономия",
             "barrier": "цена", "value": "выгода", "condition": ""}
    X_NEW = {"actor": "редактор", "action": "смотрит образец", "driver": "уверенность", "barrier": "риск",
             "value": "", "condition": "книга в плане"}

    def test_rules_never_claim_what_stayed(self):
        sg = training.surgery({"shift": "форма", "changed": [], "source": "rules"}, self.X_OLD, self.X_NEW)
        self.assertIsNone(sg["dims"])

    def test_ai_dims_only_named_ones(self):
        sg = training.surgery({"shift": "механизм", "changed": ["аудитория"], "kept": ["ресурсы"], "source": "ai"},
                              None, None)
        self.assertEqual([(d["name"], d["changed"], d["kept"]) for d in sg["dims"]],
                         [("механизм", True, False), ("ресурсы", False, True), ("аудитория", True, False)])
        self.assertIsNone(sg["parts"])  # без разбора обеих идей таблицы нет

    def test_parts_use_users_own_correction(self):
        sg = training.surgery({"shift": "механизм", "source": "ai"}, self.X_OLD, self.X_NEW)
        parts = {p["label"]: (p["before"], p["after"]) for p in sg["parts"]}
        self.assertEqual(parts["Что побуждает"], ("экономия", "уверенность"))  # поправка человека из зеркала
        self.assertEqual(parts["Что получает"], ("выгода", "—"))
        self.assertNotIn("При каком условии", [p for p in parts if not any(parts[p])])


class OriginHelpersTest(unittest.TestCase):
    def test_words_from_hints(self):
        words = training.words_from_hints("Позвать редактора на ярмарку и показать ярмарочный стенд",
                                          ["Где редакторы бывают вне офиса — например, на ярмарке?"],
                                          ["Иллюстратор ищет редакторов"])
        self.assertEqual([(w["word"], w["also_own"]) for w in words], [("редактора", True), ("ярмарку", False)])
        self.assertEqual(training.words_from_hints("на и в", ["на и в"], []), [])

    def test_elapsed_label(self):
        cases = {0: "0 мин", 59: "59 мин", 60: "1 ч", 61: "1 ч 1 мин", 1500: "через 1 дн."}
        for minutes, label in cases.items():
            self.assertEqual(training.elapsed_label(timedelta(minutes=minutes)), label)


class OriginAndFocusFlowTest(TrainingCase):
    ai_client = llm.FakeClient(kept_ai)

    def test_passport_in_journal_and_bank(self):
        self.register()
        self.fill_card()
        sid = self.run_session(final="Сделать обложку-образец к книге из плана издательства", hints=1, plan=True)
        journal = self.page(f"/s/{sid}/journal")
        for text in ("Как появилась идея", "Первые идеи — 3", "Подсказка «Вопрос»", "Версия 1",
                     "Версии идеи написали вы.", "ИИ разбирал идеи и проверял сдвиг", "старт"):
            self.assertIn(text, journal)
        self.assertNotIn("Как менялась идея", journal)  # список версий заменён паспортом
        final = self.q("SELECT id FROM ideas WHERE stage = 'final'")[0]["id"]
        card = self.page(f"/bank/{final}")
        self.assertIn("Как появилась идея", card)
        # хирургия в карточке сдвига: изменилось и осталось — то, что назвал детектор
        self.assertIn("Изменилось", card)
        self.assertIn("Осталось", card)
        self.assertIn("Разбор по частям", card)

    def test_eval_explains_autonomy_with_words(self):
        sid = self.to_idea()
        self.execute("INSERT INTO hints (session_id, kind, lib_id, text, source, created_at) "
                     "VALUES (?, 'question', 'H-test', 'Где бывают редакторы — например, на ярмарке?', 'template', "
                     "'2026-01-01T00:00:00+00:00')", (sid,))
        self.submit_final(sid, "Стенд на книжной ярмарке")
        page = self.page(f"/s/{sid}/eval")
        self.assertIn("Из подсказок", page)
        self.assertIn("ярмарке", page)

    def test_focus_mode_markup_and_logging(self):
        sid = self.to_idea()
        page = self.page(f"/s/{sid}/idea")
        for part in ("data-focus-toggle", 'data-focus-bar hidden', 'name="focus"', "data-focus-hide",
                     'value="draft" data-busy=""', 'value="ladder_start" data-busy=""', 'value="give_answer"'):
            self.assertIn(part, page)
        self.idea(sid, "submit", "Скидка 15% на второй заказ", self_check="no", focus="1")
        props = json.loads(self.q("SELECT props_json FROM events WHERE name = 'version_created'")[0]["props_json"])
        self.assertTrue(props["focus"])
        pending = self.page(f"/s/{sid}/idea")
        self.assertNotIn("data-focus-toggle", pending)  # пока решается судьба версии — режим не нужен
        self.assertIn('value="improve" class="primary" data-busy=""', pending)


if __name__ == "__main__":
    unittest.main()
