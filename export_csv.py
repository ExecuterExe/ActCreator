"""Выгрузка данных для исследователей.

Запуск:  .venv\\Scripts\\python export_csv.py
Создаёт в exports/ (UTF-8 с BOM, открывается в Excel):
- attempts_<дата>.csv — входной тест;
- sessions_<дата>.csv — тренировки (одна строка — одна сессия);
- checkpoints_<дата>.csv — контрольные точки (индекс первой мысли).
Печатает сводку: доля завершивших тест, предварительный сигнал по H5, сырые доли по тренировкам.
Логины и тексты идей тренировок в выгрузку не попадают — только номер участника и коды.
"""

import csv
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).parent
DEFAULT_DB = BASE / "instance" / "actkreator.db"


def h5_signal(case):
    """Предварительный сигнал по H5 из анкеты: недавний конкретный случай + попытка решения.

    Это НЕ проверка H5 — исходная метрика предполагает разговор с человеком.
    """
    if not case:
        return False
    recent = case.get("last_when") in {"week", "month", "quarter"}
    concrete = len((case.get("last_task") or "").strip()) >= 15
    tried = bool(set(case.get("last_tried") or []) - {"nothing"})
    return recent and concrete and tried


def main(db_path=DEFAULT_DB):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    n_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    rows = conn.execute("SELECT * FROM attempts ORDER BY id").fetchall()

    out_dir = BASE / "exports"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / f"attempts_{datetime.now():%Y%m%d_%H%M}.csv"

    fields = [
        "participant", "step", "started_at", "completed_at", "task_seconds",
        "role", "field", "freq", "goal",
        "last_when", "last_task", "last_tried", "last_result", "last_cost",
        "se_ideas", "se_confidence", "se_strategy",
        "n_ideas", "n_families", "n_unknown", "common_count", "first_uncommon_pos",
        "ideas", "families", "top2",
        "h5_signal", "useful", "surprise", "comment",
    ]
    completed = [r for r in rows if r["completed_at"]]
    h5_yes = 0
    target_completed = 0
    goals = {}
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for r in rows:
            answers = json.loads(r["answers_json"])
            about = answers.get("about", {})
            case = answers.get("case", {})
            se = answers.get("self", {})
            ideas = json.loads(r["ideas_json"]) if r["ideas_json"] else []
            result = json.loads(r["result_json"]) if r["result_json"] else {}
            feedback = json.loads(r["feedback_json"]) if r["feedback_json"] else {}
            signal = h5_signal(case)
            if r["completed_at"]:
                if set(about.get("role") or []) & {"freelance", "pet"}:
                    target_completed += 1
                    h5_yes += signal
                goals[about.get("goal")] = goals.get(about.get("goal"), 0) + 1
            writer.writerow({
                "participant": r["user_id"],
                "step": r["step"],
                "started_at": r["started_at"],
                "completed_at": r["completed_at"],
                "task_seconds": r["task_seconds"],
                "role": ";".join(about.get("role") or []),
                "field": about.get("field"),
                "freq": about.get("freq"),
                "goal": about.get("goal"),
                "last_when": case.get("last_when"),
                "last_task": case.get("last_task"),
                "last_tried": ";".join(case.get("last_tried") or []),
                "last_result": case.get("last_result"),
                "last_cost": case.get("last_cost"),
                "se_ideas": se.get("se_ideas"),
                "se_confidence": se.get("se_confidence"),
                "se_strategy": se.get("se_strategy"),
                "n_ideas": result.get("n_ideas"),
                "n_families": result.get("n_families"),
                "n_unknown": result.get("n_unknown"),
                "common_count": result.get("common_count"),
                "first_uncommon_pos": result.get("first_uncommon_pos"),
                "ideas": " | ".join(i["text"] for i in ideas),
                "families": " | ".join(i["family"] or "?" for i in ideas),
                "top2": json.loads(r["top2_json"]) if r["top2_json"] else "",
                "h5_signal": int(signal),
                "useful": feedback.get("useful"),
                "surprise": ";".join(feedback.get("surprise") or []),
                "comment": feedback.get("comment"),
            })

    print(f"Файл: {out_path}")
    print(f"Зарегистрировались: {n_users}")
    print(f"Начали тест: {len(rows)}")
    print(f"Завершили тест: {len(completed)}"
          + (f" ({len(completed) / n_users:.0%} от зарегистрированных)" if n_users else ""))
    if target_completed:
        print(f"Фрилансеры/пет-проекты среди завершивших: {target_completed}; "
              f"предварительный сигнал H5 (недавний конкретный случай + попытка решения): "
              f"{h5_yes / target_completed:.0%}  [порог H5 — 60%, но анкета не заменяет интервью]")
    if goals:
        print("Что ближе (goal):", ", ".join(f"{k}: {v}" for k, v in goals.items()))
    export_training(conn, out_dir)


def _j(value, default=None):
    return json.loads(value) if value else default


def export_training(conn, out_dir):
    """Тренировки и контрольные точки: одна строка — одна сессия / одна точка (A4)."""
    stamp = f"{datetime.now():%Y%m%d_%H%M}"
    sessions = conn.execute("SELECT * FROM sessions ORDER BY id").fetchall()
    fields = [
        "participant", "session", "mode", "state", "entry_source", "level", "methodology_version",
        "started_at", "completed_at", "diagnosis", "self_diagnosis", "self_match", "tool_recommended", "tool",
        "tool_changed", "strength", "softened", "hints", "wants_generation", "n_first", "first_families",
        "favorite_family", "final_family", "final_class_source", "shift", "rare", "flexibility", "authorship",
        "rubric_source", "rb1", "rb2", "rb3", "rb4", "taken", "difficulty_feedback", "retry_used",
        "premortem_skipped", "user_reasons", "critic_objections", "about_me", "verdicts", "strongest_category",
        "helped", "has_plan", "check_due", "outcome_d2", "outcome_d7", "outcome_d30",
        # методология v0.5 (C1): момент открытия
        "lens", "shift_kind", "shift_source", "novelty", "feasibility", "versions", "improve_used",
        "ladder_used", "no_idea", "wow", "incubation",
    ]
    stats = {"done": 0, "shift": 0, "plan": 0, "wants": 0, "about": [0, 0], "helped_bad": 0, "helped": 0,
             "d2_done": 0, "d2_due": 0, "wow_n": 0, "wow_new": 0, "mech": 0}
    path = out_dir / f"sessions_{stamp}.csv"
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for s in sessions:
            sid = s["id"]
            diag, tool = _j(s["diagnosis_json"], {}), _j(s["tool_json"], {})
            ev, flags, route = _j(s["eval_json"], {}), _j(s["flags_json"], {}), _j(s["route_json"], {})
            first = conn.execute("SELECT * FROM ideas WHERE session_id = ? AND stage = 'first' ORDER BY ord",
                                 (sid,)).fetchall()
            final = conn.execute("SELECT * FROM ideas WHERE session_id = ? AND stage = 'final'", (sid,)).fetchone()
            objections = conn.execute("SELECT * FROM objections WHERE session_id = ? ORDER BY id", (sid,)).fetchall()
            critic = [o for o in objections if o["author"] == "critic"]
            strongest = next((o for o in objections if o["id"] == flags.get("strongest")), None)
            exp = conn.execute("SELECT * FROM experiments WHERE session_id = ?", (sid,)).fetchone()
            outs = {o["day"]: o["status"] for o in conn.execute("SELECT * FROM outcomes WHERE session_id = ?", (sid,))}
            hints = conn.execute("SELECT COUNT(*) FROM hints WHERE session_id = ?", (sid,)).fetchone()[0]
            rubric = ev.get("rubric") or {}
            fav = next((i for i in first if i["is_favorite"]), None)
            if s["state"] == "done":
                stats["done"] += 1
                stats["shift"] += bool(ev.get("shift"))
                stats["plan"] += bool(exp)
                if route.get("wow"):
                    stats["wow_n"] += 1
                    stats["wow_new"] += route["wow"] in ("Нет", "Скорее нет")
                stats["mech"] += ev.get("shift_kind") == "механизм"
                if route.get("helped"):
                    stats["helped"] += 1
                    stats["helped_bad"] += route["helped"] in ("nothing", "knew")
            stats["wants"] += bool(flags.get("wants_generation"))
            for o in critic:
                if o["about_me"] is not None:
                    stats["about"][0] += o["about_me"] == 1
                    stats["about"][1] += 1
            if exp:
                stats["d2_due"] += 1
                stats["d2_done"] += outs.get(2, "").startswith("Сделал") or outs.get(2) == "Частично"
            w.writerow({
                "participant": s["user_id"], "session": sid, "mode": s["mode"], "state": s["state"],
                "entry_source": s["entry_source"], "level": s["level"],
                "methodology_version": s["methodology_version"], "started_at": s["started_at"],
                "completed_at": s["completed_at"], "diagnosis": diag.get("code"),
                "self_diagnosis": diag.get("self_diag"), "self_match": diag.get("match"),
                "tool_recommended": diag.get("tool_type"), "tool": tool.get("type"),
                "tool_changed": int(bool(flags.get("tool_changed"))), "strength": tool.get("strength"),
                "softened": int(bool(flags.get("softened"))), "hints": hints,
                "wants_generation": flags.get("wants_generation", 0), "n_first": len(first),
                "first_families": " | ".join(i["family"] or "?" for i in first),
                "favorite_family": fav["family"] if fav else None,
                "final_family": final["family"] if final else None,
                "final_class_source": final["class_source"] if final else None,
                "shift": ev.get("shift"), "rare": ev.get("rare"), "flexibility": ev.get("flexibility"),
                "authorship": ev.get("authorship"), "rubric_source": ev.get("rubric_source"),
                "rb1": (rubric.get("RB1") or {}).get("level"), "rb2": (rubric.get("RB2") or {}).get("level"),
                "rb3": (rubric.get("RB3") or {}).get("level"), "rb4": (rubric.get("RB4") or {}).get("level"),
                "taken": int(bool(ev.get("taken"))), "difficulty_feedback": ev.get("feedback"),
                "retry_used": int(bool(flags.get("retry_used"))),
                "premortem_skipped": int(bool(flags.get("premortem_skipped"))),
                "user_reasons": sum(1 for o in objections if o["author"] == "user"),
                "critic_objections": len(critic),
                "about_me": sum(1 for o in critic if o["about_me"] == 1),
                "verdicts": " | ".join(o["verdict"] or "-" for o in critic),
                "strongest_category": strongest["category"] if strongest else None,
                "helped": route.get("helped"), "has_plan": int(bool(exp)),
                "check_due": exp["due"] if exp else None,
                "outcome_d2": outs.get(2), "outcome_d7": outs.get(7), "outcome_d30": outs.get(30),
                "lens": (flags.get("barrier") or {}).get("lens"), "shift_kind": ev.get("shift_kind"),
                "shift_source": ev.get("shift_source"), "novelty": ev.get("novelty"),
                "feasibility": ev.get("feasibility"), "versions": ev.get("versions"),
                "improve_used": int(bool(flags.get("improve_used"))),
                "ladder_used": int(bool(flags.get("ladder_done") or flags.get("no_idea")
                                        or conn.execute("SELECT COUNT(*) FROM events WHERE name = 'ladder_step' "
                                                        "AND props_json LIKE ?", (f'%"session": {sid},%',)
                                                        ).fetchone()[0])),
                "no_idea": int(bool(flags.get("no_idea"))), "wow": route.get("wow"),
                "incubation": int(bool(conn.execute("SELECT COUNT(*) FROM incubations WHERE session_id = ? "
                                                    "AND answered_at IS NOT NULL", (sid,)).fetchone()[0])),
            })
    cp_path = out_dir / f"checkpoints_{stamp}.csv"
    with open(cp_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["participant", "checkpoint", "task", "started_at", "completed_at",
                                          "n_ideas", "flexibility", "ipm_rare_families", "first_uncommon_pos",
                                          "n_unknown", "top2_rare", "common_source", "late", "repeat"])
        w.writeheader()
        for c in conn.execute("SELECT * FROM checkpoints ORDER BY id"):
            r = _j(c["result_json"], {})
            m = r.get("metrics", {})
            w.writerow({"participant": c["user_id"], "checkpoint": c["id"], "task": c["task_id"],
                        "started_at": c["started_at"], "completed_at": c["completed_at"],
                        "n_ideas": m.get("n_ideas"), "flexibility": m.get("flexibility"),
                        "ipm_rare_families": m.get("rare_families"), "first_uncommon_pos": m.get("first_uncommon_pos"),
                        "n_unknown": m.get("n_unknown"), "top2_rare": r.get("top2_rare"),
                        "common_source": r.get("common_source"), "late": r.get("late"), "repeat": r.get("repeat")})
    print(f"Файл: {path}")
    print(f"Файл: {cp_path}")
    print(f"Тренировок начато: {len(sessions)}, завершено: {stats['done']}")
    if stats["done"]:
        print(f"Доля сессий со сдвигом: {stats['shift'] / stats['done']:.0%}")
        print(f"Доля сессий с планом проверки: {stats['plan'] / stats['done']:.0%}")
    if stats["helped"]:
        print(f"«Ничего не помогло» или «я и так знал(а)»: {stats['helped_bad'] / stats['helped']:.0%} "
              f"[сигнал тревоги — стабильно выше 30%, B4 §4.8]")
    if stats["about"][1]:
        print(f"Доля возражений «про меня»: {stats['about'][0] / stats['about'][1]:.0%} [предложенный порог Д4 — 70%]")
    if stats["d2_due"]:
        print(f"Проверки, отмеченные как сделанные (ответ дня 2): {stats['d2_done']} из {stats['d2_due']} планов "
              f"[предложенный порог Д3 — 30% за 7 дней]")
    print(f"Сессий с запросом «просто дай ответ»: {stats['wants']}")
    if stats["done"]:
        print(f"Сдвиг механизма (детектор ложного сдвига): {stats['mech'] / stats['done']:.0%}")
    if stats["wow_n"]:
        print(f"«Без тренировки не пришла бы» (Нет / Скорее нет): {stats['wow_new'] / stats['wow_n']:.0%} "
              f"из {stats['wow_n']} ответов — главный показатель момента открытия (C1)")
    print("Это сырые доли по базе, без поправок на размер выборки; выводы — по плану анализа A4.")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DB)
