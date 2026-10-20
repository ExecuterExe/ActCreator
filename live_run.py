"""Живой прогон тренировки на настоящем ИИ (OpenRouter → модель из settings.env) с замером времени.

Запуск:  .venv\\Scripts\\python live_run.py
- работает на отдельной временной базе — рабочую базу не трогает;
- ответы «пользователя» заданы в сценариях ниже (это не живой человек), ответы ИИ — настоящие;
- пишет в exports/: live_run_<дата>.json (замеры и вызовы ИИ) и live_run_<дата>.txt (что видно на экранах).
Ключ берётся из settings.env и никуда не выводится.
"""

import html
import json
import re
import sys
import tempfile
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import llm
from app import create_app

BASE = Path(__file__).parent
CSRF_RE = re.compile(r'name="csrf" value="([0-9a-f]+)"')

SCENARIOS = [
    {
        "name": "А. Иллюстратор — все первые идеи в одной колее",
        "task": {"text": "Иллюстратор-фрилансер хочет найти новых заказчиков среди небольших издательств. "
                         "Портфолио есть, рекламы не было.",
                 "goal": "3 новых заказа за месяц", "bans": "без платной рекламы", "in_domain": "yes"},
        "first": ["Скидка 10% на первый заказ", "Скидка 20% постоянным", "Промокод для новых"],
        "favorite": 1, "why": "Скидка мотивирует попробовать",
        "hints": ["question", "fragment"], "lens": "behavior", "wow": "Скорее нет",
        "pre_versions": ["Скидка 30% на первую обложку для нового издательства"],
        "final": "Нарисовать обложку-образец к книге из ближайшего плана издательства и отправить редактору: "
                 "ему проще оценить готовую работу к своей книге, чем листать портфолио",
        "reasons": ["Редактор не откроет письмо от незнакомого человека"],
        "defense": [("1", "argument", "Редакторы малых издательств сами ищут иллюстраторов, а письмо с обложкой "
                                      "к их книге заметно отличается от обычной рассылки портфолио"),
                    ("0", "fact", "У меня есть два знакомых автора, которые издавались в этих издательствах"),
                    ("1", "change", "Сначала отправлю короткий эскиз и спрошу, интересно ли довести его до конца")],
        "helped": "mirror", "full": True,
    },
    {
        "name": "Б. Репетитор — разные, но привычные идеи",
        "task": {"text": "Репетитор по английскому полгода работает онлайн, учеников мало. Хочет набрать школьников.",
                 "goal": "5 новых учеников за 2 месяца", "bans": "", "in_domain": "yes"},
        "first": ["Вести блог с советами по английскому", "Разместить анкету на Профи.ру",
                  "Скидка на первое занятие"],
        "favorite": 1, "why": "Люди увидят, что я разбираюсь, и придут",
        "hints": ["transfer"], "lens": "value", "wow": "Скорее да",
        "final": "Договориться со школьным учителем английского: он рекомендует меня ученикам, которым нужны "
                 "дополнительные занятия, а я раз в месяц бесплатно провожу для его класса разговорный клуб",
        "reasons": ["Учителю это неинтересно", "Родители не доверят незнакомому репетитору"],
        "defense": [("1", "fact", "Я сам учился у учителя, который так работал с репетиторами"),
                    ("1", "change", "Начну с одного разговорного клуба без обязательств и попрошу учителя "
                                    "только передать ученикам приглашение"),
                    ("0", "agree", "")],
        "helped": "tool", "full": True,
    },
    {
        "name": "В. Задача вне сферы — очередь в школьной столовой (до шага «Своя идея»)",
        "task": {"text": "В школьной столовой на большой перемене огромная очередь, половина учеников "
                         "не успевает поесть.",
                 "goal": "Чтобы все успевали поесть за 20 минут перемены уже со следующей недели", "bans": "",
                 "in_domain": "no"},
        "first": ["Поставить ещё одну кассу", "Продлить перемену", "Раздавать еду заранее"],
        "favorite": 1, "why": "Очередь пойдёт быстрее",
        "hints": ["question"], "lens": "system", "full": False,
    },
    {
        "name": "Г. Кофейня, уровень 3 — человек сам выбирает «Перевёрнутую задачу»",
        "task": {"text": "Небольшая кофейня в спальном районе работает полгода, утром очередь, а днём почти пусто.",
                 "goal": "Чтобы днём выручка выросла вдвое за 2 месяца", "bans": "без скидок", "in_domain": "yes"},
        "first": ["Скидка на кофе днём", "Бизнес-ланч", "Реклама в районном чате"],
        "favorite": 1, "why": "Днём людям нужен повод зайти", "level": 3, "tool_choice": "reframe",
        "hints": [], "lens": "behavior", "wow": "Нет",
        "final": "Сделать днём тихий рабочий зал для фрилансеров района: столы с розетками, кофе по подписке "
                 "и тишина до 17:00",
        "reasons": ["Фрилансеры будут сидеть часами с одной чашкой"],
        "defense": [("1", "change", "Подписка оплачивается заранее за неделю, поэтому выручка не зависит от "
                                    "того, сколько чашек выпьют"),
                    ("1", "argument", "Днём зал всё равно пустует, любой гость лучше пустых столов"),
                    ("0", "agree", "")],
        "helped": "tool", "full": True,
    },
]

ROLE_MARKERS = [("Разбери идею", "рентген идеи (Р11)"), ("Сравни новую идею", "детектор сдвига (Р12)"),
                ("аналитик механизмов", "детектор сдвига (Р12)"),  # v2, рабочий с 13.10
                ("ответы обычного чат-бота на ту же задачу", "сверка с чат-ботом (Р15)"),
                ("Найди общее в первых идеях", "что общего у идей (Р16)"),
                ("Составь план самой дешёвой проверки", "план проверки (Р5 v2)"),
                ("через три разные модели", "три линзы (Р13)"),
                ("цель на уровень выше", "перевёрнутая задача (Р14)"),
                ("Оцени формулировку", "проверка задачи (Р0)"), ("Построй 8–12 семейств", "временная таксономия"),
                ("отнести идею", "классификатор (Р1)"), ("Перепиши шаблон", "адаптация задания (Р5)"),
                ("причинное допущение", "извлечение допущения (Р4)"), ("Дай ОДНУ подсказку", "коуч-подсказка (Р6)"),
                ("Оцени идею по рубрикам", "рубрика (Р9)"), ("Ты критик", "критик (Р7)"), ("Ты судья", "судья (Р8)")]


def detect_role(system, user):
    if "Предложи 6 идей" in user:
        return "ответы чат-бота (Р3)"
    if "Перепиши шаблон" in system and "проверка за 48" in user:
        return "адаптация проверки (Р5)"
    return next((name for marker, name in ROLE_MARKERS if marker in system), "другое")


class TimingClient:
    """Обёртка над настоящим клиентом: время, токены и ответ каждого вызова."""

    available = True

    def __init__(self, inner):
        self.inner = inner
        self.calls = []

    def complete(self, system, user, max_tokens=600, temperature=0.3):
        role, t0, out, err = detect_role(system, user), time.perf_counter(), None, None
        try:
            out = self.inner.complete(system, user, max_tokens=max_tokens, temperature=temperature)
            return out
        except Exception as exc:
            err = str(exc)
            raise
        finally:
            usage = getattr(self.inner, "last_usage", {}) if err is None else {}
            self.calls.append({"role": role, "seconds": round(time.perf_counter() - t0, 2), "ok": err is None,
                               "error": err, "prompt_tokens": usage.get("prompt_tokens", 0),
                               "completion_tokens": usage.get("completion_tokens", 0),
                               "output": (out or "")[:700]})


def page_text(body):
    m = re.search(r"<main>(.*)</main>", body, re.S)
    body = m.group(1) if m else body
    body = re.sub(r"<script.*?</script>", "", body, flags=re.S)
    body = re.sub(r"<(br|/p|/li|/h\d|/tr|/label|/legend|/div|/summary)[^>]*>", "\n", body)
    body = re.sub(r"<[^>]+>", " ", body)
    lines = [re.sub(r"\s+", " ", html.unescape(line)).strip() for line in body.splitlines()]
    return "\n".join(line for line in lines if line)


def model_pricing(model):
    """Цены модели из публичного списка OpenRouter (USD за токен) или None."""
    try:
        with urllib.request.urlopen("https://openrouter.ai/api/v1/models", timeout=20) as r:
            data = json.loads(r.read().decode("utf-8"))
        item = next((m for m in data.get("data", []) if m.get("id") == model), None)
        if item:
            return float(item["pricing"]["prompt"]), float(item["pricing"]["completion"])
    except Exception:
        return None
    return None


class Runner:
    def __init__(self, client, db_path, flow=None):
        self.timing = client
        config = {"TESTING": True, "DATABASE": db_path, "SECRET_KEY": "live-run", "AI_CLIENT": client,
                  "FULL_ACCESS": True}
        if flow:  # путь тренировки: short (по умолчанию в сервисе с 18.10) или full
            config["DEFAULT_FLOW"] = flow
        self.app = create_app(config)
        self.c = self.app.test_client()
        self.csrf = None
        self.steps, self.screens = [], []
        self.think = 0  # секунд «человек пишет первые идеи» между задачей и отправкой идей (по умолчанию без паузы)
        self.read = 0  # секунд «человек читает карточку версии» перед «Оставить версию»
        self.look = 0  # секунд «человек читает зеркало» перед «Дальше»

    def _ai_slice(self, start):
        return self.timing.calls[start:]

    def request(self, scenario, step, method, path, data=None, screen=True):
        start, t0 = len(self.timing.calls), time.perf_counter()
        if method == "GET":
            resp = self.c.get(path, follow_redirects=True)
        else:
            data = dict(data or {})
            data["csrf"] = self.csrf
            resp = self.c.post(path, data=data, follow_redirects=True)
        seconds = round(time.perf_counter() - t0, 2)
        calls = self._ai_slice(start)
        self.steps.append({"scenario": scenario, "step": step, "method": method, "path": path,
                           "status": resp.status_code, "seconds": seconds, "ai_calls": len(calls),
                           "ai_seconds": round(sum(c["seconds"] for c in calls), 2),
                           "roles": [c["role"] for c in calls]})
        body = resp.get_data(as_text=True)
        if resp.status_code >= 400:
            raise RuntimeError(f"{step}: HTTP {resp.status_code}")
        if screen:
            self.screens.append((scenario, step, page_text(body)))
        return body

    def db(self, sql, args=()):
        with self.app.app_context():
            from db import get_db
            return [dict(r) for r in get_db().execute(sql, args).fetchall()]

    def state(self, sid):
        return self.db("SELECT state FROM sessions WHERE id = ?", (sid,))[0]["state"]

    def run(self, sc, login):
        name = sc["name"]
        if self.csrf:  # предыдущий сценарий — другой пользователь
            self.c.post("/logout", data={"csrf": self.csrf})
        self.c.post("/register", data={"login": login, "password": "live-run-pass-1", "password2": "live-run-pass-1",
                                       "consent": "1", "csrf": self._fresh_csrf("/register")})
        self.csrf = self._fresh_csrf("/me")
        self.request(name, "Карточка ресурсов", "POST", "/train/card?next=/train",
                     {"R01": "0 ₽", "R02": "2–5 ч", "R10": "посты в соцсетях — без результата", "ai_notice": "1"},
                     screen=False)
        if sc.get("level"):
            with self.app.app_context():
                from db import get_db
                conn = get_db()
                conn.execute("UPDATE user_state SET level = ?", (sc["level"],))
                conn.commit()
        self.request(name, "Старт", "POST", "/train/start", {"mode": "task"}, screen=False)
        sid = self.db("SELECT MAX(id) AS id FROM sessions")[0]["id"]
        p = f"/s/{sid}"
        self.request(name, "С1 Задача: отправка", "POST", f"{p}/task", sc["task"])
        if self.state(sid) == "task":  # предупреждение о постановке — продолжаем как есть
            self.request(name, "С1 Задача: подтверждение", "POST", f"{p}/task", dict(sc["task"], confirm="1"))
        data = {f"idea{i}": t for i, t in enumerate(sc["first"], 1)}
        data.update({"favorite": str(sc["favorite"]), "why": sc["why"]})
        time.sleep(self.think)
        self.request(name, "С2 Первые идеи: отправка", "POST", f"{p}/first", data, screen=False)
        self.request(name, "С3 Зеркало", "GET", f"{p}/mirror")
        time.sleep(self.look)
        self.request(name, "С4 Диагноз (линза выбрана)", "POST", f"{p}/mirror",
                     {"action": "continue", "lens": sc.get("lens", "behavior")}, screen=False)
        self.request(name, "С5 Инструмент", "GET", f"{p}/tool")
        tool_row = self.db("SELECT tool_json FROM sessions WHERE id = ?", (sid,))[0]["tool_json"]
        if tool_row is None:  # уровень 3: человек сам выбирает инструмент
            self.request(name, "С5 Выбор инструмента", "POST", f"{p}/tool",
                         {"action": "choose", "tool": sc.get("tool_choice", "operator"),
                          "self_diag": "Держусь за любимую идею"})
            tool_row = self.db("SELECT tool_json FROM sessions WHERE id = ?", (sid,))[0]["tool_json"]
        tool = json.loads(tool_row)
        if tool["phase"] == "reframe_confirm":
            self.request(name, "С5 Перевёрнутая задача: да", "POST", f"{p}/tool", {"action": "reframe_yes"})
            tool = json.loads(self.db("SELECT tool_json FROM sessions WHERE id = ?", (sid,))[0]["tool_json"])
        if tool["phase"] == "reframe_why":  # без ответа ИИ человек сам называет цель за целью
            self.request(name, "С5 Перевёрнутая задача: своя цель", "POST", f"{p}/tool",
                         {"action": "reframe_own", "goal_behind": "стабильная выручка весь день"})
            tool = json.loads(self.db("SELECT tool_json FROM sessions WHERE id = ?", (sid,))[0]["tool_json"])
        if tool["phase"] == "target":
            self.request(name, "С5 Направление", "POST", f"{p}/tool", {"action": "target", "target": tool["targets"][0]})
            tool = json.loads(self.db("SELECT tool_json FROM sessions WHERE id = ?", (sid,))[0]["tool_json"])
        if tool["phase"] == "select":
            self.request(name, "С5 Инструмент: выбор допущения", "POST", f"{p}/tool",
                         {"action": "select", "chosen": tool["items"][0]["id"], "about_me": tool["items"][0]["id"]})
        if tool["phase"] == "why":
            self.request(name, "С5 Инструмент: почему", "POST", f"{p}/tool", {"action": "why", "why": sc["why"]})
        self.request(name, "С5 Инструмент → идея", "POST", f"{p}/tool", {"action": "go"}, screen=False)
        for kind in sc["hints"]:
            self.request(name, f"С6 Подсказка: {kind}", "POST", f"{p}/idea", {"hint": kind, "idea": ""})
        if not sc["full"]:
            return sid
        for k, text in enumerate(sc.get("pre_versions", []), 1):  # ранние версии — затем «доработать»
            self.request(name, f"С6 Версия {k}: рентген + детектор сдвига", "POST", f"{p}/idea",
                         {"action": "submit", "idea": text, "self_check": "no"})
            self.request(name, f"С6 Версия {k}: доработать", "POST", f"{p}/idea", {"action": "improve"}, screen=False)
        self.request(name, "С6 Итоговая версия: рентген + детектор сдвига", "POST", f"{p}/idea",
                     {"action": "submit", "idea": sc["final"], "self_check": "no"})
        time.sleep(self.read)
        self.request(name, "С6 Оставить версию → оценка", "POST", f"{p}/idea", {"action": "accept_version"})
        if self.state(sid) == "choose":  # короткий путь (по умолчанию с 18.10): выбор вместо оценки и пре-мортема
            version = self.db("SELECT id FROM ideas WHERE session_id = ? AND stage = 'version' ORDER BY id DESC "
                              "LIMIT 1", (sid,))[0]["id"]
            time.sleep(self.read)  # человек читает варианты; план проверки тем временем готовится в фоне
            self.request(name, "С7 Выбор решения → проверка", "POST", f"{p}/choose",
                         {"chosen": str(version), "why": sc.get("choose_why", "Укладывается в мои ресурсы"),
                          "wow": sc.get("wow", "Скорее нет"), "action": "check"})
        else:
            self.full_tail(name, sc, sid, p)
        plan = json.loads(self.db("SELECT flags_json FROM sessions WHERE id = ?", (sid,))[0]["flags_json"])["check_plan"]
        self.request(name, "С10 Проверка: сохранить план", "POST", f"{p}/check",
                     {"action": "save", "plan": plan["action"], "when": "завтра",
                      "hypothesis": plan.get("hypothesis") or "", "audience": plan.get("audience") or "",
                      "ok": plan.get("ok") or plan["template"]["ok"], "fail": plan.get("fail") or plan["template"]["fail"]})
        return sid

    def full_tail(self, name, sc, sid, p):
        """Полный путь: оценка, пре-мортем с критиком и судьёй, маршрут мысли."""
        self.request(name, "С7 Оценка: беру в работу", "POST", f"{p}/eval", {"action": "take"}, screen=False)
        self.request(name, "С7 Оценка → дальше", "POST", f"{p}/eval", {"action": "next"}, screen=False)
        reasons = {f"reason{i}": r for i, r in enumerate(sc["reasons"], 1)}
        self.request(name, "С8 Пре-мортем: причины → критик", "POST", f"{p}/premortem",
                     dict(reasons, action="reasons"))
        critic = self.db("SELECT id FROM objections WHERE session_id = ? AND author = 'critic' ORDER BY id", (sid,))
        if critic:
            data = {"action": "defend"}
            for o, (about, rtype, text) in zip(critic, sc["defense"]):
                data.update({f"about_{o['id']}": about, f"type_{o['id']}": rtype, f"text_{o['id']}": text})
            for o in critic[len(sc["defense"]):]:
                data.update({f"about_{o['id']}": "1", f"type_{o['id']}": "agree"})
            self.request(name, "С8 Пре-мортем: защита → судья", "POST", f"{p}/premortem", data)
        self.request(name, "С8 Пре-мортем → дальше", "POST", f"{p}/premortem", {"action": "finish"})
        self.request(name, "С9 Маршрут → дальше", "POST", f"{p}/route",
                     {"helped": sc["helped"], "wow": sc.get("wow", "Скорее нет")})

    def _fresh_csrf(self, path):
        m = CSRF_RE.search(self.c.get(path, follow_redirects=True).get_data(as_text=True))
        return m.group(1) if m else ""

    def snapshot(self, sid):
        s = self.db("SELECT * FROM sessions WHERE id = ?", (sid,))[0]
        out = {k: json.loads(s[k]) if s[k] else None for k in
               ("task_json", "mirror_json", "diagnosis_json", "tool_json", "eval_json", "route_json", "flags_json")}
        out["state"] = s["state"]
        out["ideas"] = self.db("SELECT stage, ord, version, text, family, subfamily, class_source, explanation, "
                               "status, xray_json, change_json FROM ideas WHERE session_id = ? ORDER BY id", (sid,))
        out["hints"] = self.db("SELECT kind, lib_id, text, source FROM hints WHERE session_id = ?", (sid,))
        out["objections"] = self.db("SELECT author, category, card_field, mechanism_link, condition, check_text, "
                                    "severity, about_me, response_type, response_text, verdict, basis, reason, "
                                    "verdict_source FROM objections WHERE session_id = ? ORDER BY id", (sid,))
        out["experiment"] = self.db("SELECT plan, due, success_signal, fail_signal FROM experiments "
                                    "WHERE session_id = ?", (sid,))
        out["ai_errors"] = [json.loads(e["props_json"]) for e in
                            self.db("SELECT props_json FROM events WHERE name = 'ai_error'")]
        return out


def main():
    settings = llm.load_settings()
    if not (settings.get("OPENROUTER_API_KEY") or "").strip():
        print("Ключ не найден в settings.env — живой прогон невозможен.")
        return 1
    model = settings.get("OPENROUTER_MODEL") or llm.DEFAULT_MODEL
    timing = TimingClient(llm.make_client(settings))
    stamp = f"{datetime.now():%Y%m%d_%H%M}"
    out_dir = BASE / "exports"
    out_dir.mkdir(exist_ok=True)
    result = {"model": model, "started": datetime.now().isoformat(timespec="seconds"), "scenarios": []}
    with tempfile.TemporaryDirectory() as tmp:
        runner = Runner(timing, str(Path(tmp) / "live.db"),
                        flow=sys.argv[sys.argv.index("--flow") + 1] if "--flow" in sys.argv else None)
        if "--think" in sys.argv:  # пауза «человек пишет первые идеи», секунд: live_run.py --think 60
            runner.think = float(sys.argv[sys.argv.index("--think") + 1])
        if "--read" in sys.argv:  # пауза «человек читает карточку версии»: live_run.py --read 20
            runner.read = float(sys.argv[sys.argv.index("--read") + 1])
        if "--look" in sys.argv:  # пауза «человек читает зеркало»: live_run.py --look 15
            runner.look = float(sys.argv[sys.argv.index("--look") + 1])
        result["think_seconds"], result["read_seconds"], result["look_seconds"] = runner.think, runner.read, runner.look
        result["flow"] = runner.app.config.get("DEFAULT_FLOW") or "short (по умолчанию)"
        for i, sc in enumerate(SCENARIOS, 1):
            t0, start_calls = time.perf_counter(), len(timing.calls)
            print(f"Сценарий {sc['name']} ...", flush=True)
            error = None
            sid = None
            try:
                sid = runner.run(sc, f"live{i}")
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                print("  ОШИБКА:", error)
            calls = timing.calls[start_calls:]
            entry = {"name": sc["name"], "error": error, "wall_seconds": round(time.perf_counter() - t0, 1),
                     "ai_calls": len(calls), "ai_seconds": round(sum(c["seconds"] for c in calls), 1),
                     "calls": calls,
                     "steps": [s for s in runner.steps if s["scenario"] == sc["name"]]}
            if sid is None:
                sid = runner.db("SELECT MAX(id) AS id FROM sessions")[0]["id"]
            if sid:
                entry["session"] = runner.snapshot(sid)
            result["scenarios"].append(entry)
            print(f"  готово: {entry['wall_seconds']} с, вызовов ИИ: {entry['ai_calls']}, "
                  f"из них ожидание ИИ {entry['ai_seconds']} с", flush=True)
        screens = runner.screens
    pricing = model_pricing(model)
    tokens_in = sum(c["prompt_tokens"] for s in result["scenarios"] for c in s["calls"])
    tokens_out = sum(c["completion_tokens"] for s in result["scenarios"] for c in s["calls"])
    result["tokens"] = {"prompt": tokens_in, "completion": tokens_out}
    result["pricing_usd_per_token"] = pricing
    if pricing:
        result["cost_usd"] = round(tokens_in * pricing[0] + tokens_out * pricing[1], 5)
    json_path = out_dir / f"live_run_{stamp}.json"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    txt_path = out_dir / f"live_run_{stamp}.txt"
    with open(txt_path, "w", encoding="utf-8") as f:
        for scenario, step, text in screens:
            f.write(f"\n===== {scenario} | {step} =====\n{text}\n")
    print(f"Замеры: {json_path}")
    print(f"Экраны: {txt_path}")
    print(f"Токены: {tokens_in} на входе, {tokens_out} на выходе"
          + (f"; стоимость около {result['cost_usd']} USD" if pricing else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
