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
    """Ответы по контрактам A5 для всех ролей."""
    if "Оцени формулировку" in system:
        return '{"problem": "none", "in_domain": true}'
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
        self.post(f"/s/{sid}/mirror", {"action": "continue"})
        # С4–С5 диагноз и инструмент
        self.assertIn("Оператор преобразования", self.page(f"/s/{sid}/tool"))
        self.post(f"/s/{sid}/tool", {"action": "go"})
        # С6 своя идея с одной подсказкой
        self.post(f"/s/{sid}/idea", {"hint": "question", "idea": "черновик"})
        self.idea(sid, "submit", "Сделать обложку-образец к книге из плана издательства и отправить редактору")
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
        self.post(f"/s/{sid}/route", {"helped": "tool"})
        self.assertIn("Самый дешёвый способ проверить", self.page(f"/s/{sid}/check"))
        self.post(f"/s/{sid}/check", {"action": "save", "plan": "Отправить образец двум редакторам",
                                      "when": "сегодня", "ok": "ответил один", "fail": "никто"})
        self.assertEqual(self.state(sid), "done")
        self.assertIn("Через 2 дня мы спросим", self.page(f"/s/{sid}"))
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
                     "session_completed", "outcome_answered"):
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
