import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import library  # noqa: E402

LIB = library.Library()


class LibraryCountsTest(unittest.TestCase):
    def test_lib01(self):
        self.assertEqual(len(LIB.families), 15)
        self.assertEqual(len(LIB.subfamilies), 72)
        f = LIB.families["F01"]
        self.assertEqual(f["seed"], "часто")
        self.assertEqual(f["res"]["В"], "В")
        self.assertIn("F02", f["neighbors"])

    def test_lib02(self):
        self.assertEqual(sum(len(k) for _, k in LIB.keyword_rows), 1010)

    def test_lib03(self):
        self.assertEqual(len(LIB.assumptions), 500)
        self.assertEqual(sum(a["universal"] for a in LIB.assumptions), 250)

    def test_lib04(self):
        self.assertEqual(len(LIB.chains), 169)
        self.assertTrue(all(c["sub"] in LIB.subfamilies for c in LIB.chains))

    def test_lib05(self):
        self.assertEqual(len(LIB.constraints), 537)
        types = {c["type"] for c in LIB.constraints}
        self.assertEqual(types, {"channel", "family_ban", "sub_ban", "resource", "time",
                                 "budget", "scale", "antipode", "transfer", "behavior"})
        self.assertTrue(all(c["strength"] in (1, 2, 3) for c in LIB.constraints))

    def test_lib06(self):
        self.assertEqual(len(LIB.operators), 80)
        self.assertEqual(len(LIB.crossings), 10)

    def test_lib07(self):
        self.assertEqual(len(LIB.contradictions), 210)
        self.assertEqual(len(LIB.requirements), 20)
        self.assertGreaterEqual(len(LIB.limits), 27)
        for k in LIB.contradictions:
            if not k["general"]:
                self.assertIn(k["a"], LIB.requirements)
                self.assertTrue(k["b"] in LIB.limits or k["b"] in LIB.requirements, k)

    def test_lib08(self):
        self.assertEqual(len(LIB.hints["question"]), 120)
        self.assertEqual(len(LIB.hints["transfer"]), 80)
        self.assertEqual(len(LIB.hints["fragment"]), 100)
        kinds = {q["for"] for q in LIB.hints["question"]}
        self.assertEqual(kinds, {"general", "assumptions", "chain", "constraint", "operator", "contradiction", "crossing"})

    def test_lib09_10(self):
        self.assertEqual(len(LIB.failures), 200)
        self.assertEqual(len(LIB.checks), 40)
        check_ids = {c["id"] for c in LIB.checks}
        self.assertTrue(all(f["check"] in check_ids for f in LIB.failures))

    def test_lib11_12_13_14(self):
        self.assertEqual(len(LIB.training), 300)
        self.assertEqual(len(LIB.standard_tasks), 51)
        self.assertEqual(len(LIB.ui), 213)  # 166 + раздел 19 (v0.5) + облако v2, сверка с чат-ботом, смена механизма
        self.assertEqual(len(LIB.baselines), 40)
        self.assertEqual(sum(len(b["answers"]) for b in LIB.baselines.values()), 240)

    def test_lib15_16(self):
        self.assertEqual(len(LIB.rubrics), 8)
        self.assertTrue(all(len(r["levels"]) == 3 for r in LIB.rubrics.values()))
        self.assertEqual(len(LIB.card_fields), 16)
        self.assertEqual(sum(f["short"] for f in LIB.card_fields), 4)  # R10+R11 показываются одним полем
        self.assertIn("F01.3", LIB.skill_needs)

    def test_ui_slots(self):
        self.assertEqual(LIB.t("UI-159", n=1, дата="12.10"), "Пробных тренировок: 1 из 2, до 12.10.")

    def test_number_agreement(self):
        a = library.agree_numbers
        self.assertEqual(a("1 разных направлений из 3 идей"), "1 разное направление из 3 идей")
        self.assertEqual(a("2 разных направлений из 1 идей"), "2 разных направления из 1 идеи")
        self.assertEqual(a("Совпало с вашими: 11 направлений."), "Совпало с вашими: 11 направлений.")
        self.assertEqual(a("21 идей → 3 подходов; 1 тренировок"), "21 идея → 3 подхода; 1 тренировка")
        self.assertEqual(a("в 5 из 1 сессий"), "в 5 из 1 сессии")


class BundledCopyTest(unittest.TestCase):
    """site/libraries — копия для git и хостинга; она должна совпадать с 08_Библиотеки."""

    def test_copy_matches_project_libraries(self):
        if not library.PROJECT_DIR.is_dir():
            self.skipTest("оригинала рядом нет (например, на хостинге)")
        source = {p.name: p.read_bytes() for p in library.PROJECT_DIR.glob("*.md")}
        copy = {p.name: p.read_bytes() for p in library.BUNDLED_DIR.glob("*.md")}
        stale = sorted(n for n in source if copy.get(n) != source[n]) + sorted(set(copy) - set(source))
        self.assertEqual(stale, [], "копия библиотек устарела — запустите sync_libraries.py")

    def test_copy_loads_like_original(self):
        bundled = library.Library(library.BUNDLED_DIR)
        self.assertEqual((len(bundled.families), len(bundled.subfamilies), len(bundled.ui)),
                         (len(LIB.families), len(LIB.subfamilies), len(LIB.ui)))


class LegendTest(unittest.TestCase):
    def test_parse(self):
        card = library.parse_legend("Б: 3 000 ₽ · В: 4 ч · К: двое · П: письма — без ответа")
        self.assertEqual(card["R01"], "до 5 тыс. ₽")
        self.assertEqual(card["R02"], "2–5 ч")
        self.assertEqual(card["R04"], "команда 2–5 человек")
        self.assertEqual(card["R10"], "письма — без ответа")


class DomainClassifierTest(unittest.TestCase):
    def check(self, text, family):
        code, _ = LIB.classifier.classify(text)
        self.assertEqual(code[:3] if code else None, family, text)

    def test_examples(self):
        self.check("Вести инстаграм с работами", "F01")
        self.check("Холодные письма владельцам кафе", "F05")
        self.check("Бесплатный пробный урок", "F06")
        self.check("Скидка на первый заказ", "F07")
        self.check("Добавиться в 2ГИС", "F10")
        self.check("Подарочные сертификаты", "F09")
        self.check("Договориться с соседним салоном о взаимных рекомендациях", "F03")


if __name__ == "__main__":
    unittest.main()
