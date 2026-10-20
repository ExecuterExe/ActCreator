// Проверка логики Code.gs без Google: имитация SpreadsheetApp в памяти. Запуск: node apps_script/check_code.js
// (Apps Script — обычный JavaScript; настоящую таблицу проверяет кнопка «Проверить таблицу» на /admin.)
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

function makeSheet(name) {
  const data = [];
  const sheet = {
    name, data,
    getLastRow: () => data.length,
    getLastColumn: () => Math.max(0, ...data.map(r => r.length)),
    setFrozenRows: () => {},
    getRange(row, col, rows, cols) {
      return {
        getValues: () => Array.from({ length: rows }, (_, i) =>
          Array.from({ length: cols }, (_, j) => ((data[row - 1 + i] || [])[col - 1 + j] ?? ""))),
        setValues(values) {
          values.forEach((v, i) => { data[row - 1 + i] = data[row - 1 + i] || []; v.forEach((x, j) => { data[row - 1 + i][col - 1 + j] = x; }); });
        },
        createTextFinder(text) {
          let exact = false;
          return {
            matchEntireCell(b) { exact = b; return this; },
            findNext() {
              for (let i = 0; i < rows; i++) {
                const value = String((data[row - 1 + i] || [])[col - 1] ?? "");
                if (exact ? value === text : value.includes(text)) return { getRow: () => row + i };
              }
              return null;
            },
          };
        },
      };
    },
  };
  return sheet;
}

const sheets = {};
const context = {
  SpreadsheetApp: { getActiveSpreadsheet: () => ({
    getName: () => "Тест",
    getSheetByName: n => sheets[n] || null,
    insertSheet: n => (sheets[n] = makeSheet(n)),
  }) },
  LockService: { getScriptLock: () => ({ waitLock() {}, releaseLock() {} }) },
  ContentService: { MimeType: { JSON: "json" }, createTextOutput: s => ({ setMimeType() { return this; }, s }) },
};
vm.createContext(context);
const code = fs.readFileSync(path.join(__dirname, "Code.gs"), "utf8");
vm.runInContext(code, context);
const post = obj => JSON.parse(vm.runInContext("doPost", context)({ postData: { contents: JSON.stringify(obj) } }).s);

// открытие URL в браузере: «скрипт на месте», без имени таблицы и данных
const got = JSON.parse(vm.runInContext("doGet", context)({}).s);
assert.strictEqual(got.ok, true);
assert.strictEqual(JSON.stringify(got).includes("Тест"), false, "doGet не раскрывает таблицу");

// пароль-заглушка не пускает никого, даже с «правильным» токеном
assert.strictEqual(post({ token: "ЗАМЕНИТЕ-НА-ДЛИННЫЙ-СЛУЧАЙНЫЙ-ПАРОЛЬ", action: "ping" }).ok, false);
const patched = code.replace("const TOKEN = 'ЗАМЕНИТЕ-НА-ДЛИННЫЙ-СЛУЧАЙНЫЙ-ПАРОЛЬ';", "const TOKEN = 'секрет';");
const ctx2 = vm.createContext({ ...context });
vm.runInContext(patched, ctx2);
const post2 = obj => JSON.parse(vm.runInContext("doPost", ctx2)({ postData: { contents: JSON.stringify(obj) } }).s);

assert.strictEqual(post2({ token: "чужой", action: "ping" }).error, "неверный токен");
assert.deepStrictEqual(post2({ token: "секрет", action: "ping" }), { ok: true, sheet: "Тест" });
const headers = { "Тренировки": ["record_id", "db", "state", "note"] };
let r = post2({ token: "секрет", action: "upsert", headers, rows: [
  { tab: "Тренировки", row: ["db1:session:1", "db1", "first", "=HYPERLINK(\"x\")"] },
  { tab: "Тренировки", row: ["db1:session:2", "db1", "mirror", null] },
] });
assert.strictEqual(r.ok, true);
assert.deepStrictEqual(r.results.map(x => x.row), [2, 3]);
r = post2({ token: "секрет", action: "upsert", headers, rows: [{ tab: "Тренировки", row: ["db1:session:1", "db1", "done", "ok"] }] });
const t = sheets["Тренировки"].data;
assert.deepStrictEqual(t[0], headers["Тренировки"]);
assert.strictEqual(t.length, 3, "строка той же сессии обновлена, а не добавлена");
assert.deepStrictEqual(t[1], ["db1:session:1", "db1", "done", "ok"]);
assert.strictEqual(t[2][3], "", "null → пустая ячейка");
post2({ token: "секрет", action: "upsert", headers, rows: [{ tab: "Тренировки", row: ["db1:session:3", "db1", "x", "=1+1"] }] });
assert.strictEqual(t[3][3], "'=1+1", "текст, похожий на формулу, записан как текст");
console.log("Code.gs: все проверки прошли");
