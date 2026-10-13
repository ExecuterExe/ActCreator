"""Доступность: каждая страница, которую сервис отдаёт по пути пользователя, понятна экранному диктору.

Проверяем то, что диктор читает из разметки: язык страницы, один заголовок первого уровня, у каждого поля
есть подпись, переключатели собраны в группу с названием вопроса, у кнопок и ссылок есть текст, id не
повторяются. Это не заменяет прослушивание в NVDA/VoiceOver, но ловит самые частые поломки при правках."""

import json
import unittest
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

import llm
from helpers import TrainingCase
from test_e2e import FullChainMixin, contract_ai

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
NO_NAME_NEEDED = {"hidden", "submit", "button", "reset", "image"}


class A11yParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []        # открытые элементы: (тег, атрибуты)
        self.problems = []
        self.ids = {}
        self.label_for = set()
        self.need_for = []     # поля, которым подпись может дать только <label for>
        self.h1 = 0
        self.lang = None
        self.named = []        # кнопки и ссылки, у которых копим текст: [тег, атрибуты, текст]

    def _inside(self, test):
        return any(test(tag, attrs) for tag, attrs in self.stack)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "html":
            self.lang = a.get("lang")
        if a.get("id"):
            self.ids[a["id"]] = self.ids.get(a["id"], 0) + 1
        if tag == "label" and a.get("for"):
            self.label_for.add(a["for"])
        if tag == "h1":
            self.h1 += 1
        if tag == "img" and "alt" not in a:
            self.problems.append(f"img без alt: {a.get('src')}")
        if tag in ("input", "select", "textarea") and a.get("type", "text") not in NO_NAME_NEEDED:
            name = a.get("name") or a.get("id") or tag
            labelled = (self._inside(lambda t, _: t == "label") or a.get("aria-label") or a.get("aria-labelledby")
                        or a.get("title"))
            if not labelled:
                self.need_for.append((a.get("id"), f"{tag}[{name}]"))
            if a.get("type") == "radio" and not self._inside(
                    lambda t, at: t == "fieldset" or (at.get("role") == "radiogroup"
                                                      and (at.get("aria-label") or at.get("aria-labelledby")))):
                self.problems.append(f"переключатель {name} вне группы с названием (fieldset/legend)")
        if tag in ("button", "a") and (tag == "button" or "href" in a):
            self.named.append([tag, a, ""])
        if tag not in VOID:
            self.stack.append((tag, a))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID and self.stack and self.stack[-1][0] == tag:
            self.stack.pop()

    def handle_endtag(self, tag):
        if tag in ("button", "a") and self.named and self.named[-1][0] == tag:
            t, a, text = self.named.pop()
            if not text.strip() and not a.get("aria-label") and not a.get("aria-labelledby") and not a.get("title"):
                self.problems.append(f"{t} без текста: {a.get('href') or a.get('name') or a.get('class')}")
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        for item in self.named:
            item[2] += data

    def report(self):
        problems = list(self.problems)
        if self.lang != "ru":
            problems.append(f"html lang={self.lang!r}")
        if self.h1 != 1:
            problems.append(f"заголовков h1: {self.h1}")
        problems += [f"повторяется id={i}" for i, n in self.ids.items() if n > 1]
        problems += [f"поле без подписи: {what}" for fid, what in self.need_for if not fid or fid not in self.label_for]
        return problems


def a11y_problems(html):
    p = A11yParser()
    p.feed(html)
    p.close()
    return p.report()


class A11yMixin:
    """Подменяет запросы тестового клиента: каждая отданная HTML-страница проходит проверку."""

    def setUp(self):
        super().setUp()
        self.problems = {}
        self.pages_seen = set()
        original = self.c.open

        def open_and_check(*args, **kwargs):
            resp = original(*args, **kwargs)
            if resp.status_code == 200 and resp.mimetype == "text/html":
                path = resp.request.path
                self.pages_seen.add(path)
                found = a11y_problems(resp.get_data(as_text=True))
                if found:
                    self.problems.setdefault(path, set()).update(found)
            return resp

        self.c.open = open_and_check

    def assertAccessible(self, min_pages):
        self.assertEqual({k: sorted(v) for k, v in self.problems.items()}, {})
        self.assertGreaterEqual(len(self.pages_seen), min_pages, sorted(self.pages_seen))


class ParserSelfTest(unittest.TestCase):
    """Проверка самой проверки: ловит то, что должна ловить."""

    def test_detects_typical_breakages(self):
        html = ('<html lang="en"><body><h1>a</h1><h1>b</h1><input type="text" name="x">'
                '<input type="radio" name="r"><button></button><a href="/y"></a><img src="i.png">'
                '<div id="d"></div><div id="d"></div></body></html>')
        found = " | ".join(a11y_problems(html))
        for part in ("lang='en'", "h1: 2", "поле без подписи: input[x]", "переключатель r", "button без текста",
                     "a без текста", "img без alt", "id=d"):
            self.assertIn(part, found)

    def test_accepts_labelled_markup(self):
        html = ('<html lang="ru"><body><h1>a</h1><label>Имя <input type="text" name="x"></label>'
                '<label for="s">Статус</label><select id="s" name="s"></select>'
                '<fieldset><legend>Вопрос</legend><label><input type="radio" name="r"> да</label></fieldset>'
                '<button>Ок</button><a href="/y" aria-label="Домой"><svg></svg></a></body></html>')
        self.assertEqual(a11y_problems(html), [])


class GuestPagesTest(A11yMixin, TrainingCase):
    def test_guest_pages(self):
        for path in ("/", "/login", "/register"):
            self.c.get(path)
        self.c.post("/register", data={"csrf": self.csrf("/register"), "login": "x"})  # страница с ошибками
        self.c.post("/login", data={"csrf": self.csrf("/login"), "login": "nobody", "password": "wrong-pass-1"})
        self.assertAccessible(3)


class TrainingPathNoAITest(A11yMixin, FullChainMixin, TrainingCase):
    def test_full_chain_pages(self):
        self.full_chain()
        self.assertAccessible(15)

    def test_versions_and_ladder(self):
        sid = self.to_idea()
        self.idea(sid, "submit", "Скидка 15% на второй заказ")  # ожидающая версия: «та же ветка»
        self.page(f"/s/{sid}/idea")
        self.idea(sid, "improve")
        self.idea(sid, "ladder_start", "наброс")
        for step in ({"action": "ladder_1", "obstacle": "Все идеи кажутся одинаковыми"},
                     {"action": "ladder_2", "barrier": "редактор не знает меня"},
                     {"action": "ladder_3", "operation": "участника"}):
            self.page(f"/s/{sid}/idea")
            self.post(f"/s/{sid}/idea", step)
        self.page(f"/s/{sid}/idea")
        self.assertAccessible(5)

    def test_every_tool_screen(self):
        self.register()
        self.fill_card()
        for code, ttype in (("Д1", "operator"), ("Д2", "assumptions"), ("Д3", "chain"), ("Д3", "constraint"),
                            ("Д4", "contradiction"), ("Д5", "crossing"), ("Д3", "reframe")):
            sid = self.start()
            self.set_task(sid)
            self.submit_first(sid, ["Скидка 10% на первый заказ", "Скидка 20% постоянным", "Промокод для новых"], 1)
            self.page(f"/s/{sid}/mirror")
            self.post(f"/s/{sid}/mirror", {"action": "continue"})
            self.force_diagnosis(sid, code, ttype)
            self.page(f"/s/{sid}/tool")
            self.execute("UPDATE sessions SET state = 'abandoned' WHERE id = ?", (sid,))  # освободить место
        self.assertEqual(len({p for p in self.pages_seen if p.endswith("/tool")}), 7)
        phases = {json.loads(r["tool_json"])["phase"] for r in self.q("SELECT tool_json FROM sessions")}
        self.assertIn("select", phases)  # допущения: список утверждений с флажками
        self.assertAccessible(7)

    def test_level_two_chooses_tool(self):
        sid = self.to_idea(level=2)  # самодиагноз и выбор инструмента самим человеком
        self.page(f"/s/{sid}/idea")
        self.assertAccessible(5)

    def test_practice_and_entry_test(self):
        self.register()
        self.fill_card()
        sid = self.start(mode="practice")
        self.page(f"/s/{sid}")
        self.post("/test/survey/about", {"role": ["freelance"], "field": "design", "freq": "monthly",
                                         "goal": "solve_now"})
        self.post("/test/survey/case", {"last_when": "none"})
        self.post("/test/survey/self", {"se_ideas": "3", "se_confidence": "4", "se_strategy": "2"})
        self.page("/test/task")
        self.c.get("/test/task?go=1")
        self.post("/test/task", {"ideas": "Скидка на первый кофе\nВести инстаграм\nДоговориться с коворкингом"})
        self.page("/test/pick")
        self.post("/test/pick", {"pick": ["0", "2"]})
        self.page("/test/result")
        for path in ("/me", "/bank", "/summary", "/checkpoint", "/train"):
            self.page(path)
        self.assertAccessible(10)


class BetweenSessionsTest(A11yMixin, TrainingCase):
    def test_checkpoint_and_month(self):
        self.register()
        self.fill_card()
        self.run_session(plan=True)
        self.run_session()
        past = (datetime.now(timezone.utc) - timedelta(days=15)).isoformat(timespec="seconds")
        self.execute("UPDATE sessions SET completed_at = ? WHERE id = (SELECT MIN(id) FROM sessions)", (past,))
        self.page("/me")  # приглашение на контрольную точку и вопросы об исходах
        self.page("/checkpoint")
        self.post("/checkpoint/start", {}, csrf_from="/checkpoint")
        cid = self.q("SELECT id FROM checkpoints")[0]["id"]
        self.page(f"/checkpoint/{cid}/task")
        self.post(f"/checkpoint/{cid}/task", {"ideas": "Скидка новым гостям\nКонкурс в соцсетях\nПеть песни у входа"})
        self.post(f"/checkpoint/{cid}/pick", {"pick": ["0"]})  # ошибка: нужно ровно две
        self.post(f"/checkpoint/{cid}/pick", {"pick": ["0", "2"]})
        self.page(f"/checkpoint/{cid}")
        final = self.q("SELECT id FROM ideas WHERE stage = 'final' ORDER BY id")[0]["id"]
        self.page(f"/bank/{final}")
        self.page("/summary")
        self.assertAccessible(8)


class TrainingPathContractAITest(A11yMixin, FullChainMixin, TrainingCase):
    """С ответами ИИ появляются линзы, рентген и разбор сдвига — их разметка тоже проверяется."""

    ai_client = llm.FakeClient(contract_ai)

    def test_full_chain_pages(self):
        self.full_chain()
        self.assertAccessible(15)


if __name__ == "__main__":
    unittest.main()
