"""Подключение к языковой модели через OpenRouter.

ИИ в сервисе — исполнитель узких ролей (A5). Каждая роль получает и возвращает
JSON. Если ключа нет, сеть недоступна или ответ не прошёл проверку, роль
возвращает None, и вызывающий код использует запасной вариант на библиотеках.
"""

import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

SETTINGS_PATH = Path(__file__).resolve().parent / "settings.env"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "deepseek/deepseek-chat"
# Самый долгий удачный вызов в живых прогонах 11–12.10 — 23 с (из 300+). Было 60 с на попытку, 2 попытки в клиенте и
# повтор в ask_json: при зависшем провайдере шаг ждал до 4 минут, а gunicorn рвёт запрос через 120 с — человек видел
# ошибку вместо запасного варианта (docs/llm-quality/AUDIT.md, задержки).
DEFAULT_TIMEOUT = 35
RETRY_BUDGET = 25  # секунд: если первая попытка роли уже заняла больше, повтора нет — сразу запасной вариант
SETTING_KEYS = ("OPENROUTER_API_KEY", "OPENROUTER_MODEL", "PILOT_FULL_ACCESS")


class LLMError(Exception):
    pass


def load_settings(path=SETTINGS_PATH):
    """Читает settings.env (ключ=значение). Переменные окружения важнее файла."""
    values = {}
    path = Path(path)
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    for key in SETTING_KEYS:
        if os.environ.get(key):
            values[key] = os.environ[key]
    return values


class OpenRouterClient:
    available = True

    def __init__(self, api_key, model=DEFAULT_MODEL, timeout=DEFAULT_TIMEOUT, opener=None):
        self.api_key = api_key
        self.model = model or DEFAULT_MODEL
        self.timeout = timeout
        self._open = opener or urllib.request.urlopen
        self.last_usage = {}  # токены последнего ответа (для замеров стоимости)
        # кто на самом деле ответил: OpenRouter раздаёт одну модель разным провайдерам, а ответы у них
        # различаются — для воспроизводимости eval-прогонов это нужно знать (docs/llm-quality/EVAL_PLAN.md)
        self.last_meta = {}

    def complete(self, system, user, max_tokens=600, temperature=0.3):
        return self.chat([{"role": "system", "content": system}, {"role": "user", "content": user}],
                         max_tokens=max_tokens, temperature=temperature)

    def chat(self, messages, max_tokens=600, temperature=0.3):
        """Диалог целиком: [{role: system|user|assistant, content}]. Нужен руке «обычный чат-бот» эксперимента
        (lab.py) — роли сервиса ходят через complete()."""
        payload = {"model": self.model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature}
        request = urllib.request.Request(
            OPENROUTER_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "X-Title": "AktKreator",
            },
            method="POST",
        )
        last_error = None
        for attempt in range(2):
            self.last_meta = {"attempts": attempt + 1}
            started = time.monotonic()
            try:
                with self._open(request, timeout=self.timeout) as response:
                    data = json.loads(response.read().decode("utf-8"))
                self.last_usage = data.get("usage") or {}
                self.last_meta.update(provider=data.get("provider"), model=data.get("model"))
                content = data["choices"][0]["message"]["content"]
                if not content or not content.strip():
                    raise LLMError("пустой ответ модели")
                return content
            except urllib.error.HTTPError as exc:
                last_error = LLMError(f"HTTP {exc.code}")
                if exc.code in (401, 402, 403):  # ключ, оплата, доступ — повтор не поможет
                    break
                if exc.code == 429:  # слишком много запросов сразу (параллельные вызовы) — подождать дольше
                    time.sleep(2.5)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = LLMError(f"сеть: {type(exc).__name__}")
                if time.monotonic() - started > self.timeout / 2:
                    break  # провайдер завис: вторая такая же попытка удвоит ожидание — лучше запасной вариант
            except (KeyError, IndexError, ValueError) as exc:
                last_error = LLMError(f"неожиданный формат ответа: {type(exc).__name__}")
                break
            time.sleep(1)
        raise last_error


class NullClient:
    """Ключа нет — ИИ выключен, работают запасные варианты."""

    available = False

    def complete(self, *args, **kwargs):
        raise LLMError("ИИ не настроен")

    def chat(self, *args, **kwargs):
        raise LLMError("ИИ не настроен")


class FakeClient:
    """Для тестов: ответ возвращает функция responder(system, user)."""

    available = True

    def __init__(self, responder):
        self.responder = responder
        self.calls = []

    def complete(self, system, user, max_tokens=600, temperature=0.3):
        self.calls.append((system, user))
        result = self.responder(system, user)
        if isinstance(result, Exception):
            raise result
        return result

    def chat(self, messages, max_tokens=600, temperature=0.3):
        """Диалог: responder получает системное сообщение и последнюю реплику человека."""
        system = next((m["content"] for m in messages if m["role"] == "system"), "")
        user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        return self.complete(system, user, max_tokens=max_tokens, temperature=temperature)


def make_client(settings):
    key = (settings.get("OPENROUTER_API_KEY") or "").strip()
    if not key:
        return NullClient()
    return OpenRouterClient(key, settings.get("OPENROUTER_MODEL") or DEFAULT_MODEL)


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def extract_json(text):
    """Достаёт JSON из ответа модели: с обёрткой ```json или без, с текстом вокруг."""
    if text is None:
        raise ValueError("нет текста")
    fenced = _FENCE.search(text)
    if fenced:
        text = fenced.group(1)
    text = text.strip()
    starts = [i for i in (text.find("{"), text.find("[")) if i != -1]
    if not starts:
        raise ValueError("JSON не найден")
    start = min(starts)
    opener = text[start]
    closer = "}" if opener == "{" else "]"
    depth, in_str, escape = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return json.loads(text[start : i + 1])
    raise ValueError("JSON не закрыт")


class AI:
    """Фасад для ролей: вызов модели, разбор JSON, проверка, повтор, учёт ошибок."""

    def __init__(self, client, on_event=None):
        self.client = client
        self.on_event = on_event or (lambda name, props: None)

    @property
    def available(self):
        return bool(getattr(self.client, "available", False))

    def ask_json(self, role, system, user, validate, max_tokens=600, temperature=0.2, retries=1):
        """validate(data) возвращает очищенные данные или бросает ValueError."""
        if not self.available:
            return None
        last, start = None, time.monotonic()
        for attempt in range(retries + 1):
            if attempt and time.monotonic() - start > RETRY_BUDGET:
                break  # долгий сбой не повторяем: человек и так ждал, дальше — запасной вариант без ИИ
            try:
                text = self.client.complete(system, user, max_tokens=max_tokens, temperature=temperature)
                return validate(extract_json(text))
            except (LLMError, ValueError, TypeError, KeyError, AttributeError, IndexError) as exc:
                last = exc  # ответ неожиданной формы — то же, что ошибка: повтор, затем запасной путь без ИИ
        self.on_event("ai_error", {"role": role, "error": type(last).__name__, "detail": str(last)[:120]})
        return None

    def ask_text(self, role, system, user, max_tokens=200, temperature=0.5):
        if not self.available:
            return None
        try:
            text = self.client.complete(system, user, max_tokens=max_tokens, temperature=temperature)
            return text.strip()
        except LLMError as exc:
            self.on_event("ai_error", {"role": role, "error": "LLMError", "detail": str(exc)[:120]})
            return None
