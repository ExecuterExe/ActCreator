"""Данные исследования в Google Sheets — на подставной таблице в памяти (unittest не ходит в сеть)."""

import base64
import io
import json
import os
import re
import unittest
from unittest import mock

import research
import sheets
from helpers import TrainingCase
from test_a11y import a11y_problems


class FakeSheets:
    """Ведёт себя как SheetsClient: вкладки со шапкой, добавление строк, обновление по диапазону."""

    email = "robot@actkreator-test.iam.gserviceaccount.com"
    spreadsheet_id = "TEST-SHEET"

    def __init__(self):
        self.tabs, self.fail, self.writes = {}, False, 0

    def _check(self):
        if self.fail:
            raise sheets.SheetsError("нет связи с Google Sheets: тестовый сбой")

    def ensure_tab(self, tab, header):
        self._check()
        rows = self.tabs.setdefault(tab, [list(header)])
        rows[0] = list(header)

    def append(self, tab, row):
        self._check()
        self.writes += 1
        self.tabs[tab].append([sheets.cell(v) for v in row])
        n = len(self.tabs[tab])
        return f"{sheets.quote_range(tab, f'A{n}:Z{n}')}"

    def update(self, rng, row):
        self._check()
        self.writes += 1
        tab, n = re.match(r"'(.+)'!A(\d+)", rng).groups()
        self.tabs[tab][int(n) - 1] = [sheets.cell(v) for v in row]
        return rng

    def read(self, rng):
        tab, n = re.match(r"'(.+)'!A(\d+)", rng).groups()
        return [[str(v) for v in self.tabs[tab][int(n) - 1]]]

    def records(self, tab):
        rows = self.tabs.get(tab, [[]])
        return [dict(zip(rows[0], r)) for r in rows[1:]]


class SheetsCase(TrainingCase):
    def setUp(self):
        super().setUp()
        self.sheet = FakeSheets()
        self.app.config.update(SHEETS_CLIENT=self.sheet, RESEARCH_SYNC=True, ADMINS="user1")

    def uid(self):
        return self.q("SELECT value FROM meta WHERE key = 'db_uid'")[0]["value"]


class TrainingToSheetTest(SheetsCase):
    def test_session_is_one_row_updated_in_place(self):
        self.register()
        self.fill_card()
        sid = self.run_session(plan=True)
        rows = self.sheet.records(research.TAB_SESSIONS)
        self.assertEqual(len(rows), 1)  # десяток шагов — одна строка, обновлённая на месте
        row = rows[0]
        self.assertEqual((row["record_id"], row["state"], row["has_plan"]), (f"{self.uid()}:session:{sid}", "done", 1))
        self.assertEqual((row["participant"], row["session"]), (f"{self.uid()}-u1", f"{self.uid()}-s{sid}"))
        ideas = self.sheet.records(research.TAB_IDEAS)
        self.assertIn("Стенд на книжной ярмарке", [i["text"] for i in ideas if i["source"] == "участник"])
        self.assertTrue([i for i in ideas if i["source"] == "чат-бот"])
        self.assertEqual(self.q("SELECT COUNT(*) AS n FROM research_outbox "
                                "WHERE sent_hash IS NULL OR sent_hash != row_hash")[0]["n"], 0)

    def test_unchanged_data_is_not_resent(self):
        self.register()
        self.fill_card()
        sid = self.run_session()
        writes = self.sheet.writes
        with self.app.test_request_context():
            research.queue_session(sid)
            research.queue_session(sid)
        self.assertEqual(self.sheet.writes, writes)

    def test_outage_keeps_data_and_tells_the_truth(self):
        self.register()
        self.fill_card()
        sid = self.run_session()
        self.sheet.fail = True
        self.post(f"/s/{sid}/done", {"action": "exit_survey", "useful": "5", "chatgpt_replace": "Не заменит"})
        page = self.page(f"/s/{sid}")
        self.assertIn("В хранилище исследования они пока не записались", page)
        self.assertNotIn("Спасибо — оценка сохранена", page)
        self.assertEqual(self.q("SELECT COUNT(*) AS n FROM exit_surveys")[0]["n"], 1)  # на сервере ответ есть
        admin = self.page("/admin")
        self.assertIn("тестовый сбой", admin)
        self.sheet.fail = False
        self.post("/admin", {"action": "sheet_flush"})
        rows = self.sheet.records(research.TAB_SESSIONS)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["exit_useful"], rows[0]["h4_signal"]), (5, 1))

    def test_exit_survey_confirmed_by_sheet(self):
        self.register()
        self.fill_card()
        sid = self.run_session()
        self.post(f"/s/{sid}/done", {"action": "exit_survey", "useful": "3"})
        self.assertIn("Спасибо — оценка сохранена", self.page(f"/s/{sid}"))
        self.assertEqual(self.sheet.records(research.TAB_SESSIONS)[0]["exit_useful"], 3)


class EntryTestToSheetTest(SheetsCase):
    def test_each_step_updates_one_row(self):
        self.register()
        self.post("/test/survey/about", {"role": ["freelance"], "field": "design", "freq": "monthly",
                                         "goal": "solve_now"})
        rows = self.sheet.records(research.TAB_ATTEMPTS)
        self.assertEqual((len(rows), rows[0]["role"], rows[0]["step"]), (1, "freelance", "case"))
        self.post("/test/survey/case", {"last_when": "month", "last_task": "Найти заказчиков без бюджета",
                                        "last_tried": ["self"], "last_result": "ordinary"})
        rows = self.sheet.records(research.TAB_ATTEMPTS)
        self.assertEqual((len(rows), rows[0]["last_when"]), (1, "month"))
        self.assertTrue(rows[0]["participant"].startswith(self.uid() + "-u"))


class AdminSheetsTest(SheetsCase):
    def test_status_check_and_accessibility(self):
        self.register()
        page = self.page("/admin")
        for text in ("Google Sheets", FakeSheets.email, self.uid()):
            self.assertIn(text, page)
        self.assertEqual(a11y_problems(page), [])
        self.post("/admin", {"action": "sheet_check"})
        self.assertIn("Таблица записала и вернула строку", self.page("/admin"))
        self.assertEqual(len(self.sheet.records(research.TAB_CHECK)), 1)

    def test_without_google_settings_everything_stays_local(self):
        self.app.config.pop("SHEETS_CLIENT")
        self.app.extensions["akt_sheets"] = None
        self.register()
        self.fill_card()
        self.run_session()
        self.assertEqual(self.q("SELECT COUNT(*) AS n FROM research_outbox")[0]["n"], 0)
        self.assertIn("Не подключены", self.page("/admin"))


class SheetsModuleTest(unittest.TestCase):
    def test_cells_ranges_and_keys(self):
        self.assertEqual([sheets.cell(v) for v in (None, True, 3, 2.5, "=СУММ(A1)")], ["", 1, 3, 2.5, "=СУММ(A1)"])
        self.assertEqual(sheets.quote_range("Идеи пользователя", "1:1"), "'Идеи пользователя'!1:1")
        self.assertEqual(sheets.quote_range("It's", "A1"), "'It''s'!A1")
        key = {"type": "service_account", "client_email": "x@y"}
        self.assertEqual(sheets.load_key(json.dumps(key)), key)
        self.assertEqual(sheets.load_key(base64.b64encode(json.dumps(key).encode()).decode()), key)
        self.assertIsNone(sheets.load_key("  "))

    def test_client_creates_tab_and_header_once(self):
        calls = []

        def fake_call(method, path, params=None, body=None):
            calls.append((method, path, body))
            if path == "":
                return {"sheets": [{"properties": {"title": "Лист1"}}]}
            if method == "GET":
                return {}
            if path.endswith(":append"):
                return {"updates": {"updatedRange": "'Тренировки'!A2:C2"}}
            return {}

        client = object.__new__(sheets.SheetsClient)
        client._tabs = None
        client._call = fake_call
        client.ensure_tab("Тренировки", ["a", "b"])
        self.assertIn(("POST", ":batchUpdate", {"requests": [{"addSheet": {"properties": {"title": "Тренировки"}}}]}),
                      calls)
        self.assertTrue(any(m == "PUT" and b == {"values": [["a", "b"]]} for m, _, b in calls))
        self.assertEqual(client.append("Тренировки", ["x", None]), "'Тренировки'!A2:C2")
        self.assertEqual(calls[-1][2], {"values": [["x", ""]]})


class FakeAppsScript:
    """Ведёт себя как веб-приложение apps_script/Code.gs: строка ищется по record_id и обновляется на месте."""

    mode = "apps_script"
    email = spreadsheet_id = None

    def __init__(self):
        self.tabs, self.fail, self.batches = {}, False, 0

    def ping(self):
        return "Тестовая таблица"

    def upsert_many(self, headers, items):
        if self.fail:
            raise sheets.SheetsError("нет связи с Apps Script: тестовый сбой")
        self.batches += 1
        out = []
        for tab, row in items:
            rows = self.tabs.setdefault(tab, [list(headers[tab])])
            rows[0] = list(headers[tab])
            index = next((i for i, r in enumerate(rows[1:], 2) if r[0] == row[0]), None)
            if index:
                rows[index - 1] = list(row)
            else:
                rows.append(list(row))
                index = len(rows)
            out.append({"tab": tab, "row": index, "id": row[0]})
        return out

    def records(self, tab):
        rows = self.tabs.get(tab, [[]])
        return [dict(zip(rows[0], r)) for r in rows[1:]]


class AppsScriptFlowTest(TrainingCase):
    def setUp(self):
        super().setUp()
        self.sheet = FakeAppsScript()
        self.app.config.update(SHEETS_CLIENT=self.sheet, RESEARCH_SYNC=True, ADMINS="user1")

    def test_one_row_per_session_and_recovery(self):
        self.register()
        self.fill_card()
        sid = self.run_session(plan=True)
        rows = self.sheet.records(research.TAB_SESSIONS)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state"], "done")
        self.sheet.fail = True
        self.post(f"/s/{sid}/done", {"action": "exit_survey", "useful": "4"})
        self.assertIn("пока не записались", self.page(f"/s/{sid}"))
        self.sheet.fail = False
        self.post("/admin", {"action": "sheet_flush"})
        rows = self.sheet.records(research.TAB_SESSIONS)
        self.assertEqual((len(rows), rows[0]["exit_useful"]), (1, 4))
        # outbox забыл диапазоны (как после перезапуска) — строка всё равно одна: таблица ищет её по record_id
        self.execute("UPDATE research_outbox SET sheet_range = NULL, sent_hash = NULL")
        self.post("/admin", {"action": "sheet_flush"})
        self.assertEqual(len(self.sheet.records(research.TAB_SESSIONS)), 1)

    def test_admin_check(self):
        self.register()
        page = self.page("/admin")
        self.assertIn("Apps Script — скрипт в самой таблице", page)
        self.post("/admin", {"action": "sheet_check"})
        self.assertIn("Таблица записала и вернула строку", self.page("/admin"))
        self.assertEqual(len(self.sheet.records(research.TAB_CHECK)), 1)


class _Answer(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class AppsScriptClientTest(unittest.TestCase):
    URL = "https://script.google.com/macros/s/TEST/exec"

    def call(self, body):
        sent = []

        def fake_urlopen(req, timeout=None):
            sent.append(json.loads(req.data.decode("utf-8")))
            return _Answer(body.encode("utf-8"))

        with mock.patch.object(sheets.urllib.request, "urlopen", fake_urlopen):
            client = sheets.AppsScriptClient(self.URL, "секрет")
            result = client.upsert_many({"Т": ["record_id", "a"]}, [("Т", ["id1", None])])
        return sent, result

    def test_request_shape_and_answer(self):
        sent, result = self.call('{"ok": true, "results": [{"tab": "Т", "row": 2, "id": "id1"}]}')
        self.assertEqual(sent[0]["token"], "секрет")
        self.assertEqual(sent[0]["rows"], [{"tab": "Т", "row": ["id1", ""]}])
        self.assertEqual(sent[0]["headers"], {"Т": ["record_id", "a"]})
        self.assertEqual(result[0]["row"], 2)

    def test_errors_are_explained(self):
        with self.assertRaisesRegex(sheets.SheetsError, "неверный токен"):
            self.call('{"ok": false, "error": "неверный токен"}')
        with self.assertRaisesRegex(sheets.SheetsError, "доступ к веб-приложению «Все»"):
            self.call("<html>Вход в аккаунт Google</html>")


class ClientChoiceTest(TrainingCase):
    def fresh(self, env):
        self.app.extensions.pop("akt_sheets", None)
        with mock.patch.dict(os.environ, env, clear=False), self.app.test_request_context():
            return research.client(), self.app.extensions.get("akt_sheets_error")

    def test_apps_script_from_env(self):
        made, error = self.fresh({"APPS_SCRIPT_URL": AppsScriptClientTest.URL, "APPS_SCRIPT_TOKEN": "x"})
        self.assertIsInstance(made, sheets.AppsScriptClient)
        self.assertIsNone(error)

    def test_half_configured_is_reported(self):
        made, error = self.fresh({"APPS_SCRIPT_URL": "https://example.com/exec", "APPS_SCRIPT_TOKEN": ""})
        self.assertIsNone(made)
        self.assertIn("APPS_SCRIPT_URL", error)


if __name__ == "__main__":
    unittest.main()
