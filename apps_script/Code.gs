/**
 * АктКреватор → Google Таблица (данные исследования). Без Google Cloud и сервисных аккаунтов.
 *
 * Как подключить (подробно — README, «Данные исследования»):
 * 1. В таблице: Расширения → Apps Script. Удалить всё, что там есть, и вставить этот файл целиком.
 * 2. Заменить TOKEN ниже на длинный случайный пароль. Тот же пароль — в переменную APPS_SCRIPT_TOKEN на Render.
 * 3. Начать развертывание → Новое развертывание → тип «Веб-приложение»;
 *    «Выполнять от имени»: я; «У кого есть доступ»: все. Разрешить доступ к таблице.
 * 4. Скопировать URL веб-приложения (оканчивается на /exec) в переменную APPS_SCRIPT_URL на Render.
 *
 * Сайт присылает строки пачкой; каждая строка ищется по record_id (первая колонка) и обновляется на месте,
 * а если её ещё нет — добавляется в конец. Поэтому одна сессия — одна строка, сколько бы раз её ни присылали.
 */
const TOKEN = 'ЗАМЕНИТЕ-НА-ДЛИННЫЙ-СЛУЧАЙНЫЙ-ПАРОЛЬ';

function doPost(e) {
  let req;
  try {
    req = JSON.parse(e.postData.contents);
  } catch (err) {
    return reply({ ok: false, error: 'не JSON' });
  }
  if (!req || req.token !== TOKEN || TOKEN.indexOf('ЗАМЕНИТЕ') === 0) {
    return reply({ ok: false, error: 'неверный токен' });
  }
  const lock = LockService.getScriptLock();
  lock.waitLock(25000);
  try {
    const ss = SpreadsheetApp.getActiveSpreadsheet();
    if (req.action === 'ping') {
      return reply({ ok: true, sheet: ss.getName() });
    }
    const results = (req.rows || []).map(function (item) {
      return upsert(ss, item.tab, req.headers[item.tab], item.row);
    });
    return reply({ ok: true, results: results });
  } catch (err) {
    return reply({ ok: false, error: String(err) });
  } finally {
    lock.releaseLock();
  }
}

function upsert(ss, tab, header, row) {
  const sheet = ss.getSheetByName(tab) || ss.insertSheet(tab);
  const width = header.length;
  const top = sheet.getLastRow() >= 1 ? sheet.getRange(1, 1, 1, width).getValues()[0] : [];
  if (top.join('\u0001') !== header.join('\u0001')) {
    sheet.getRange(1, 1, 1, width).setValues([header]);
    sheet.setFrozenRows(1);
  }
  const id = String(row[0]);
  let index = 0;
  if (sheet.getLastRow() > 1) {
    const found = sheet.getRange(2, 1, sheet.getLastRow() - 1, 1)
      .createTextFinder(id).matchEntireCell(true).findNext();
    if (found) index = found.getRow();
  }
  if (!index) index = sheet.getLastRow() + 1;
  sheet.getRange(index, 1, 1, width).setValues([row.map(safe)]);
  return { tab: tab, row: index, id: id };
}

// Текст, похожий на формулу («=…», «+…», «-…», «@…»), записываем как текст — чужой ввод не исполняется.
function safe(value) {
  if (value === null || value === undefined) return '';
  if (typeof value === 'string' && /^[=+\-@]/.test(value)) return "'" + value;
  return value;
}

function reply(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}
