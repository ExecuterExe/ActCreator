"""Сквозная проверка всей цепочки: регистрация → карточка → сессия С1–С10 → исход → банк → выгрузка.

Три режима ИИ: нет ключа (NullClient), ИИ отвечает по контракту (FakeClient), ИИ падает или отвечает мусором.
Настоящий OpenRouter в тестах не вызывается никогда.
"""

import csv
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

import export_csv
import llm
from helpers import TrainingCase


def contract_ai(system, user):
    """Ответы по контрактам A5 для всех ролей (включая рентген, линзы и детектор сдвига v0.5)."""
    if "Оцени формулировку" in system:
        return '{"problem": "none", "in_domain": true}'
    if "Разбери идею" in system:
        idea = user.split("Идея:")[-1]
        if "Скидк" in idea or "Промокод" in idea:
            x = {"actor": "клиент", "action": "делает первый заказ", "driver": "денежная выгода",
                 "barrier": "цена", "value": "экономия", "condition": "если цена решает", "family_id": "F07.1"}
        else:
            x = {"actor": "редактор", "action": "оценивает готовый образец", "driver": "снижение неопределённости",
                 "barrier": "непонятно, подходит ли стиль", "value": "уверенность в выборе",
                 "condition": "если книга действительно в плане", "family_id": "F06.2"}
        return json.dumps(dict(x, confidence="high"), ensure_ascii=False)
    if "Сравни новую идею" in system or "аналитик механизмов" in system:
        if "обложк" in user.split("Новая идея:")[-1]:
            return json.dumps({"new_driver": "возможность оценить работу до заказа",
                               "new_barrier": "непонятно, подойдёт ли стиль",
                               "shift": "механизм", "changed": ["механизм", "источник ценности"], "same_as": None,
                               "confidence": "high", "levers": ["trial"], "goal_kept": True,
                               "opens": "Редактор оценивает работу к своей книге, а не портфолио вообще.",
                               "risk": "Нужно угадать стиль книги без брифа.",
                               "explanation": "Раньше клиент получал выгоду, теперь — возможность оценить работу "
                                              "до заказа."}, ensure_ascii=False)
        return ('{"new_driver": "денежная выгода", "new_barrier": "цена", "shift": "форма", "changed": [], '
                '"same_as": 1, "confidence": "high", "levers": ["price"], "goal_kept": true, '
                '"explanation": "Принцип тот же — выгода."}')
    if "через три разные модели" in system:
        return json.dumps({
            "system": {"barrier": "Портфолио не показывает, подойдёт ли стиль к конкретной книге.",
                       "question": "Где в пути редактора от письма до решения самое узкое место?"},
            "behavior": {"barrier": "Редактору проще проигнорировать незнакомца, чем тратить время на оценку.",
                         "question": "Что снизило бы для редактора цену первого шага?"},
            "value": {"barrier": "Редактор пока не видит выгоды, ради которой стоит тратить время.",
                      "question": "Что редактор получает уже в первые минуты знакомства?"}}, ensure_ascii=False)
    if "Предложи 6 идей" in user:
        return json.dumps({"ideas": ["Вести соцсети", "Скидка новым клиентам", "Попросить отзывы",
                                     "Профиль на бирже", "Партнёрство с типографией", "Нетворкинг"]}, ensure_ascii=False)
    if "отнести идею" in system:
        return '{"family_id": "F12.5", "confidence": "medium", "explanation": "клиент делает часть работы"}'
    if "Перепиши шаблон" in system:
        return '{"task_text": "Пусть редактор сам сделает то, что сейчас делаете вы. Что это должно быть?"}'
    if "причинное допущение" in system:
        return '{"assumption": "Издательства выбирают по цене", "task": "Как добиться цели, если цена не важна?"}'
    if "Дай ОДНУ подсказку" in system:
        return '{"hint": "Кто из окружения редактора первым узнаёт, что ему нужен иллюстратор?"}'
    if "Оцени идею по рубрикам" in system:
        return ('{"criteria": [{"id": "RB1", "level": 3, "reason": "прямо к заказам"},'
                '{"id": "RB2", "level": 3, "reason": "бесплатно"}, {"id": "RB3", "level": 2, "reason": "срок размыт"}]}')
    if "Ты критик пре-мортема" in system:
        return json.dumps({"objections": [
            {"category": "К3", "card_field": None, "mechanism_link": "доверие к незнакомому автору",
             "condition": "Не сработает, если редакторы не открывают письма от незнакомых",
             "check": "Отправить 5 писем и посчитать ответы", "severity": 3},
            {"category": "К5", "card_field": "R02", "mechanism_link": None,
             "condition": "Не сработает, если на образцы уйдёт больше 5 часов", "check": "Сделать один образец",
             "severity": 2}]}, ensure_ascii=False)
    if "Ты судья" in system:
        return '{"verdict": "ослаблено", "basis": "аргумент", "reason": "Довод есть, но не проверен."}'
    if "Найди общее в первых идеях" in system:  # Р16 v2, что общего у первых идей (19.10)
        # все три идеи — ценовое поощрение, но модель не уверена («medium»): колея возможна, не установлена —
        # обязательной «смены механизма» нет, задание выбирают правила Д0–Д5; полная колея — test_product_flow
        return json.dumps({"ideas": [
            {"result": "заказ", "tool": tool, "because": "издательство закажет, потому что дешевле", "lever": "price",
             "clear": True, "missing": ""} for tool in ("скидка 10%", "скидка 20%", "промокод", "скидка")],
            "limit": "", "confidence": "medium"}, ensure_ascii=False)
    if "Составь план самой дешёвой проверки" in system:  # Р5 v2, план проверки за 48 часов (18.10)
        return json.dumps({"step": "Отправить 5 писем с эскизом редакторам", "success": "Ответил хотя бы один",
                           "fail": "Ни одного ответа за неделю"}, ensure_ascii=False)
    if "ответы обычного чат-бота на ту же задачу" in system:  # Р15, сверка с чат-ботом (17.10)
        return '{"same": null, "explanation": "У чат-бота — профиль и скидки, а здесь образец работы под книгу."}'
    return "{}"


def failing_ai(system, user):
    return llm.LLMError("сеть недоступна")


def garbage_ai(system, user):
    """Валидный JSON неожиданной формы на любой запрос — сервис не должен падать."""
    return ('{"objections": ["x", 1], "ideas": "строка", "families": [1, 2], "criteria": ["a"], "hint": 5, '
            '"task_text": ["a"], "verdict": 1, "family_id": 7, "problem": 3, "assumption": [], "in_domain": "да"}')


class FullChainMixin:
    def full_chain(self):
        # регистрация и кабинет
        self.register()
        self.assertIn("Кабинет", self.page("/me"))
        # без карточки ресурсов тренировку не начать
        r = self.c.get("/train")
        self.assertIn("/train/card", r.headers["Location"])
        self.fill_card()
        sid = self.start()
        # С1 задача
        self.set_task(sid)
        self.assertEqual(self.state(sid), "first")
        # С2 первые идеи → С3 зеркало
        self.submit_first(sid, ["Скидка 10% на первый заказ", "Скидка 20% постоянным", "Промокод для новых"], 1)
        mirror = self.page(f"/s/{sid}/mirror")
        self.assertIn("Скидки и акции", mirror)
        self.assertIn("Посмотрим на задачу с трёх сторон", mirror)
        self.post(f"/s/{sid}/mirror", {"action": "continue", "lens": "behavior"})
        # С4–С5 диагноз и инструмент
        self.assertIn("Оператор преобразования", self.page(f"/s/{sid}/tool"))
        self.post(f"/s/{sid}/tool", {"action": "go"})
        # С6 своя идея с одной подсказкой
        self.post(f"/s/{sid}/idea", {"hint": "question", "idea": "черновик"})
        self.submit_final(sid, "Сделать обложку-образец к книге из плана издательства и отправить редактору")
        # С7 оценка
        self.assertIn("Самостоятельность", self.page(f"/s/{sid}/eval"))
        self.post(f"/s/{sid}/eval", {"action": "take"})
        self.post(f"/s/{sid}/eval", {"action": "next"})
        # С8 пре-мортем
        self.post(f"/s/{sid}/premortem", {"action": "reasons", "reason1": "Редактор не откроет письмо"})
        critic = self.q("SELECT id FROM objections WHERE author = 'critic'")
        self.assertTrue(critic)
        data = {"action": "defend"}
        for o in critic:
            data.update({f"about_{o['id']}": "1", f"type_{o['id']}": "change",
                         f"text_{o['id']}": "Сначала напишу редактору короткое письмо и спрошу разрешения"})
        self.post(f"/s/{sid}/premortem", data)
        self.post(f"/s/{sid}/premortem", {"action": "finish"})
        # С9 маршрут, С10 проверка за 48 часов
        self.assertIn("Начали с", self.page(f"/s/{sid}/route"))
        self.post(f"/s/{sid}/route", {"helped": "tool", "wow": "Скорее нет"})
        self.assertIn("Самый дешёвый способ проверить", self.page(f"/s/{sid}/check"))
        self.post(f"/s/{sid}/check", {"action": "save", "plan": "Отправить образец двум редакторам",
                                      "when": "сегодня", "ok": "ответил один", "fail": "никто"})
        self.assertEqual(self.state(sid), "done")
        self.assertIn("Через 2 дня мы спросим", self.page(f"/s/{sid}"))
        # журнал идеи собран из данных этой сессии
        journal = self.page(f"/s/{sid}/journal")
        for text in ("Лабораторный журнал идеи", "Сделать обложку-образец к книге", "Скидка 10% на первый заказ",
                     "Первоначальный подход", "Следующий тест", "Отправить образец двум редакторам"):
            self.assertIn(text, journal)
        # М1 исход через 3 дня → М2 банк
        past = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat(timespec="seconds")
        self.execute("UPDATE sessions SET completed_at = ? WHERE id = ?", (past, sid))
        self.assertIn("Удалось сделать проверку", self.page("/me"))
        self.post(f"/outcome/{sid}/2", {"status": "Сделал(а): возражение снято", "next": "/me"}, csrf_from="/me")
        self.assertIn("Проверено (1)", self.page("/bank"))
        self.assertIn("За последние 30 дней тренировок: 1", self.page("/summary"))
        # выгрузка
        with tempfile.TemporaryDirectory() as tmp:
            with closing(sqlite3.connect(self.db_path)) as conn, redirect_stdout(io.StringIO()) as out:
                conn.row_factory = sqlite3.Row
                export_csv.export_training(conn, Path(tmp))
            self.assertIn("Тренировок начато: 1, завершено: 1", out.getvalue())
            files = {p.name.split("_")[0]: p for p in Path(tmp).iterdir()}
            with open(files["sessions"], encoding="utf-8-sig") as f:
                rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["state"], row["diagnosis"], row["tool"], row["hints"], row["has_plan"],
                          row["outcome_d2"]), ("done", "Д1", "operator", "1", "1", "Сделал(а): возражение снято"))
        self.assertNotIn("Сделать обложку-образец", json.dumps(rows, ensure_ascii=False))  # без текстов идей
        # ни одной ошибки сервера по пути: все шаги отданы
        names = [r["name"] for r in self.q("SELECT name FROM events")]
        for name in ("session_started", "diagnosis_made", "tool_shown", "hint_used", "idea_submitted",
                     "evaluated", "idea_taken", "premortem_done", "route_done", "check_planned",
                     "session_completed", "outcome_answered", "journal_opened"):
            self.assertIn(name, names)
        return sid, row


class ChainNoAITest(FullChainMixin, TrainingCase):
    def test_full_chain(self):
        sid, row = self.full_chain()
        self.assertEqual(row["rubric_source"], "rules")
        self.assertEqual(self.q("SELECT source FROM hints")[0]["source"], "template")
        self.assertEqual(self.q("SELECT COUNT(*) AS n FROM events WHERE name = 'ai_error'")[0]["n"], 0)


class ChainContractAITest(FullChainMixin, TrainingCase):
    ai_client = llm.FakeClient(contract_ai)

    def test_full_chain(self):
        self.ai_client.calls.clear()
        sid, row = self.full_chain()
        self.assertEqual(row["rubric_source"], "ai")
        self.assertEqual(self.q("SELECT source FROM hints")[0]["source"], "ai")
        self.assertEqual({o["verdict_source"] for o in self.q("SELECT verdict_source FROM objections "
                                                               "WHERE author = 'critic'")}, {"ai"})
        mirror = json.loads(self.q("SELECT mirror_json FROM sessions")[0]["mirror_json"])
        self.assertEqual(mirror["baseline_source"], "ai")
        self.assertEqual(json.loads(self.q("SELECT tool_json FROM sessions")[0]["tool_json"])["source"], "ai")
        # текст пользователя уходит в модель только как данные в <<< >>>
        task_calls = [u for s, u in self.ai_client.calls if "Иллюстратор-фрилансер" in u]
        self.assertTrue(task_calls)
        self.assertTrue(all("<<<Иллюстратор-фрилансер" in u or "Предложи 6 идей" in u for u in task_calls))
        self.assertEqual(self.q("SELECT COUNT(*) AS n FROM events WHERE name = 'ai_error'")[0]["n"], 0)
        # v0.5: рентген, линзы, детектор сдвига, «вау»-вопрос
        self.assertEqual({r["class_source"] for r in self.q("SELECT class_source FROM ideas WHERE stage = 'first'")},
                         {"xray"})
        self.assertEqual(mirror["lens_source"], "ai")
        flags = json.loads(self.q("SELECT flags_json FROM sessions")[0]["flags_json"])
        self.assertEqual(flags["barrier"]["lens"], "behavior")
        final = self.q("SELECT family, change_json FROM ideas WHERE stage = 'final'")[0]
        self.assertEqual(final["family"], "F06")
        self.assertEqual(json.loads(final["change_json"])["vs_first"]["shift"], "механизм")
        ev = json.loads(self.q("SELECT eval_json FROM sessions")[0]["eval_json"])
        self.assertEqual((ev["shift_kind"], ev["novelty"]), ("механизм", "высокая" if ev["rare"] else "средняя"))
        self.assertEqual(json.loads(self.q("SELECT route_json FROM sessions")[0]["route_json"])["wow"], "Скорее нет")


class ChainFailingAITest(FullChainMixin, TrainingCase):
    ai_client = llm.FakeClient(failing_ai)

    def test_full_chain_degrades(self):
        sid, row = self.full_chain()
        self.assertEqual(row["rubric_source"], "rules")
        self.assertGreater(self.q("SELECT COUNT(*) AS n FROM events WHERE name = 'ai_error'")[0]["n"], 3)


class ChainGarbageAITest(FullChainMixin, TrainingCase):
    ai_client = llm.FakeClient(garbage_ai)

    def test_full_chain_survives_garbage(self):
        sid, row = self.full_chain()
        self.assertEqual(row["rubric_source"], "rules")
        self.assertEqual(self.q("SELECT source FROM hints")[0]["source"], "template")


if __name__ == "__main__":
    unittest.main()
