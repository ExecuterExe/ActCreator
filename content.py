"""Вопросы входного теста.

Значения вариантов (первый элемент пары) — стабильные коды для выгрузки,
подписи можно править без потери данных.
Правила составления: спрашиваем о прошлом поведении и конкретном случае,
а не о мнениях и гипотетическом будущем (подход The Mom Test); самооценку
собираем до задания, чтобы результат задания на неё не влиял.
"""

STEPS = ["about", "case", "self", "task", "pick", "done"]

LIKERT = [
    ("1", "Совсем не про меня"),
    ("2", "Скорее нет"),
    ("3", "Отчасти"),
    ("4", "Скорее да"),
    ("5", "Точно про меня"),
]

SURVEY = {
    "about": {
        "title": "Шаг 1 из 5. О вас",
        "intro": "Пара вопросов, чтобы понимать, в какой ситуации вы находитесь. Около минуты.",
        "questions": [
            {
                "id": "role",
                "type": "multi",
                "label": "Кто вы сейчас? Можно выбрать несколько.",
                "required": True,
                "options": [
                    ("freelance", "Фрилансер"),
                    ("pet", "Делаю пет-проект или свой продукт"),
                    ("employee", "Работаю в компании по найму"),
                    ("owner", "Свой бизнес"),
                    ("student", "Учусь"),
                    ("other", "Другое"),
                ],
            },
            {
                "id": "field",
                "type": "single",
                "label": "Чем вы в основном занимаетесь?",
                "required": True,
                "options": [
                    ("marketing", "Маркетинг и продвижение"),
                    ("design", "Дизайн"),
                    ("dev", "Разработка"),
                    ("product", "Продукт и аналитика"),
                    ("content", "Контент и тексты"),
                    ("sales", "Продажи и работа с клиентами"),
                    ("education", "Образование"),
                    ("other", "Другое"),
                ],
            },
            {
                "id": "freq",
                "type": "single",
                "label": "Как часто в работе или проектах вам нужно придумать новый подход к задаче?",
                "required": True,
                "options": [
                    ("weekly_plus", "Чаще раза в неделю"),
                    ("monthly", "Раз в 1–4 недели"),
                    ("rarely", "Раз в несколько месяцев"),
                    ("never", "Почти никогда"),
                ],
            },
            {
                "id": "goal",
                "type": "single",
                "label": "Что вам ближе прямо сейчас?",
                "required": True,
                "options": [
                    ("solve_now", "Есть конкретная задача — нужна идея"),
                    ("train", "Хочу научиться сам(а) придумывать нестандартные решения"),
                    ("curious", "Просто интересно"),
                ],
            },
        ],
    },
    "case": {
        "title": "Шаг 2 из 5. Последний случай",
        "intro": "Вспомните последний раз, когда вам была нужна нестандартная идея для работы или проекта. Нас интересует реальный случай, а не «как обычно бывает».",
        "questions": [
            {
                "id": "last_when",
                "type": "single",
                "label": "Когда это было?",
                "required": True,
                "options": [
                    ("week", "На этой неделе"),
                    ("month", "В этом месяце"),
                    ("quarter", "1–3 месяца назад"),
                    ("older", "Больше 3 месяцев назад"),
                    ("none", "Не могу вспомнить такого случая"),
                ],
            },
            {
                "id": "last_task",
                "type": "text",
                "label": "Что нужно было придумать? Опишите задачу в 1–2 предложениях.",
                "placeholder": "Например: как получить первых клиентов для нового сервиса без бюджета",
                "required": True,
                "required_unless": ("last_when", "none"),
                "min_len": 10,
            },
            {
                "id": "last_tried",
                "type": "multi",
                "label": "Что вы делали, чтобы найти идею? Можно выбрать несколько.",
                "required": True,
                "required_unless": ("last_when", "none"),
                "options": [
                    ("self", "Думал(а) сам(а)"),
                    ("ai", "Спросил(а) ChatGPT или другой ИИ"),
                    ("people", "Обсуждал(а) с коллегами или друзьями"),
                    ("search", "Искал(а) примеры в интернете"),
                    ("method", "Использовал(а) методику: мозговой штурм, ТРИЗ и т. п."),
                    ("nothing", "Ничего особенного — сделал(а) как обычно"),
                ],
            },
            {
                "id": "last_result",
                "type": "single",
                "label": "Чем закончилось?",
                "required": True,
                "required_unless": ("last_when", "none"),
                "options": [
                    ("good", "Нашёл(а) идею, которой доволен(льна)"),
                    ("ordinary", "Нашёл(а), но идея оказалась обычной"),
                    ("not_found", "Так и не нашёл(а)"),
                    ("in_progress", "Ещё в процессе"),
                ],
            },
            {
                "id": "last_cost",
                "type": "single",
                "label": "Сколько примерно времени ушло на поиск идеи?",
                "required": False,
                "options": [
                    ("lt_hour", "Меньше часа"),
                    ("hours", "1–3 часа"),
                    ("day", "Полдня — день"),
                    ("days", "Несколько дней и больше"),
                ],
            },
        ],
    },
    "self": {
        "title": "Шаг 3 из 5. Как вы себя оцениваете",
        "intro": "Три коротких утверждения. Отвечайте как есть — здесь нет правильных ответов. Это точка отсчёта, к которой можно будет вернуться позже.",
        "questions": [
            {
                "id": "se_ideas",
                "type": "likert",
                "label": "Я хорошо придумываю новые идеи.",
                "required": True,
                "options": LIKERT,
            },
            {
                "id": "se_confidence",
                "type": "likert",
                "label": "Я уверен(а), что могу нестандартно решить рабочую задачу.",
                "required": True,
                "options": LIKERT,
            },
            {
                "id": "se_strategy",
                "type": "likert",
                "label": "Когда я застреваю на задаче, я знаю, что сделать, чтобы найти новый подход.",
                "required": True,
                "options": LIKERT,
            },
        ],
    },
}

FEEDBACK_SURPRISE = [
    ("families", "Что мои идеи укладываются в несколько похожих подходов"),
    ("obvious", "Сколько моих идей оказались типичными"),
    ("order", "Что первые идеи — самые очевидные"),
    ("untouched", "Направления, о которых я не подумал(а)"),
    ("top2", "Сравнение моего выбора с оценкой"),
    ("nothing", "Ничего не удивило"),
]


def validate(step, form):
    """Возвращает (ответы, ошибки) для шага анкеты."""
    answers, errors = {}, {}
    questions = SURVEY[step]["questions"]
    for q in questions:
        allowed = {value for value, _ in q.get("options", [])}
        if q["type"] == "multi":
            value = [v for v in form.getlist(q["id"]) if v in allowed]
        elif q["type"] == "text":
            value = (form.get(q["id"]) or "").strip()[:1000]
        else:
            value = form.get(q["id"])
            value = value if value in allowed else None
        answers[q["id"]] = value

    for q in questions:
        required = q.get("required", False)
        unless = q.get("required_unless")
        if unless and answers.get(unless[0]) == unless[1]:
            required = False
        value = answers[q["id"]]
        if required and not value:
            errors[q["id"]] = "Ответьте, пожалуйста, на этот вопрос."
        elif q["type"] == "text" and required and len(value) < q.get("min_len", 0):
            errors[q["id"]] = "Опишите чуть подробнее — хотя бы одно предложение."
    return answers, errors
