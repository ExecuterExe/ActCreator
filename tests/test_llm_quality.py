"""Изменения по итогам диагностики качества LLM (docs/llm-quality): облако v2, рентген v2, рубрика v2,
чат-бот с полным контекстом, предвыборка материала задачи. Настоящий API не вызывается — только FakeClient."""

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
from test_llm import FakeResponse  # noqa: E402

LIB = library.Library()


class CloudLabelsTest(unittest.TestCase):
    def test_rare_needs_seed_and_samples(self):
        labels, src = engine.cloud_labels(LIB, [{"F03", "F06"}, {"F03"}, {"F13"}])
        self.assertEqual(src, "seed")
        self.assertEqual(labels["F01"], "common")      # «часто» по LIB-01
        self.assertEqual(labels["F03"], "common")      # в 2 из 3 ответов
        self.assertEqual(labels["F06"], "uncertain")   # «средне», 1 из 3 — не «редкое»
        self.assertEqual(labels["F13"], "uncertain")   # «редко», но чат-бот назвал
        self.assertEqual(labels["F15"], "rare")        # «редко» и ни в одном ответе

    def test_no_samples_claims_no_rarity(self):
        labels, _ = engine.cloud_labels(LIB, [])
        self.assertEqual({v for f, v in labels.items() if LIB.families[f]["seed"] != "часто"}, {"uncertain"})

    def test_same_samples_same_labels_any_order(self):
        a = engine.cloud_labels(LIB, [{"F03"}, {"F06", "F03"}, {"F11"}])[0]
        b = engine.cloud_labels(LIB, [{"F11"}, {"F03"}, {"F03", "F06"}])[0]
        self.assertEqual(a, b)

    def test_data_mode_unchanged(self):
        labels, src = engine.cloud_labels(LIB, [], data_shares={"F12": 0.5, "F01": 0.1}, n_sessions=30)
        self.assertEqual(src, "data")
        self.assertEqual((labels["F12"], labels["F01"]), ("common", "rare"))

    def test_temp_taxonomy(self):
        labels = engine.cloud_labels_temp(["T01", "T02", "T03"], [{"T01"}, {"T01", "T02"}, set()])
        self.assertEqual(labels, {"T01": "common", "T02": "uncertain", "T03": "rare"})
        self.assertEqual(engine.cloud_labels_temp(["T01"], [set()]), {"T01": "uncertain"})  # один ответ — не вывод

    def test_novelty_needs_explicit_rarity(self):
        self.assertEqual(engine.novelty_level("механизм", True), "высокая")
        self.assertEqual(engine.novelty_level("механизм", None), "средняя")


class FamilyFrequencyTest(unittest.TestCase):
    def test_old_and_new_mirrors(self):
        old = {"common": ["F01"]}
        self.assertEqual(training.family_frequency("F06", old), "rare")  # до v2: всё вне облака — редкое
        new = {"common": ["F01"], "rare": ["F15"]}
        self.assertEqual(training.family_frequency("F06", new), "uncertain")
        self.assertEqual(training.family_frequency("F15", new), "rare")
        self.assertEqual(training.family_frequency("F01", new), "common")
        self.assertIsNone(training.family_frequency(None, new))


class XrayV2Test(unittest.TestCase):
    def answer(self, **extra):
        x = {"actor": "редактор", "action": "смотрит образец", "driver": "видит работу к своей книге",
             "barrier": "непонятно, подойдёт ли стиль", "value": "уверенность", "condition": "книга в плане",
             "family_id": "F06.4", "alt_family_id": "F05.1", "confidence": "high"}
        x.update(extra)
        return json.dumps(x, ensure_ascii=False)

    def test_mechanism_definitions_and_goal_in_prompt(self):
        client = llm.FakeClient(lambda s, u: self.answer())
        r = ai_roles.xray_v2(llm.AI(client), LIB, "Обложка-образец редактору", "Иллюстратор ищет заказы", "3 заказа")
        system, user = client.calls[0]
        self.assertIn("Разбери идею", system)  # тот же маркер роли для журналов и тестов
        self.assertIn(LIB.subfamilies["F06.4"]["mechanism"], user)  # уточнение механизма подсемейства
        self.assertIn("легко спутать", user)
        self.assertIn("<<<3 заказа>>>", user)
        self.assertEqual((r["family"], r["subfamily"], r["alt"], r["abstained"]), ("F06", "F06.4", "F05", False))
        self.assertEqual(r["xray"]["driver"], "видит работу к своей книге")

    def test_honest_abstain(self):
        ai = llm.AI(llm.FakeClient(lambda s, u: self.answer(family_id=None, alt_family_id=None, confidence="low")))
        r = ai_roles.xray_v2(ai, LIB, "Сделать иллюстрации лучше", "задача")
        self.assertEqual((r["family"], r["abstained"], r["confidence"]), (None, True, "low"))

    def test_code_outside_taxonomy_rejected(self):
        ai = llm.AI(llm.FakeClient(lambda s, u: self.answer(family_id="F99")))
        self.assertIsNone(ai_roles.xray_v2(ai, LIB, "идея", "задача"))

    def test_same_alt_dropped(self):
        ai = llm.AI(llm.FakeClient(lambda s, u: self.answer(alt_family_id="F06.2")))
        self.assertIsNone(ai_roles.xray_v2(ai, LIB, "идея", "задача")["alt"])


class RubricV2Test(unittest.TestCase):
    def test_evidence_anchors_and_validation(self):
        reply = json.dumps({"criteria": [
            {"id": "RB1", "evidence": "стать заметнее", "level": 1, "reason": "не про заказы"},
            {"id": "RB2", "evidence": "0 ₽", "level": 2, "reason": "оценить нельзя"},
            {"id": "RB3", "evidence": "нет действия", "level": 1, "reason": "размыто"}]}, ensure_ascii=False)
        client = llm.FakeClient(lambda s, u: reply)
        r = ai_roles.rubric_v2(llm.AI(client), LIB, "Стать заметнее", "задача", "цель", "Бюджет: 0 ₽")
        system = client.calls[0][0]
        self.assertIn("Оцени идею по рубрикам", system)
        self.assertIn(LIB.rubrics["RB2"]["levels"][1]["anchors"][0], system)  # якорь LIB-15
        self.assertIn("не «нехватка данных»", system)
        self.assertEqual((r["RB1"]["level"], r["RB3"]["evidence"]), (1, "нет действия"))
        self.assertEqual(engine.feasibility_level(r), "низкая")

    def test_card_rules_cap_rb2(self):
        reply = json.dumps({"criteria": [{"id": k, "level": 3, "evidence": "x", "reason": "x"}
                                         for k in ("RB1", "RB2", "RB3", "RB4")],
                            "needs": {"money_rub": 0, "hours_per_week": 6, "helpers": False,
                                      "skills": ["снимать и монтировать видео"], "uses": []}}, ensure_ascii=False)
        ai = llm.AI(llm.FakeClient(lambda s, u: reply))
        card = {"R01": "0 ₽", "R02": "2–5 ч", "R05": ["дизайн и визуал"]}
        r = ai_roles.rubric_v2(ai, LIB, "Видео о процессе", "задача", "цель", "карточка", card)
        self.assertEqual(r["RB2"]["level"], 2)  # один недостающий навык и чуть больше часов — «на пределе»
        self.assertIn("снимать и монтировать видео", r["RB2"]["reason"])
        self.assertTrue(r["RB2"]["rule"])
        self.assertEqual(r["RB1"]["level"], 3)  # остальные критерии правило не трогает
        no_card = ai_roles.rubric_v2(ai, LIB, "Видео о процессе", "задача", "цель", "карточка")
        self.assertEqual(no_card["RB2"]["level"], 3)

    def test_resource_fit_rules(self):
        self.assertEqual(engine.resource_fit({"money_rub": 20000}, {"R01": "до 5 тыс. ₽"})[0], 1)
        self.assertEqual(engine.resource_fit({"money_rub": 4500}, {"R01": "до 5 тыс. ₽"})[0], 2)
        self.assertEqual(engine.resource_fit({"hours_per_week": 12}, {"R02": "2–5 ч"})[0], 1)
        self.assertEqual(engine.resource_fit({"helpers": True}, {"R04": "только я"})[0], 1)
        self.assertEqual(engine.resource_fit({"uses": ["давать скидки"]}, {"R06": ["давать скидки"]})[0], 1)
        self.assertEqual(engine.resource_fit({"uses": ["платная реклама"]}, {"R12": ["без платной рекламы"]})[0], 1)
        self.assertEqual(engine.resource_fit({"uses": ["давать скидки"]}, {"R12": ["без платной рекламы"]})[0], 3)
        self.assertEqual(engine.resource_fit({"skills": ["программировать", "писать тексты"]},
                                             {"R05": ["дизайн и визуал"]})[0], 1)
        self.assertEqual(engine.resource_fit({"skills": ["программировать"]}, {})[0], 3)  # навыки не указаны — не судим
        self.assertEqual(engine.resource_fit({"money_rub": 0, "hours_per_week": 2}, {"R01": "0 ₽", "R02": "2–5 ч"}),
                         (3, []))

    def test_too_few_criteria_is_failure(self):
        ai = llm.AI(llm.FakeClient(lambda s, u: '{"criteria": [{"id": "RB1", "level": 3}]}'))
        self.assertIsNone(ai_roles.rubric_v2(ai, LIB, "идея", "задача", "цель", "карточка"))


class BaselineV2Test(unittest.TestCase):
    def test_gets_goal_and_bans(self):
        client = llm.FakeClient(lambda s, u: '{"ideas": ["a", "b", "c", "d"]}')
        ai_roles.baseline_v2(llm.AI(client), "Пекарня у остановки", "продавать весь хлеб", "без скидок")
        user = client.calls[0][1]
        self.assertIn("Предложи 6 идей: Пекарня у остановки", user)
        self.assertIn("Цель: продавать весь хлеб", user)
        self.assertIn("Ограничения: без скидок", user)


class Clock:
    """Подменяет время в llm: каждый вызов monotonic() сдвигает часы на step секунд; sleep не ждёт."""

    def __init__(self, step):
        self.now, self.step = 0.0, step

    def monotonic(self):
        self.now += self.step
        return self.now

    def sleep(self, seconds):
        pass


class TimeBudgetTest(unittest.TestCase):
    def patched(self, step):
        clock = Clock(step)
        original = llm.time
        llm.time = clock
        self.addCleanup(setattr, llm, "time", original)

    def test_hung_provider_not_retried(self):
        calls = []

        def _open(request, timeout=None):
            calls.append(timeout)
            raise TimeoutError("timed out")
        self.patched(step=30)  # попытка «длится» 30 с — больше половины тайм-аута
        with self.assertRaises(llm.LLMError):
            llm.OpenRouterClient("k", opener=_open).complete("s", "u")
        self.assertEqual(calls, [llm.DEFAULT_TIMEOUT])  # одна попытка, не две

    def test_quick_network_blip_retried(self):
        calls = []

        def _open(request, timeout=None):
            calls.append(1)
            if len(calls) == 1:
                raise OSError("reset")
            return FakeResponse(json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode())
        self.patched(step=0.1)
        self.assertEqual(llm.OpenRouterClient("k", opener=_open).complete("s", "u"), "ok")
        self.assertEqual(len(calls), 2)

    def test_slow_failure_goes_to_fallback_without_retry(self):
        client = llm.FakeClient(lambda s, u: llm.LLMError("сеть"))
        self.patched(step=llm.RETRY_BUDGET + 1)  # первая попытка заняла больше бюджета
        self.assertIsNone(llm.AI(client).ask_json("r", "s", "u", validate=lambda d: d))
        self.assertEqual(len(client.calls), 1)

    def test_fast_failure_still_retried(self):
        answers = iter(["не json", '{"ok": true}'])
        self.patched(step=0.5)
        self.assertEqual(llm.AI(llm.FakeClient(lambda s, u: next(answers))).ask_json("r", "s", "u", lambda d: d),
                         {"ok": True})


class ProviderMetaTest(unittest.TestCase):
    def test_provider_recorded(self):
        def _open(request, timeout=None):
            return FakeResponse(json.dumps({"provider": "DeepInfra", "model": "deepseek/deepseek-chat",
                                            "choices": [{"message": {"content": "ok"}}]}).encode())
        client = llm.OpenRouterClient("k", opener=_open)
        client.complete("s", "u")
        self.assertEqual(client.last_meta, {"attempts": 1, "provider": "DeepInfra", "model": "deepseek/deepseek-chat"})


def material_ai(system, user):
    if "Оцени формулировку" in system:
        return '{"problem": "none", "in_domain": true}'
    if "Предложи 6 идей" in user:
        return json.dumps({"ideas": ["Вести соцсети", "Скидка новым клиентам", "Попросить отзывы",
                                     "Профиль на бирже", "Партнёрство с типографией", "Нетворкинг"]},
                          ensure_ascii=False)
    if "отнести идею" in system:
        return '{"family_id": "F03.1", "confidence": "medium", "explanation": "партнёр"}'
    if "через три разные модели" in system:
        item = {"barrier": "Редактор не знает автора.", "question": "Что редактор видит первым?"}
        return json.dumps({"system": item, "behavior": item, "value": item}, ensure_ascii=False)
    if "Разбери идею" in system:
        return json.dumps({"actor": "клиент", "action": "заказывает", "driver": "выгода", "barrier": "цена",
                           "value": "экономия", "condition": "если цена важна", "family_id": "F07.1",
                           "confidence": "high"}, ensure_ascii=False)
    return "{}"


class PrefetchTest(TrainingCase):
    ai_client = llm.FakeClient(material_ai)

    def prepare(self):
        self.register()
        self.fill_card()
        sid = self.start()
        self.set_task(sid)
        return sid

    def test_material_ready_before_first_ideas(self):
        sid = self.prepare()
        text, future = training._PREFETCHED[sid]
        material, _ = future.result(timeout=10)  # ответы чат-бота и линзы пришли до первых идей
        self.assertIn("Иллюстратор", text)
        self.assertEqual(len(material["samples"]), engine.CLOUD_SAMPLES)
        self.assertEqual(len(material["classes"]), 6)
        asked = len([1 for s, u in self.ai_client.calls if "Предложи 6 идей" in u])
        self.submit_first(sid, ["Скидка 10% на первый заказ", "Промокод для новых"])
        self.assertEqual(asked, len([1 for s, u in self.ai_client.calls if "Предложи 6 идей" in u]))
        self.assertNotIn(sid, training._PREFETCHED)
        self.page(f"/s/{sid}/mirror")
        m = json.loads(self.q("SELECT mirror_json FROM sessions")[0]["mirror_json"])
        self.assertEqual((m["cloud"], m["n_samples"], m["baseline_source"], m["lens_source"]),
                         ("v2", engine.CLOUD_SAMPLES, "ai", "ai"))
        self.assertIn("F03", m["common"])  # в каждом из 3 ответов есть «Партнёрство с типографией»
        self.assertIn("F15", m["rare"])
        page = self.page(f"/s/{sid}/mirror")
        self.assertIn("независимых ответа обычного чат-бота", page)

    def test_other_task_text_is_not_reused(self):
        sid = self.prepare()
        self.assertIsNone(training.take_prefetch({"id": sid}, {"text": "другая задача"}))
        self.assertIsNone(training.take_prefetch({"id": sid}, {"text": "другая задача"}))  # и забрана из памяти

    def test_without_prefetch_old_path_still_works(self):
        training.PREFETCH = False
        training._PREFETCHED.clear()  # предвыборки других тестов (те же id сессий в новых базах)
        try:
            sid = self.prepare()
            self.assertNotIn(sid, training._PREFETCHED)
            before = len([1 for s, u in self.ai_client.calls if "Предложи 6 идей" in u or "три разные" in s])
            self.submit_first(sid, ["Скидка 10% на первый заказ", "Промокод для новых"])
            self.page(f"/s/{sid}/mirror")
            m = json.loads(self.q("SELECT mirror_json FROM sessions")[0]["mirror_json"])
            self.assertEqual((m["baseline_source"], m["n_samples"]), ("ai", 1))
            # чат-бот и линзы — по одному разу: до 17.10 вторая запись флагов теряла их, и зеркало спрашивало снова
            after = len([1 for s, u in self.ai_client.calls if "Предложи 6 идей" in u or "три разные" in s])
            self.assertEqual(after - before, 2)
        finally:
            training.PREFETCH = True


class ChatbotOverlapRoleTest(unittest.TestCase):
    def test_index_checked(self):
        answers = ["Скидка новым", "Бесплатный пробный проект для издательства"]
        client = llm.FakeClient(lambda s, u: '{"same": 2, "explanation": "оценить работу до оплаты"}')
        r = ai_roles.chatbot_overlap(llm.AI(client), "Обложка-образец редактору", answers, "задача", "цель")
        self.assertEqual(r["same"], 2)
        self.assertIn("<<<Бесплатный пробный проект", client.calls[0][1])  # ответы чат-бота — как данные
        bad = llm.AI(llm.FakeClient(lambda s, u: '{"same": 7, "explanation": "x"}'))
        self.assertIsNone(ai_roles.chatbot_overlap(bad, "идея", answers)["same"])


def overlap_ai(system, user):
    if "ответы обычного чат-бота на ту же задачу" in system:
        return '{"same": 5, "explanation": "Тот же механизм: заказы через партнёра, у которого уже есть клиенты."}'
    if "Разбери идею" in system and "Новогодняя" in user.split("Идея:")[-1]:
        return json.dumps({"actor": "клиент", "action": "покупает", "driver": "позиция против нормы",
                           "barrier": "недоверие", "value": "свой", "condition": "если ценности совпадают",
                           "family_id": "F15.4", "confidence": "high"}, ensure_ascii=False)
    if "аналитик механизмов" in system:
        return json.dumps({"new_driver": "ценности", "new_barrier": "недоверие", "shift": "механизм",
                           "changed": ["механизм"], "kept": [], "same_as": None, "confidence": "high",
                           "explanation": "Клиента побуждают ценности, а не выгода."}, ensure_ascii=False)
    if "Оцени идею по рубрикам" in system:
        return json.dumps({"criteria": [{"id": k, "level": 3, "evidence": "x", "reason": "x"}
                                        for k in ("RB1", "RB2", "RB3", "RB4")],
                           "needs": {"money_rub": 0, "hours_per_week": 2}}, ensure_ascii=False)
    return material_ai(system, user)


class ChatbotOverlapFlowTest(TrainingCase):
    ai_client = llm.FakeClient(overlap_ai)

    def test_overlap_is_a_hint_not_a_verdict(self):
        sid = self.to_idea()
        self.submit_final(sid, "Новогодняя позиция против нормы")
        self.page(f"/s/{sid}/eval")
        ev = self.session_json(sid, "eval_json")
        self.assertEqual(ev["freq"], "rare")
        self.assertEqual(ev["chatbot_same"]["text"], "Партнёрство с типографией")  # 5-й ответ зеркала
        # с 18.10 сверка — только подсказка «сравните сами»: ~1/3 её совпадений ложные, новизну она не меняет
        self.assertEqual(ev["novelty"], "высокая")
        page = self.page(f"/s/{sid}/eval")
        self.assertIn("среди ответов обычного чат-бота на эту задачу: «Партнёрство с типографией»", page)
        self.assertIn("ошибается примерно в трети случаев", page)
        self.post(f"/s/{sid}/eval", {"action": "overlap_disagree"})
        ev = self.session_json(sid, "eval_json")
        self.assertEqual((ev["overlap_disputed"], ev["novelty"]), (True, "высокая"))
        self.assertEqual(len(self.q("SELECT id FROM events WHERE name = 'chatbot_overlap_disputed'")), 1)
        self.assertNotIn("среди ответов обычного чат-бота", self.page(f"/s/{sid}/eval"))

    def test_no_overlap_check_against_library_answers(self):
        training.CHATBOT_OVERLAP = True
        sid = self.to_idea()
        self.execute("UPDATE sessions SET mirror_json = json_set(mirror_json, '$.baseline_source', 'library')")
        self.submit_final(sid, "Новогодняя позиция против нормы")
        self.page(f"/s/{sid}/eval")
        self.assertIsNone(self.session_json(sid, "eval_json")["chatbot_same"])  # заготовки LIB-14 — не чат-бот


if __name__ == "__main__":
    unittest.main()
