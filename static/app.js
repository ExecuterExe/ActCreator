/* АктКреватор: минимум поведения поверх серверных страниц. Без библиотек.
   Честное ожидание ответа ИИ: форма с data-busy блокируется от повторной отправки
   и показывает, что идёт работа (без выдуманных процентов). */
(function () {
  "use strict";

  // Необратимые действия (удалить идею) — только после явного согласия.
  document.addEventListener("submit", function (event) {
    var submitter = event.submitter;
    var question = submitter && submitter.getAttribute("data-confirm");
    if (question && !window.confirm(question)) {
      event.preventDefault();
      event.stopImmediatePropagation();
    }
  }, true);

  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!(form instanceof HTMLFormElement) || !form.hasAttribute("data-busy")) return;
    if (form.getAttribute("aria-busy") === "true") { event.preventDefault(); return; }
    var submitter = event.submitter;
    // Кнопка, которой отправили форму, должна дойти до сервера, даже если мы её заблокируем.
    if (submitter && submitter.name) {
      var hidden = document.createElement("input");
      hidden.type = "hidden";
      hidden.name = submitter.name;
      hidden.value = submitter.value;
      form.appendChild(hidden);
    }
    form.setAttribute("aria-busy", "true");
    var note = form.querySelector(".busy-note");
    if (!note) {  // статус ожидания нужен всегда: его читает экранный диктор
      note = document.createElement("span");
      note.className = "busy-note";
      note.setAttribute("aria-live", "polite");
      (submitter && submitter.parentNode || form).appendChild(note);
    }
    // У кнопки может быть своё описание ожидания (подсказка — одно, разбор версии — другое).
    // Пустой data-busy у кнопки — действие мгновенное, ИИ не вызывается: никакого «разбираем…».
    var text = submitter && submitter.hasAttribute("data-busy")
      ? submitter.getAttribute("data-busy") : form.getAttribute("data-busy");
    // Текст — чуть позже вставки: так живую область объявляют и NVDA, и VoiceOver.
    if (text) window.setTimeout(function () { note.textContent = text; }, 60);
    else note.textContent = "";
    window.setTimeout(function () {
      form.querySelectorAll("button").forEach(function (b) { b.disabled = true; });
      if (submitter) submitter.classList.add("btn-busy");
    }, 0);
  }, true);

  // «Чистый лист» на экране своей идеи: прячем историю и помощь атрибутом hidden (их не видит и диктор),
  // выбор помним в этом браузере, а флаг уходит на сервер вместе с идеей — чтобы проверить, нужен ли режим.
  var toggle = document.querySelector("[data-focus-toggle]");
  if (toggle) {
    var KEY = "actkreator.focus";
    var hide = document.querySelectorAll("[data-focus-hide]");
    var field = document.querySelector("[data-focus-field]");
    var exit = document.querySelector("[data-focus-exit]");
    var hint = document.querySelector("[data-focus-hint]");
    var wasHidden = Array.prototype.map.call(hide, function (el) { return el.hidden; });
    var setFocus = function (on) {
      Array.prototype.forEach.call(hide, function (el, i) { el.hidden = on || wasHidden[i]; });
      toggle.setAttribute("aria-pressed", on ? "true" : "false");
      toggle.textContent = on ? "Вернуть историю и помощь" : "Чистый лист";
      if (hint) hint.hidden = on;
      if (field) field.value = on ? "1" : "0";
      if (exit) exit.hidden = !on;
      document.body.classList.toggle("is-focus", on);
      try { window.localStorage.setItem(KEY, on ? "1" : "0"); } catch (e) { /* без хранилища — просто не помним */ }
    };
    var saved = null;
    try { saved = window.localStorage.getItem(KEY); } catch (e) { saved = null; }
    toggle.closest("[data-focus-bar]").hidden = false;
    setFocus(saved === "1");
    toggle.addEventListener("click", function () { setFocus(toggle.getAttribute("aria-pressed") !== "true"); });
    if (exit) exit.addEventListener("click", function () { setFocus(false); toggle.focus(); });
  }

  // «Сохранить как PDF»: свёрнутые разделы (разбор по частям и т. п.) печатаются раскрытыми, потом — как было.
  var closedForPrint = [];
  window.addEventListener("beforeprint", function () {
    closedForPrint = Array.prototype.filter.call(document.querySelectorAll("details:not([open])"), function (d) {
      d.open = true;
      return true;
    });
  });
  window.addEventListener("afterprint", function () {
    closedForPrint.forEach(function (d) { d.open = false; });
    closedForPrint = [];
  });

  // Форма вернулась с ошибкой — переводим фокус на первую: диктор прочтёт её сразу, а не начнёт с шапки.
  var error = document.querySelector("main .error");
  if (error) {
    error.setAttribute("tabindex", "-1");
    error.focus();
  }
})();
