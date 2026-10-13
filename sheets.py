"""Google Sheets — хранилище данных исследования, когда диск сервера временный (бесплатный Render).

Доступ — сервисный аккаунт Google. Ключ JSON (целиком или в base64) — в переменной GOOGLE_SERVICE_ACCOUNT_JSON,
номер таблицы — в GOOGLE_SHEETS_ID. Ключ живёт только в переменных окружения сервера: ни в репозитории,
ни в браузере, ни в журналах. Таблицу нужно открыть сервисному аккаунту на редактирование (его адрес —
на странице /admin). Запросы — стандартной библиотекой; google-auth только подписывает вход.
"""

import base64
import json
import urllib.error
import urllib.parse
import urllib.request

from google.auth.transport import Request as AuthRequest
from google.auth.transport import Response as AuthResponse
from google.oauth2 import service_account

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
API = "https://sheets.googleapis.com/v4/spreadsheets"


class SheetsError(Exception):
    pass


class _Response(AuthResponse):
    def __init__(self, status, headers, data):
        self._status, self._headers, self._data = status, headers, data

    @property
    def status(self):
        return self._status

    @property
    def headers(self):
        return self._headers

    @property
    def data(self):
        return self._data


class _UrllibRequest(AuthRequest):
    """Транспорт google-auth поверх urllib — чтобы не тянуть requests ради одного запроса токена."""

    def __call__(self, url, method="GET", body=None, headers=None, timeout=None, **kwargs):
        req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout or 20) as r:
                return _Response(r.status, dict(r.headers), r.read())
        except urllib.error.HTTPError as e:
            return _Response(e.code, dict(e.headers), e.read())


def load_key(raw):
    """Ключ сервисного аккаунта: JSON как есть или в base64 (так проще вставить в одну строку переменной)."""
    raw = (raw or "").strip()
    if not raw:
        return None
    if not raw.startswith("{"):
        raw = base64.b64decode(raw).decode("utf-8")
    return json.loads(raw)


def quote_range(tab, cells="A1"):
    return f"'{tab.replace(chr(39), chr(39) * 2)}'!{cells}"


def cell(value):
    """Значение для ячейки: RAW — строка «=…» остаётся текстом, а не формулой."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return value
    return str(value)


class AppsScriptClient:
    """Таблица через Apps Script — без Google Cloud: в таблице опубликован скрипт apps_script/Code.gs
    (веб-приложение), сайт шлёт ему строки пачкой с паролем. Скрипт сам находит строку по record_id
    и обновляет её на месте — одна сессия остаётся одной строкой даже после перезапуска сервера."""

    email = None
    spreadsheet_id = None
    mode = "apps_script"

    def __init__(self, url, token, timeout=60):
        self.url, self.token, self.timeout = url, token, timeout

    def _post(self, payload):
        data = json.dumps(dict(payload, token=self.token), ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.url, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            # веб-приложение отвечает переадресацией на googleusercontent — urllib её проходит сам
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                body = r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            raise SheetsError(f"Apps Script ответил {e.code}") from None
        except (urllib.error.URLError, TimeoutError) as e:
            raise SheetsError(f"нет связи с Apps Script: {e}") from None
        try:
            answer = json.loads(body)
        except ValueError:  # чаще всего — доступ к веб-приложению не «Все» и Google вернул страницу входа
            raise SheetsError("Apps Script вернул не JSON — проверьте, что доступ к веб-приложению «Все»") from None
        if not answer.get("ok"):
            raise SheetsError(f"Apps Script: {answer.get('error', 'неизвестная ошибка')}")
        return answer

    def ping(self):
        return self._post({"action": "ping"}).get("sheet")

    def upsert_many(self, headers, items):
        """items — [(tab, row)], row[0] — record_id. Возвращает номера строк в таблице."""
        tabs = {tab for tab, _ in items}
        answer = self._post({"action": "upsert", "headers": {t: headers[t] for t in tabs},
                             "rows": [{"tab": tab, "row": [cell(v) for v in row]} for tab, row in items]})
        return answer.get("results", [])


class SheetsClient:
    mode = "service_account"

    def __init__(self, spreadsheet_id, key_info, timeout=15):
        self.spreadsheet_id = spreadsheet_id
        self.timeout = timeout
        self.creds = service_account.Credentials.from_service_account_info(key_info, scopes=SCOPES)
        self._tabs = None

    @property
    def email(self):
        return self.creds.service_account_email

    def _call(self, method, path, params=None, body=None):
        if not self.creds.valid:
            self.creds.refresh(_UrllibRequest())
        url = f"{API}/{self.spreadsheet_id}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.creds.token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            try:
                message = json.loads(e.read().decode("utf-8")).get("error", {}).get("message", "")
            except (ValueError, AttributeError):
                message = ""
            raise SheetsError(f"Google Sheets ответил {e.code}: {message[:200]}") from None
        except (urllib.error.URLError, TimeoutError) as e:
            raise SheetsError(f"нет связи с Google Sheets: {e}") from None

    def ensure_tab(self, tab, header):
        """Вкладка с нужной шапкой: создаётся при первой записи, шапка дописывается при новых колонках."""
        if self._tabs is None:
            info = self._call("GET", "", {"fields": "sheets.properties.title"})
            self._tabs = {s["properties"]["title"] for s in info.get("sheets", [])}
        if tab not in self._tabs:
            self._call("POST", ":batchUpdate", body={"requests": [{"addSheet": {"properties": {"title": tab}}}]})
            self._tabs.add(tab)
        rng = quote_range(tab, "1:1")
        current = self._call("GET", f"/values/{urllib.parse.quote(rng, safe='')}").get("values", [[]])
        if (current[0] if current else []) != header:
            self._call("PUT", f"/values/{urllib.parse.quote(quote_range(tab), safe='')}",
                       {"valueInputOption": "RAW"}, {"values": [header]})

    def append(self, tab, row):
        """Новая строка в конец вкладки; возвращает её диапазон — по нему строку потом обновляют на месте."""
        r = self._call("POST", f"/values/{urllib.parse.quote(quote_range(tab), safe='')}:append",
                       {"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
                       {"values": [[cell(v) for v in row]]})
        return r.get("updates", {}).get("updatedRange")

    def update(self, rng, row):
        self._call("PUT", f"/values/{urllib.parse.quote(rng, safe='')}", {"valueInputOption": "RAW"},
                   {"values": [[cell(v) for v in row]]})
        return rng

    def read(self, rng):
        return self._call("GET", f"/values/{urllib.parse.quote(rng, safe='')}").get("values", [])
