/*
 * Медицинский дневник — вкладка «Лекарства» (шаг 2): график приёма на сегодня,
 * отметки «принял / пропустил», список лекарств и напоминания об измерениях.
 *
 * Файл самодостаточен: не использует переменные app.js (только глобальную
 * showToast, если она есть) и сам читает CSRF-токен из <meta name="csrf-token">.
 * Весь пользовательский текст выводится через textContent (никакого innerHTML).
 *
 * Различие «факт» и «расчёт»: состояния taken/skipped — записанные отметки
 * пользователя; «Не отмечено» (unmarked) — расчёт сервера по времени и НЕ
 * означает, что лекарство не принято.
 */
(function () {
  'use strict';

  var root = document.getElementById('page-meds');
  var settingsRoot = document.getElementById('page-settings');
  if (!root) { return; }
  function medsUiVisible() { return !root.hidden || (settingsRoot && !settingsRoot.hidden); }

  var csrfMeta = document.querySelector('meta[name="csrf-token"]');
  var csrf = csrfMeta ? csrfMeta.content : '';

  var UNITS = ['мг', 'мкг', 'г', 'мл', 'шт', 'капли', 'ЕД'];
  var DAY_NAMES = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс'];
  var KINDS = [
    ['glucose', 'Глюкоза'], ['vitals', 'Давление и пульс'], ['weight', 'Вес'],
    ['temperature', 'Температура'], ['food', 'Питание'], ['custom', 'Своё']
  ];
  var KIND_ICON = { glucose: '💧', vitals: '♥', weight: '⚖️', temperature: '🌡️', food: '🍴', custom: '🔔' };
  var KIND_DEFAULT_TITLE = {
    glucose: 'Измерить глюкозу', vitals: 'Измерить давление и пульс', weight: 'Взвеситься',
    temperature: 'Измерить температуру', food: 'Записать приём пищи', custom: ''
  };

  var state = { meds: [], reminders: [], schedule: null, medsFilter: '', medsInactiveOpen: false };
  var loadSeq = 0;

  /* ------------------------------------------------------------ утилиты */

  function $(id) { return document.getElementById(id); }
  function pad(n) { return String(n).padStart(2, '0'); }
  function localDate() {
    var d = new Date();
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
  }
  function h(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) { e.className = cls; }
    if (text !== undefined && text !== null) { e.textContent = text; }
    return e;
  }
  function clear(el) { while (el.firstChild) { el.removeChild(el.firstChild); } }
  function toast(text, ok) { if (typeof showToast === 'function') { showToast(text, ok); } }
  function uuid() {
    if (window.crypto && window.crypto.randomUUID) { return window.crypto.randomUUID(); }
    return 'idem-' + Date.now() + '-' + Math.random().toString(36).slice(2);
  }
  function fmtNum(v) { return String(v).replace('.', ','); }
  function fmtDose(v, u) { return (v === null || v === undefined) ? '' : fmtNum(v) + '\u00a0' + (u || ''); }
  function fmtDayList(days) {
    if (!days || days.length === 7) { return 'каждый день'; }
    return days.map(function (d) { return DAY_NAMES[d]; }).join(', ');
  }
  function fmtStamp(s) {
    // 'YYYY-MM-DD HH:MM:SS' -> 'DD.MM HH:MM'
    if (!s) { return ''; }
    return s.slice(8, 10) + '.' + s.slice(5, 7) + ' ' + s.slice(11, 16);
  }
  function fmtDate(s) {
    if (!s) { return ''; }
    return s.slice(8, 10) + '.' + s.slice(5, 7) + '.' + s.slice(0, 4);
  }
  function joinParts(parts) { return parts.filter(function (p) { return p; }).join(' · '); }

  async function api(method, url, body, idemKey) {
    var opts = { method: method, headers: { 'X-CSRF-Token': csrf } };
    if (body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    if (idemKey) { opts.headers['Idempotency-Key'] = idemKey; }
    var res;
    try {
      res = await fetch(url, opts);
    } catch (e) {
      throw new Error('Нет соединения с сервером — проверьте интернет и попробуйте ещё раз.');
    }
    if (res.status === 401) { window.location = '/login'; throw new Error('Требуется вход'); }
    var out = {};
    try { out = await res.json(); } catch (e) { /* не JSON */ }
    if (!res.ok) { throw new Error(out.error || ('HTTP ' + res.status)); }
    return out;
  }

  function setMsg(id, text, ok) {
    var el = $(id);
    if (!el) { return; }
    el.textContent = text || '';
    el.className = 'message ' + (ok ? 'ok' : 'error');
  }

  /* ------------------------------------------------------- модальные окна */

  var MODALS = ['med-modal', 'rem-modal', 'intake-modal', 'barcode-scan-modal'];
  function openModal(id) { $(id).hidden = false; document.body.style.overflow = 'hidden'; }
  function closeModal(id) {
    var m = $(id);
    if (m) { m.hidden = true; }
    // Закрытие модалки сканирования любым способом (кнопка, backdrop,
    // Escape) должно гасить камеру, а не только прятать оверлей.
    if (id === 'barcode-scan-modal' && window.BarcodeScan) { window.BarcodeScan.cancel(); }
    var anyOpen = MODALS.some(function (x) { var e = $(x); return e && !e.hidden; });
    if (!anyOpen) { document.body.style.overflow = ''; }
  }
  function anyModalOpen() { return MODALS.some(function (x) { var e = $(x); return e && !e.hidden; }); }

  /* -------------------------------------------------- чипы дней и времена */

  function buildDayChips(container, prefix, days) {
    clear(container);
    for (var i = 0; i < 7; i++) {
      var inp = h('input');
      inp.type = 'checkbox';
      inp.id = prefix + '-d' + i;
      inp.value = String(i);
      inp.checked = days.indexOf(i) !== -1;
      var lab = h('label', null, DAY_NAMES[i]);
      lab.htmlFor = inp.id;
      container.appendChild(inp);
      container.appendChild(lab);
    }
  }
  function readDayChips(container) {
    return Array.prototype.filter.call(container.querySelectorAll('input'), function (x) { return x.checked; })
      .map(function (x) { return parseInt(x.value, 10); });
  }

  function addTimeRow(value) {
    var box = $('med-times');
    var row = h('div', 'time-row');
    var inp = h('input');
    inp.type = 'time';
    inp.value = value || '';
    inp.setAttribute('aria-label', 'Время приёма');
    var del = h('button', 'med-btn', '×');
    del.type = 'button';
    del.setAttribute('aria-label', 'Убрать время');
    del.addEventListener('click', function () { box.removeChild(row); });
    row.appendChild(inp);
    row.appendChild(del);
    box.appendChild(row);
  }
  function readTimes() {
    return Array.prototype.map.call($('med-times').querySelectorAll('input'), function (x) { return x.value; })
      .filter(function (v) { return v; });
  }

  /* ----------------------------------------------------------- загрузка */

  function setStatus(text) {
    // Ошибка может относиться к «Лекарствам» (график, список) или к
    // «Ещё → Уведомления» (напоминания) — блоки на разных вкладках,
    // поэтому пишем в оба места, где есть такой элемент.
    ['meds-status', 'notif-status'].forEach(function (id) {
      var el = $(id);
      if (!el) { return; }
      el.textContent = text || '';
      el.className = 'message ' + (text ? 'error' : '');
    });
  }

  async function load() {
    var seq = ++loadSeq;
    var day = localDate();
    var label = $('meds-date-label');
    if (label) { label.textContent = 'Сегодня, ' + fmtDate(day); }
    try {
      var r = await Promise.all([
        api('GET', '/api/medication-schedule?date=' + day),
        api('GET', '/api/medications'),
        api('GET', '/api/reminders')
      ]);
      if (seq !== loadSeq) { return; }
      state.schedule = r[0];
      state.meds = r[1].medications || [];
      state.reminders = r[2].reminders || [];
      setStatus('');
      render();
      pushRefresh();
    } catch (e) {
      if (seq === loadSeq) { setStatus(e.message); }
    }
  }

  /* ---------------------------------------------------------- отрисовка */

  function badge(kind, text) { return h('span', 'med-badge ' + kind, text); }

  function slotCard(slot) {
    // Сюда попадают только НЕотмеченные слоты (renderToday уже отфильтровал
    // recorded) — состояние всегда 'pending' или 'unmarked', отметки нет,
    // поэтому карточка всегда с активными кнопками, без ветки "Изменить".
    var card = h('div', 'med-card');
    card.appendChild(h('div', 'med-time', slot.time));
    var body = h('div', 'med-body');
    body.appendChild(h('div', 'med-name', slot.name));
    var sub = joinParts([fmtDose(slot.dose_value, slot.dose_unit), slot.instructions]);
    if (sub) { body.appendChild(h('div', 'med-sub', sub)); }
    body.appendChild(slot.state === 'unmarked' ? badge('unmarked', 'Не отмечено') : badge('pending', 'Ожидается'));
    card.appendChild(body);

    var actions = h('div', 'med-actions');
    var took = h('button', 'med-btn primary', 'Принял');
    took.type = 'button';
    took.addEventListener('click', function () { quickMark(slot, 'taken', [took, skip, more]); });
    var skip = h('button', 'med-btn', 'Пропустил');
    skip.type = 'button';
    skip.addEventListener('click', function () { quickMark(slot, 'skipped', [took, skip, more]); });
    var more = h('button', 'med-btn', '⋯');
    more.type = 'button';
    more.setAttribute('aria-label', 'Уточнить время или комментарий');
    more.addEventListener('click', function () { openIntakeForSlot(slot); });
    actions.appendChild(took);
    actions.appendChild(skip);
    actions.appendChild(more);
    card.appendChild(actions);
    return card;
  }

  function renderToday() {
    // Здесь только то, что ещё предстоит сделать. Отмеченное (принято или
    // пропущено) сразу уходит в историю приёмов — так график на сегодня
    // остаётся списком дел, а не журналом уже сделанного.
    var box = $('meds-today');
    clear(box);
    var s = state.schedule;
    if (!s) { return; }
    var pending = s.slots.filter(function (slot) { return slot.state_basis !== 'recorded'; });
    if (!pending.length) {
      var msg = !state.meds.length ? 'Пока нет лекарств. Добавьте первое ниже.'
        : (s.slots.length ? 'На сегодня всё отмечено — записи в истории приёмов ниже.' : 'На сегодня приёмов по графику нет.');
      box.appendChild(h('div', 'muted', msg));
      return;
    }
    pending.forEach(function (slot) { box.appendChild(slotCard(slot)); });
  }

  function medCard(m) {
    var card = h('div', 'med-card' + (m.is_active ? '' : ' inactive'));
    var body = h('div', 'med-body med-body-wide');
    body.appendChild(h('div', 'med-name', m.name));
    var sub = joinParts([fmtDose(m.dose_value, m.dose_unit), m.instructions]);
    if (sub) { body.appendChild(h('div', 'med-sub', sub)); }
    var when = m.times.length ? m.times.join(', ') + ' · ' + fmtDayList(m.days) : 'без графика (по мере необходимости)';
    body.appendChild(h('div', 'med-sub', when));
    if (m.end_date) { body.appendChild(h('div', 'med-sub', 'до ' + fmtDate(m.end_date))); }
    if (!m.is_active) { body.appendChild(badge('skipped', 'Приостановлено')); }
    card.appendChild(body);
    var actions = h('div', 'med-actions');
    var now = h('button', 'med-btn', 'Принял сейчас');
    now.type = 'button';
    now.addEventListener('click', function () { markNow(m, now); });
    var edit = h('button', 'med-btn', 'Изменить');
    edit.type = 'button';
    edit.addEventListener('click', function () { openMedModal(m); });
    actions.appendChild(now);
    actions.appendChild(edit);
    card.appendChild(actions);
    return card;
  }

  function renderMeds() {
    var q = (state.medsFilter || '').trim().toLowerCase();
    var filtered = q ? state.meds.filter(function (m) { return m.name.toLowerCase().indexOf(q) !== -1; }) : state.meds;
    var active = filtered.filter(function (m) { return m.is_active; });
    var inactive = filtered.filter(function (m) { return !m.is_active; });

    var box = $('meds-list');
    clear(box);
    if (!state.meds.length) {
      box.appendChild(h('div', 'muted', 'Список пуст.'));
    } else if (!active.length) {
      box.appendChild(h('div', 'muted', q ? 'Ничего не найдено.' : 'Активных лекарств нет — все приостановлены или завершены, см. ниже.'));
    } else {
      active.forEach(function (m) { box.appendChild(medCard(m)); });
    }

    var inactiveBox = $('meds-list-inactive');
    var toggle = $('meds-inactive-toggle');
    clear(inactiveBox);
    if (!inactive.length) {
      toggle.hidden = true;
      inactiveBox.hidden = true;
      return;
    }
    toggle.hidden = false;
    toggle.textContent = (state.medsInactiveOpen ? '▾ Скрыть' : '▸ Показать') + ' неактивные и завершённые · ' + inactive.length;
    inactiveBox.hidden = !state.medsInactiveOpen;
    inactive.forEach(function (m) { inactiveBox.appendChild(medCard(m)); });
  }

  function renderReminders() {
    var box = $('rem-list');
    clear(box);
    if (!state.reminders.length) { box.appendChild(h('div', 'muted', 'Напоминаний пока нет.')); return; }
    state.reminders.forEach(function (r) {
      var card = h('div', 'med-card rem-card' + (r.is_active ? '' : ' inactive'));
      card.appendChild(h('div', 'med-time', r.time));
      var body = h('div', 'med-body');
      body.appendChild(h('div', 'med-name', (KIND_ICON[r.kind] || '🔔') + ' ' + r.title));
      body.appendChild(h('div', 'med-sub', fmtDayList(r.days)));
      card.appendChild(body);

      var actions = h('div', 'med-actions');
      var edit = h('button', 'med-btn', 'Изменить');
      edit.type = 'button';
      edit.addEventListener('click', function () { openRemModal(r); });
      var sw = h('label', 'switch-row rem-switch');
      var cb = h('input');
      cb.type = 'checkbox';
      cb.checked = r.is_active;
      cb.setAttribute('aria-label', 'Напоминание включено');
      var knob = h('span', 'switch');
      cb.addEventListener('change', function () { toggleReminder(r, cb); });
      sw.appendChild(cb);
      sw.appendChild(knob);
      actions.appendChild(edit);
      actions.appendChild(sw);
      card.appendChild(actions);
      box.appendChild(card);
    });
  }

  function render() { renderToday(); renderMeds(); renderReminders(); }

  /* ------------------------------------------------------ быстрые отметки */

  async function quickMark(slot, status, buttons) {
    buttons.forEach(function (b) { b.disabled = true; });
    try {
      await api('POST', '/api/medication-intakes',
        { medication_id: slot.medication_id, scheduled_at: slot.scheduled_at, status: status },
        'intake-' + uuid());
      toast(status === 'taken' ? 'Приём отмечен' : 'Пропуск отмечен', true);
    } catch (e) {
      toast(e.message, false);
    }
    load();
  }

  async function markNow(med, btn) {
    var dose = fmtDose(med.dose_value, med.dose_unit);
    if (!window.confirm('Записать приём «' + med.name + '»' + (dose ? ' (' + dose + ')' : '') + ' сейчас?')) { return; }
    btn.disabled = true;
    try {
      await api('POST', '/api/medication-intakes', { medication_id: med.id, status: 'taken' }, 'intake-' + uuid());
      toast('Приём записан', true);
    } catch (e) {
      toast(e.message, false);
    }
    btn.disabled = false;
    load();
  }

  async function toggleReminder(r, cb) {
    var want = cb.checked;
    try {
      await api('PATCH', '/api/reminders/' + r.id, { is_active: want });
      r.is_active = want;
      cb.closest('.med-card').classList.toggle('inactive', !want);
    } catch (e) {
      cb.checked = !want;
      toast(e.message, false);
    }
  }

  /* ---------------------------------------------------- окно отметки приёма */

  var intakeCtx = null;

  function openIntakeCommon(ctx, title, sub) {
    intakeCtx = ctx;
    $('intake-title').textContent = title;
    $('intake-sub').textContent = sub;
    $('ik_taken').checked = ctx.status !== 'skipped';
    $('ik_skipped').checked = ctx.status === 'skipped';
    $('intake-status-group').hidden = !ctx.canSkip;
    $('intake-time').value = ctx.time || '';
    $('intake-comment').value = ctx.comment || '';
    $('intake-delete').hidden = !ctx.intakeId;
    setMsg('intake-msg', '', true);
    syncIntakeTimeVisibility();
    openModal('intake-modal');
  }

  function syncIntakeTimeVisibility() {
    $('intake-time-wrap').hidden = $('ik_skipped').checked;
  }

  function openIntakeForSlot(slot) {
    openIntakeCommon({
      intakeId: slot.intake_id,
      medicationId: slot.medication_id,
      scheduledAt: slot.scheduled_at,
      baseDate: (slot.taken_at || slot.scheduled_at).slice(0, 10),
      canSkip: true,
      status: slot.state === 'skipped' ? 'skipped' : 'taken',
      time: slot.taken_at ? slot.taken_at.slice(11, 16) : '',
      comment: slot.comment,
      idem: 'intake-' + uuid()
    }, slot.name, joinParts(['по графику ' + slot.time, fmtDose(slot.dose_value, slot.dose_unit)]));
  }

  function openIntakeForRecord(it) {
    // Открывает форму редактирования по строке из истории — подходит и
    // для приёма по графику, и для приёма вне графика (у него нет
    // scheduled_at, поэтому статус «пропустил» для него недоступен).
    var hasSlot = !!it.scheduled_at;
    openIntakeCommon({
      intakeId: it.id,
      medicationId: it.medication_id,
      scheduledAt: it.scheduled_at,
      baseDate: (it.taken_at || it.scheduled_at || '').slice(0, 10),
      canSkip: hasSlot,
      status: it.status,
      time: it.taken_at ? it.taken_at.slice(11, 16) : '',
      comment: it.comment,
      idem: null
    }, it.medication_name, joinParts([hasSlot ? ('по графику ' + it.scheduled_at.slice(11, 16)) : 'вне графика', fmtDose(it.dose_value, it.dose_unit)]));
  }

  async function saveIntake(ev) {
    ev.preventDefault();
    var c = intakeCtx;
    if (!c) { return; }
    var status = $('ik_skipped').checked ? 'skipped' : 'taken';
    var time = $('intake-time').value;
    var comment = $('intake-comment').value.trim();
    var btn = $('intake-save');
    btn.disabled = true;
    try {
      if (c.intakeId) {
        var upd = { status: status, comment: comment };
        if (status === 'taken' && time) { upd.taken_at = c.baseDate + ' ' + time; }
        await api('PATCH', '/api/medication-intakes/' + c.intakeId, upd);
      } else {
        var create = { medication_id: c.medicationId, scheduled_at: c.scheduledAt, status: status, comment: comment };
        if (status === 'taken' && time) { create.taken_at = c.baseDate + ' ' + time; }
        await api('POST', '/api/medication-intakes', create, c.idem);
      }
      closeModal('intake-modal');
      toast('Сохранено', true);
      load();
      refreshHistoryIfOpen();
    } catch (e) {
      setMsg('intake-msg', e.message, false);
    }
    btn.disabled = false;
  }

  async function deleteIntake() {
    var c = intakeCtx;
    if (!c || !c.intakeId) { return; }
    if (!window.confirm('Снять отметку? Запись о приёме будет удалена.')) { return; }
    try {
      await api('DELETE', '/api/medication-intakes/' + c.intakeId);
      closeModal('intake-modal');
      toast('Отметка снята', true);
      load();
      refreshHistoryIfOpen();
    } catch (e) {
      setMsg('intake-msg', e.message, false);
    }
  }

  /* ------------------------------------------------------ окно лекарства */

  var medCtx = null;

  function hideMedNameSuggest() {
    var box = $('med-name-suggest');
    if (box) { box.hidden = true; clear(box); }
  }

  var medNameSuggestSeq = 0;
  async function fetchMedNameSuggest(q) {
    var seq = ++medNameSuggestSeq;
    var items;
    try {
      var r = await api('GET', '/api/medications/suggest?q=' + encodeURIComponent(q));
      items = r.items || [];
    } catch (e) {
      hideMedNameSuggest();
      return;
    }
    if (seq !== medNameSuggestSeq) { return; } // пришёл ответ на уже неактуальный запрос
    var box = $('med-name-suggest');
    if (!box) { return; }
    clear(box);
    if (!items.length) { box.hidden = true; return; }
    items.forEach(function (name) {
      var row = h('div', 'suggest-item', name);
      // mousedown, а не click — чтобы сработать раньше blur у поля ввода
      row.addEventListener('mousedown', function (e) {
        e.preventDefault();
        $('med-name').value = name;
        hideMedNameSuggest();
      });
      box.appendChild(row);
    });
    box.hidden = false;
  }

  function openMedModal(med) {
    medCtx = { id: med ? med.id : null, idem: med ? null : 'med-' + uuid(), gtin: null };
    hideMedNameSuggest();
    $('med-modal-title').textContent = med ? 'Изменить лекарство' : 'Новое лекарство';
    $('med-name').value = med ? med.name : '';
    $('med-dose').value = med && med.dose_value !== null ? fmtNum(med.dose_value) : '';
    var unitSel = $('med-unit');
    if (!unitSel.options.length) {
      UNITS.forEach(function (u) { var o = h('option', null, u); o.value = u; unitSel.appendChild(o); });
    }
    unitSel.value = med && med.dose_unit ? med.dose_unit : UNITS[0];
    $('med-instr').value = med ? med.instructions : '';
    clear($('med-times'));
    (med ? med.times : ['08:00']).forEach(addTimeRow);
    buildDayChips($('med-days'), 'med', med ? med.days : [0, 1, 2, 3, 4, 5, 6]);
    $('med-start').value = med ? med.start_date : localDate();
    $('med-end').value = med && med.end_date ? med.end_date : '';
    $('med-comment').value = med ? med.comment : '';
    $('med-active').checked = med ? med.is_active : true;
    $('med-delete').hidden = !med;
    setMsg('med-msg', '', true);
    openModal('med-modal');
  }

  /* Скан штрихкода/DataMatrix упаковки — только подстановка названия и
   * дозы в форму, ничего не сохраняет само по себе. Пользователь всегда
   * может поправить подставленное перед сохранением. */
  async function scanMedBarcode() {
    setMsg('barcode-scan-msg', 'Наведите камеру на штрихкод упаковки', true);
    openModal('barcode-scan-modal');
    var gtin;
    try {
      gtin = await BarcodeScan.scanOnce('barcode-video');
    } catch (e) {
      closeModal('barcode-scan-modal');
      toast(e.message || 'Не удалось отсканировать код', false);
      return;
    }
    closeModal('barcode-scan-modal');
    medCtx.gtin = gtin;
    try {
      var found = await api('GET', '/api/medication-barcodes/' + encodeURIComponent(gtin));
      if (found.found && found.source === 'personal') {
        $('med-name').value = found.name;
        $('med-dose').value = found.dose_value !== null && found.dose_value !== undefined ? fmtNum(found.dose_value) : '';
        if (found.dose_unit) { $('med-unit').value = found.dose_unit; }
        toast('Название и доза подставлены из вашего справочника — проверьте перед сохранением', true);
      } else if (found.found && found.source === 'mdlp') {
        $('med-name').value = found.name;
        var hint = found.dose_hint ? (' Доза по данным маркировки: ' + found.dose_hint + '.') : '';
        toast('Название подставлено из открытых данных «Честного знака».' + hint + ' Впишите дозу и проверьте название перед сохранением', true);
      } else {
        toast('Код не найден ни в вашем справочнике, ни в открытых данных — введите название, оно запомнится', true);
      }
    } catch (e) {
      // Поиск в справочнике не критичен для продолжения — просто не
      // подставляем название, пользователь вводит его сам.
      toast('Код отсканирован, но справочник недоступен — введите название вручную', false);
    }
  }

  async function saveMed(ev) {
    ev.preventDefault();
    var name = $('med-name').value.trim();
    var doseRaw = $('med-dose').value.trim().replace(',', '.');
    if (!name) { setMsg('med-msg', 'Название: заполните поле', false); return; }
    if (doseRaw && !/^\d+(\.\d+)?$/.test(doseRaw)) { setMsg('med-msg', 'Доза: введите число', false); return; }
    var days = readDayChips($('med-days'));
    if (!days.length) { setMsg('med-msg', 'Дни недели: выберите хотя бы один день', false); return; }
    var payload = {
      name: name,
      dose_value: doseRaw,
      dose_unit: doseRaw ? $('med-unit').value : '',
      instructions: $('med-instr').value.trim(),
      times: readTimes(),
      days: days,
      start_date: $('med-start').value,
      end_date: $('med-end').value,
      comment: $('med-comment').value.trim(),
      is_active: $('med-active').checked,
      source: medCtx.gtin ? 'barcode_scan' : 'manual'
    };
    var btn = $('med-save');
    btn.disabled = true;
    try {
      if (medCtx.id) { await api('PATCH', '/api/medications/' + medCtx.id, payload); }
      else { await api('POST', '/api/medications', payload, medCtx.idem); }
      if (medCtx.gtin) {
        // Обновляем личный справочник «GTIN → название» под итоговым,
        // возможно поправленным пользователем текстом. Не критично для
        // основного сохранения — ошибку здесь не показываем пользователю
        // как провал операции, само лекарство уже сохранено.
        try {
          await api('PUT', '/api/medication-barcodes/' + encodeURIComponent(medCtx.gtin), {
            name: name,
            dose_value: payload.dose_value,
            dose_unit: payload.dose_unit
          });
        } catch (e) { /* не критично для основного сохранения */ }
      }
      closeModal('med-modal');
      toast('Сохранено', true);
      load();
    } catch (e) {
      setMsg('med-msg', e.message, false);
    }
    btn.disabled = false;
  }

  async function deleteMed() {
    if (!medCtx || !medCtx.id) { return; }
    if (!window.confirm('Удалить лекарство? История приёмов сохранится.')) { return; }
    try {
      await api('DELETE', '/api/medications/' + medCtx.id);
      closeModal('med-modal');
      toast('Лекарство удалено', true);
      load();
    } catch (e) {
      setMsg('med-msg', e.message, false);
    }
  }

  /* ---------------------------------------------------- окно напоминания */

  var remCtx = null;

  function updateRemTitleHint() {
    var kind = $('rem-kind').value;
    $('rem-title').placeholder = KIND_DEFAULT_TITLE[kind] || 'Например: Пить воду';
  }

  function openRemModal(rem) {
    remCtx = { id: rem ? rem.id : null, idem: rem ? null : 'rem-' + uuid() };
    $('rem-modal-title').textContent = rem ? 'Изменить напоминание' : 'Новое напоминание';
    var kindSel = $('rem-kind');
    if (!kindSel.options.length) {
      KINDS.forEach(function (k) { var o = h('option', null, k[1]); o.value = k[0]; kindSel.appendChild(o); });
    }
    kindSel.value = rem ? rem.kind : 'glucose';
    $('rem-title').value = rem && rem.title !== KIND_DEFAULT_TITLE[rem.kind] ? rem.title : '';
    updateRemTitleHint();
    $('rem-time').value = rem ? rem.time : '08:00';
    buildDayChips($('rem-days'), 'rem', rem ? rem.days : [0, 1, 2, 3, 4, 5, 6]);
    $('rem-active').checked = rem ? rem.is_active : true;
    $('rem-delete').hidden = !rem;
    setMsg('rem-msg', '', true);
    openModal('rem-modal');
  }

  async function saveRem(ev) {
    ev.preventDefault();
    var days = readDayChips($('rem-days'));
    if (!days.length) { setMsg('rem-msg', 'Дни недели: выберите хотя бы один день', false); return; }
    if (!$('rem-time').value) { setMsg('rem-msg', 'Время: укажите время', false); return; }
    var payload = {
      kind: $('rem-kind').value,
      title: $('rem-title').value.trim(),
      time: $('rem-time').value,
      days: days,
      is_active: $('rem-active').checked
    };
    var btn = $('rem-save');
    btn.disabled = true;
    try {
      if (remCtx.id) { await api('PATCH', '/api/reminders/' + remCtx.id, payload); }
      else { await api('POST', '/api/reminders', payload, remCtx.idem); }
      closeModal('rem-modal');
      toast('Сохранено', true);
      load();
    } catch (e) {
      setMsg('rem-msg', e.message, false);
    }
    btn.disabled = false;
  }

  async function deleteRem() {
    if (!remCtx || !remCtx.id) { return; }
    if (!window.confirm('Удалить напоминание?')) { return; }
    try {
      await api('DELETE', '/api/reminders/' + remCtx.id);
      closeModal('rem-modal');
      toast('Напоминание удалено', true);
      load();
    } catch (e) {
      setMsg('rem-msg', e.message, false);
    }
  }

  /* -------------------------------------------------------------- история */

  function refreshHistoryIfOpen() {
    var d = $('meds-history-details');
    if (d && d.open) { loadHistory(); }
  }

  async function loadHistory() {
    var box = $('meds-history');
    clear(box);
    box.appendChild(h('div', 'muted', 'Загрузка…'));
    try {
      var out = await api('GET', '/api/medication-intakes');
      clear(box);
      var list = out.intakes || [];
      if (!list.length) { box.appendChild(h('div', 'muted', 'За 30 дней отметок нет.')); return; }
      list.forEach(function (it) {
        var row = h('div', 'med-hist');
        row.appendChild(h('div', 'med-hist-time', fmtStamp(it.scheduled_at || it.taken_at)));
        var body = h('div', 'med-body med-body-wide');
        body.appendChild(h('div', 'med-name', it.medication_name));
        var sub = joinParts([fmtDose(it.dose_value, it.dose_unit), it.scheduled_at ? '' : 'вне графика']);
        if (sub) { body.appendChild(h('div', 'med-sub', sub)); }
        body.appendChild(it.status === 'taken'
          ? badge('taken', '✓ Принято' + (it.taken_at ? ' в ' + it.taken_at.slice(11, 16) : ''))
          : badge('skipped', 'Пропущено'));
        if (it.comment) { body.appendChild(h('div', 'med-sub', it.comment)); }
        var actions = h('div', 'med-actions');
        var edit = h('button', 'med-btn', 'Изменить');
        edit.type = 'button';
        edit.addEventListener('click', function () { openIntakeForRecord(it); });
        actions.appendChild(edit);
        body.appendChild(actions);
        row.appendChild(body);
        box.appendChild(row);
      });
    } catch (e) {
      clear(box);
      box.appendChild(h('div', 'message error', e.message));
    }
  }

  /* --------------------------------------------------------- привязка событий */

  $('med-add-btn').addEventListener('click', function () { openMedModal(null); });
  if ($('med-scan-btn')) { $('med-scan-btn').addEventListener('click', scanMedBarcode); }
  // Быстрый вход в скан прямо со страницы списка, без промежуточного шага
  // «сначала открой пустую форму, потом заметь мелкую ссылку скана внутри».
  if ($('med-scan-quick-btn')) {
    $('med-scan-quick-btn').addEventListener('click', function () {
      openMedModal(null);
      scanMedBarcode();
    });
  }
  if ($('meds-search')) {
    $('meds-search').addEventListener('input', function (e) {
      state.medsFilter = e.target.value;
      renderMeds();
    });
  }
  if ($('meds-inactive-toggle')) {
    $('meds-inactive-toggle').addEventListener('click', function () {
      state.medsInactiveOpen = !state.medsInactiveOpen;
      renderMeds();
    });
  }
  if ($('med-name')) {
    var medNameSuggestTimer = null;
    $('med-name').addEventListener('input', function (e) {
      var q = e.target.value.trim();
      clearTimeout(medNameSuggestTimer);
      if (q.length < 2) { hideMedNameSuggest(); return; }
      medNameSuggestTimer = setTimeout(function () { fetchMedNameSuggest(q); }, 250);
    });
    $('med-name').addEventListener('blur', function () {
      setTimeout(hideMedNameSuggest, 150); // даём mousedown на подсказке сработать раньше
    });
  }
  $('rem-add-btn').addEventListener('click', function () { openRemModal(null); });
  $('med-time-add').addEventListener('click', function () { addTimeRow(''); });
  $('med-form').addEventListener('submit', saveMed);
  $('med-delete').addEventListener('click', deleteMed);
  $('rem-form').addEventListener('submit', saveRem);
  $('rem-delete').addEventListener('click', deleteRem);
  $('rem-kind').addEventListener('change', updateRemTitleHint);
  $('intake-form').addEventListener('submit', saveIntake);
  $('intake-delete').addEventListener('click', deleteIntake);
  $('ik_taken').addEventListener('change', syncIntakeTimeVisibility);
  $('ik_skipped').addEventListener('change', syncIntakeTimeVisibility);
  $('meds-history-details').addEventListener('toggle', function () {
    if ($('meds-history-details').open) { loadHistory(); }
  });

  Array.prototype.forEach.call(document.querySelectorAll('[data-close]'), function (btn) {
    btn.addEventListener('click', function () { closeModal(btn.getAttribute('data-close')); });
  });
  MODALS.forEach(function (id) {
    $(id).addEventListener('click', function (e) { if (e.target === this) { closeModal(id); } });
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') { MODALS.forEach(closeModal); }
  });


  /* ------------------------------------------------------------ уведомления */

  var pushBusy = false;

  function pushSupported() {
    return ('serviceWorker' in navigator) && ('PushManager' in window) && ('Notification' in window);
  }
  function withTimeout(promise, ms, message) {
    return new Promise(function (resolve, reject) {
      var t = setTimeout(function () { reject(new Error(message)); }, ms);
      promise.then(function (v) { clearTimeout(t); resolve(v); }, function (e) { clearTimeout(t); reject(e); });
    });
  }
  function b64uToBytes(s) {
    s = s.replace(/-/g, '+').replace(/_/g, '/');
    while (s.length % 4) { s += '='; }
    var bin = atob(s), out = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) { out[i] = bin.charCodeAt(i); }
    return out;
  }
  function getRegistration() {
    return withTimeout(navigator.serviceWorker.ready, 5000,
      'Служебный процесс приложения не запущен. Перезапустите приложение и попробуйте снова.');
  }

  var PUSH_REASONS = {
    library_missing: 'На сервере не установлен модуль уведомлений (нужна пересборка образа).',
    disabled: 'Уведомления отключены на сервере.',
    key_error: 'На сервере не удалось прочитать ключ уведомлений.'
  };

  function pushRender(text, showToggle, toggleText, showTest) {
    $('push-status').textContent = text;
    var tg = $('push-toggle');
    tg.hidden = !showToggle;
    tg.textContent = toggleText || 'Включить уведомления';
    tg.disabled = false;
    $('push-test').hidden = !showTest;
    $('push-test').disabled = false;
  }

  async function pushRefresh() {
    if (!pushSupported()) {
      pushRender('Уведомления работают в приложении, добавленном на экран «Домой» (iOS 16.4 и новее). Откройте дневник с иконки на экране.', false, '', false);
      return;
    }
    try {
      var cfg = await api('GET', '/api/push/config');
      if (!cfg.available) {
        pushRender(PUSH_REASONS[cfg.reason] || 'Уведомления на сервере недоступны.', false, '', false);
        return;
      }
      if (Notification.permission === 'denied') {
        pushRender('Уведомления запрещены в настройках телефона или браузера для этого приложения.', false, '', false);
        return;
      }
      var reg = await getRegistration();
      var sub = await reg.pushManager.getSubscription();
      if (sub && cfg.subscriptions > 0) {
        pushRender('Уведомления включены на этом устройстве.', true, 'Отключить', true);
        $('push-toggle').className = 'med-btn';
      } else {
        pushRender('Уведомления выключены.', true, 'Включить уведомления', false);
        $('push-toggle').className = 'med-btn primary';
      }
    } catch (e) {
      pushRender(e.message, false, '', false);
    }
  }

  async function pushEnable() {
    // Разрешение запрашиваем первым же вызовом внутри обработчика нажатия:
    // Safari требует жест пользователя.
    var perm = await Notification.requestPermission();
    if (perm !== 'granted') {
      throw new Error('Разрешение на уведомления не получено. Его можно включить в настройках телефона.');
    }
    var cfg = await api('GET', '/api/push/config');
    if (!cfg.available) { throw new Error(PUSH_REASONS[cfg.reason] || 'Уведомления на сервере недоступны.'); }
    var reg = await getRegistration();
    var old = await reg.pushManager.getSubscription();
    if (old) {
      // Подписка могла быть создана с другим ключом сервера — создаём заново.
      var oldEndpoint = old.endpoint;
      try { await old.unsubscribe(); } catch (e) { /* уже недействительна */ }
      try { await api('POST', '/api/push/unsubscribe', { endpoint: oldEndpoint }); } catch (e) { /* не критично */ }
    }
    var sub = await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: b64uToBytes(cfg.public_key)
    });
    await api('POST', '/api/push/subscribe', sub.toJSON());
  }

  async function pushDisable() {
    var reg = await getRegistration();
    var sub = await reg.pushManager.getSubscription();
    if (sub) {
      var endpoint = sub.endpoint;
      await api('POST', '/api/push/unsubscribe', { endpoint: endpoint });
      try { await sub.unsubscribe(); } catch (e) { /* ничего страшного */ }
    }
  }

  async function onPushToggle() {
    if (pushBusy) { return; }
    pushBusy = true;
    $('push-toggle').disabled = true;
    try {
      var enabled = $('push-toggle').textContent === 'Отключить';
      if (enabled) { await pushDisable(); toast('Уведомления отключены', true); }
      else { await pushEnable(); toast('Уведомления включены', true); }
      pushBusy = false;
      pushRefresh();
    } catch (e) {
      // Показываем именно причину сбоя. pushRefresh() здесь не вызываем: она
      // заново читает Notification.permission и способна перекрыть точное
      // сообщение общим статусом ("запрещены") — это уже не то, что произошло.
      pushBusy = false;
      toast(e.message, false);
      pushRender(e.message, true, 'Включить уведомления', false);
    }
  }

  async function onPushTest() {
    $('push-test').disabled = true;
    try {
      var r = await api('POST', '/api/push/test', {});
      toast(r.ok ? 'Тестовое уведомление отправлено' : 'Не удалось доставить уведомление', !!r.ok);
    } catch (e) {
      toast(e.message, false);
    }
    $('push-test').disabled = false;
  }

  $('push-toggle').addEventListener('click', onPushToggle);
  $('push-test').addEventListener('click', onPushTest);

  // Нажатие на уведомление: service worker сообщает, какую вкладку открыть.
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.addEventListener('message', function (e) {
      var d = e.data;
      if (d && d.type === 'open-tab' && typeof showPage === 'function' && ['input', 'meds'].indexOf(d.tab) !== -1) {
        showPage(d.tab);
      }
    });
  }
  // Приложение открыто нажатием на уведомление (/?tab=meds).
  (function () {
    var m = /[?&]tab=(input|meds)\b/.exec(window.location.search);
    if (m && typeof showPage === 'function') { showPage(m[1]); }
  })();

  // Обновление, пока вкладка открыта: при возврате в приложение и раз в минуту
  // (расчётное состояние «Не отмечено» меняется со временем). Пока открыто
  // окно редактирования, список не перерисовываем.
  function refreshIfVisible() {
    if (!document.hidden && medsUiVisible() && !anyModalOpen()) { load(); }
  }
  document.addEventListener('visibilitychange', refreshIfVisible);
  setInterval(refreshIfVisible, 60000);

  window.MedsUI = { load: load };
  if (medsUiVisible()) { load(); }
})();
