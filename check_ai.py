"""Проверка связи с DeepSeek через OpenRouter.

Запуск:  .venv\\Scripts\\python check_ai.py
"""

import sys

import llm


def main():
    settings = llm.load_settings()
    key = (settings.get("OPENROUTER_API_KEY") or "").strip()
    model = settings.get("OPENROUTER_MODEL") or llm.DEFAULT_MODEL
    if not key:
        print("Ключ не найден. Откройте файл site/settings.env и вставьте ключ после OPENROUTER_API_KEY=")
        return 1
    print(f"Модель: {model}. Ключ: ...{key[-4:]}. Отправляю тестовый запрос...")
    client = llm.make_client(settings)
    ai = llm.AI(client, on_event=lambda name, props: print("Ошибка:", props))
    result = ai.ask_json(
        "check",
        "Отвечай только JSON.",
        'Верни JSON {"ok": true, "word": "<одно русское слово>"}',
        validate=lambda d: d if isinstance(d, dict) and d.get("ok") is True else (_ for _ in ()).throw(ValueError("нет ok")),
        max_tokens=50,
    )
    if result:
        print("Связь работает. Ответ модели:", result)
        return 0
    print("Связь не работает. Частые причины: неверный ключ (HTTP 401), нет средств на счёте OpenRouter (HTTP 402), нет интернета.")
    return 2


if __name__ == "__main__":
    sys.exit(main())
