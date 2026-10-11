import json
from contextlib import closing
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import llm  # noqa: E402
import scoring  # noqa: E402
from app import create_app  # noqa: E402

CSRF_RE = re.compile(r'name="csrf" value="([0-9a-f]+)"')


class ClassifierTest(unittest.TestCase):
    def setUp(self):
        task = scoring.load_task(scoring.default_task_path())
        self.c = scoring.Classifier(task["families"])

    def check(self, text, expected):
        self.assertEqual(self.c.classify(text)[0], expected, text)

    def test_typical_ideas(self):
        self.check("Вести инстаграм и выкладывать сторис", "social")
        self.check("Скидка 20% на первый заказ", "discounts")
        self.check("Карта лояльности: каждый шестой кофе бесплатно", "loyalty")
        self.check("Договориться с соседним фитнес-клубом", "partners")
        self.check("Добавиться в Яндекс Карты и 2ГИС", "maps")
        self.check("Штендер у входа и листовки у метро", "offline_visibility")
        self.check("Проводить настольные игры по вечерам", "events")
        self.check("Позвать местного блогера", "influencers")
        self.check("Возить кофе в офисы бизнес-центра на велосипеде", "formats")

    def test_longer_overlapping_keyword_wins(self):
        # «бизнес-ланч» — продукт, а не партнёрство с бизнесом.
        self.check("Запустить бизнес-ланч", "product")
        # «фотозона» — атмосфера, а не соцсети.
        self.check("Сделать фотозону", "experience")

    def test_no_false_positives(self):
        # «постоянные» не должны считаться «постами», «другой» — «другом».
        self.assertNotEqual(self.c.classify("Постоянные гости")[0], "social")
        self.assertNotEqual(self.c.classify("Другой формат")[0], "referral")
        self.check("Кофе в обмен на рисунок гостя", "pricing_model")
        self.assertIsNone(self.c.classify("Сделать что-то совсем странное")[0])

    def test_parse_ideas_strips_bullets(self):
        raw = "1. Первая\n- Вторая\n\n• Третья\n  2) Четвёртая  "
        self.assertEqual(scoring.parse_ideas(raw), ["Первая", "Вторая", "Третья", "Четвёртая"])


class AppTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "test.db")
        self.app = create_app({"TESTING": True, "DATABASE": self.db_path, "SECRET_KEY": "test",
                               "AI_CLIENT": llm.NullClient()})
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def csrf(self, path):
        page = self.client.get(path).get_data(as_text=True)
        return CSRF_RE.search(page).group(1)

    def register(self, login="tester", password="secret-pass-1", consent=True):
        data = {"csrf": self.csrf("/register"), "login": login, "password": password, "password2": password}
        if consent:
            data["consent"] = "1"
        return self.client.post("/register", data=data)

    def test_post_without_csrf_is_rejected(self):
        self.client.get("/register")
        resp = self.client.post("/register", data={"login": "x", "password": "y"})
        self.assertEqual(resp.status_code, 400)

    def test_register_requires_consent_and_hashes_password(self):
        resp = self.register(consent=False)
        self.assertIn("Без согласия", resp.get_data(as_text=True))
        resp = self.register()
        self.assertEqual(resp.status_code, 302)
        with closing(sqlite3.connect(self.db_path)) as conn:
            stored = conn.execute("SELECT password_hash FROM users").fetchone()[0]
        self.assertNotIn("secret-pass-1", stored)

    def test_duplicate_login_rejected(self):
        self.register()
        self.client.post("/logout", data={"csrf": self.csrf("/me")})
        resp = self.register(login="TESTER")
        self.assertIn("уже занят", resp.get_data(as_text=True))

    def test_login_wrong_password_and_throttling(self):
        self.register()
        self.client.post("/logout", data={"csrf": self.csrf("/me")})
        for _ in range(5):
            resp = self.client.post("/login", data={"csrf": self.csrf("/login"), "login": "tester", "password": "wrong-pass"})
            self.assertIn("Неверный логин или пароль", resp.get_data(as_text=True))
        resp = self.client.post("/login", data={"csrf": self.csrf("/login"), "login": "tester", "password": "secret-pass-1"})
        self.assertIn("Слишком много", resp.get_data(as_text=True))

    def test_login_success(self):
        self.register()
        self.client.post("/logout", data={"csrf": self.csrf("/me")})
        resp = self.client.post("/login", data={"csrf": self.csrf("/login"), "login": "tester", "password": "secret-pass-1"})
        self.assertEqual(resp.status_code, 302)
        self.assertIn("Кабинет", self.client.get("/me").get_data(as_text=True))

    def test_test_requires_login(self):
        resp = self.client.get("/test")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])

    def test_full_flow(self):
        self.register()
        self.assertIn("/test/survey/about", self.client.get("/test").headers["Location"])

        # Нельзя перепрыгнуть шаг.
        self.assertIn("/test/survey/about", self.client.get("/test/task").headers["Location"])

        resp = self.client.post("/test/survey/about", data={"csrf": self.csrf("/test/survey/about")})
        self.assertIn("Ответьте, пожалуйста", resp.get_data(as_text=True))

        self.client.post("/test/survey/about", data={
            "csrf": self.csrf("/test/survey/about"),
            "role": ["freelance", "pet"], "field": "design", "freq": "monthly", "goal": "solve_now",
        })
        self.client.post("/test/survey/case", data={
            "csrf": self.csrf("/test/survey/case"),
            "last_when": "month", "last_task": "Как найти первых клиентов для студии без бюджета",
            "last_tried": ["ai", "self"], "last_result": "ordinary",
        })
        self.client.post("/test/survey/self", data={
            "csrf": self.csrf("/test/survey/self"),
            "se_ideas": "3", "se_confidence": "4", "se_strategy": "2",
        })
        self.assertIn("запустить таймер", self.client.get("/test/task").get_data(as_text=True))
        self.client.get("/test/task?go=1")
        ideas = "Скидка на первый кофе\nВести инстаграм\nДоговориться с коворкингом рядом\nКофе в обмен на рисунок гостя\nПеть гостям песни"
        resp = self.client.post("/test/task", data={"csrf": self.csrf("/test/task"), "ideas": ideas})
        self.assertIn("/test/pick", resp.headers["Location"])

        resp = self.client.post("/test/pick", data={"csrf": self.csrf("/test/pick"), "pick": ["0"]})
        self.assertIn("ровно две", resp.get_data(as_text=True))
        resp = self.client.post("/test/pick", data={"csrf": self.csrf("/test/pick"), "pick": ["2", "4"]})
        self.assertIn("/test/result", resp.headers["Location"])

        page = self.client.get("/test/result").get_data(as_text=True)
        self.assertIn("Облако очевидного", page)
        self.assertIn("<b>5</b> идей", page)
        self.assertIn("Незанятые пространства", page)

        with closing(sqlite3.connect(self.db_path)) as conn:
            result = json.loads(conn.execute("SELECT result_json FROM attempts").fetchone()[0])
        self.assertEqual(result["n_families"], 4)
        self.assertEqual(result["n_unknown"], 1)
        self.assertEqual(result["common_count"], 2)
        self.assertEqual(result["first_uncommon_pos"], 3)
        self.assertEqual([i["text"] for i in result["top2"]], ["Договориться с коворкингом рядом", "Петь гостям песни"])

        resp = self.client.post("/test/feedback", data={
            "csrf": self.csrf("/test/result"), "useful": "5", "surprise": ["order"], "comment": "ок",
        })
        self.assertIn("Спасибо", self.client.get("/test/result").get_data(as_text=True))

    def test_zero_ideas(self):
        self.register()
        self.client.post("/test/survey/about", data={
            "csrf": self.csrf("/test/survey/about"), "role": ["student"], "field": "other", "freq": "never", "goal": "curious",
        })
        self.client.post("/test/survey/case", data={"csrf": self.csrf("/test/survey/case"), "last_when": "none"})
        self.client.post("/test/survey/self", data={
            "csrf": self.csrf("/test/survey/self"), "se_ideas": "1", "se_confidence": "1", "se_strategy": "1",
        })
        self.client.get("/test/task?go=1")
        self.client.post("/test/task", data={"csrf": self.csrf("/test/task"), "ideas": "  \n "})
        self.client.get("/test/pick")
        page = self.client.get("/test/result").get_data(as_text=True)
        self.assertIn("не записали ни одной идеи", page)


if __name__ == "__main__":
    unittest.main()
