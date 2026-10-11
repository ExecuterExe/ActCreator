import io
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import llm  # noqa: E402


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def opener_returning(payload):
    def _open(request, timeout=None):
        return FakeResponse(json.dumps(payload).encode("utf-8"))
    return _open


class SettingsTest(unittest.TestCase):
    def test_reads_file_and_ignores_comments(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "settings.env"
            p.write_text("# комментарий\nOPENROUTER_API_KEY= sk-or-123 \nOPENROUTER_MODEL=x/y\n", encoding="utf-8")
            s = llm.load_settings(p)
        self.assertEqual(s["OPENROUTER_API_KEY"], "sk-or-123")
        self.assertEqual(s["OPENROUTER_MODEL"], "x/y")

    def test_empty_key_gives_null_client(self):
        self.assertFalse(llm.make_client({"OPENROUTER_API_KEY": ""}).available)
        self.assertTrue(llm.make_client({"OPENROUTER_API_KEY": "k"}).available)

    def test_project_settings_file_exists_and_parses(self):
        s = llm.load_settings()
        self.assertIn("OPENROUTER_MODEL", s)


class ExtractJsonTest(unittest.TestCase):
    def test_variants(self):
        self.assertEqual(llm.extract_json('{"a": 1}'), {"a": 1})
        self.assertEqual(llm.extract_json('Вот ответ:\n```json\n{"a": [1, 2]}\n```'), {"a": [1, 2]})
        self.assertEqual(llm.extract_json('текст {"s": "скобка } внутри"} хвост'), {"s": "скобка } внутри"})
        self.assertEqual(llm.extract_json('[{"x": 1}]'), [{"x": 1}])
        with self.assertRaises(ValueError):
            llm.extract_json("нет json")


class ClientTest(unittest.TestCase):
    def test_openrouter_parses_content(self):
        client = llm.OpenRouterClient("k", opener=opener_returning({"choices": [{"message": {"content": "привет"}}]}))
        self.assertEqual(client.complete("s", "u"), "привет")

    def test_openrouter_auth_error_not_retried(self):
        calls = []

        def _open(request, timeout=None):
            calls.append(1)
            raise urllib.error.HTTPError(llm.OPENROUTER_URL, 401, "unauthorized", {}, None)

        client = llm.OpenRouterClient("bad", opener=_open)
        with self.assertRaises(llm.LLMError):
            client.complete("s", "u")
        self.assertEqual(len(calls), 1)

    def test_key_sent_in_header_not_in_body(self):
        seen = {}

        def _open(request, timeout=None):
            seen["auth"] = request.get_header("Authorization")
            seen["body"] = request.data.decode("utf-8")
            return FakeResponse(json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode())

        llm.OpenRouterClient("secret-key", opener=_open).complete("s", "u")
        self.assertEqual(seen["auth"], "Bearer secret-key")
        self.assertNotIn("secret-key", seen["body"])


class AIFacadeTest(unittest.TestCase):
    def test_retry_then_success(self):
        answers = iter(["не json", '{"ok": true}'])
        ai = llm.AI(llm.FakeClient(lambda s, u: next(answers)))
        self.assertEqual(ai.ask_json("r", "s", "u", validate=lambda d: d), {"ok": True})

    def test_failure_returns_none_and_logs(self):
        events = []
        ai = llm.AI(llm.FakeClient(lambda s, u: llm.LLMError("сеть")), on_event=lambda n, p: events.append((n, p)))
        self.assertIsNone(ai.ask_json("role_x", "s", "u", validate=lambda d: d))
        self.assertEqual(events[0][0], "ai_error")
        self.assertEqual(events[0][1]["role"], "role_x")

    def test_validation_error_falls_back(self):
        def validate(d):
            raise ValueError("плохо")
        ai = llm.AI(llm.FakeClient(lambda s, u: '{"a": 1}'))
        self.assertIsNone(ai.ask_json("r", "s", "u", validate=validate))

    def test_null_client_never_calls(self):
        ai = llm.AI(llm.NullClient())
        self.assertFalse(ai.available)
        self.assertIsNone(ai.ask_json("r", "s", "u", validate=lambda d: d))
        self.assertIsNone(ai.ask_text("r", "s", "u"))


if __name__ == "__main__":
    unittest.main()
