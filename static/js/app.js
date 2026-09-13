/*
 * Медицинский дневник — основной JS приложения.
 * Извлечено из двух встроенных <script> в app.py (шаг 1 модуляризации):
 *  1) минимальный аварийный слой интерфейса (fallback), не зависящий от
 *     остального кода — если ниже что-то упадёт, базовые кнопки продолжат работать;
 *  2) основная логика приложения.
 *
 * Внимание: этот файл НЕ содержит Jinja-переменных (IS_ADMIN, USER_ID,
 * USER_SETTINGS, RANGES, DEFAULT_RANGES_JS) — они объявляются в маленьком
 * инлайн-<script> в самом HTML-шаблоне ПЕРЕД подключением этого файла,
 * и используются здесь как уже существующие глобальные переменные.
 */
(function () {
  // Минимальный аварийный слой интерфейса. Он не зависит от остального JS-бандла:
  // если дополнительный код упадёт, основные кнопки всё равно должны работать.
  function nowLocalDateTime() {
    var d = new Date();
    function p(n) { return String(n).padStart(2, '0'); }
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + 'T' + p(d.getHours()) + ':' + p(d.getMinutes());
  }
  function fallbackOpen(type) {
    var modal = document.getElementById('entry-modal');
    if (!modal) return;
    ['glucose','vitals','temperature','weight','food'].forEach(function(k) {
      var f = document.getElementById(k + '-form');
      if (f) f.hidden = (k !== type);
    });
    var cfg = {
      glucose:['Глюкоза','Ввод показателя','💧','glucose'],
      vitals:['Давление и пульс','Ввод показателей','♥','vitals'],
      temperature:['Температура','Ввод показателя','🌡️','temperature'],
      weight:['Вес','Ввод показателя','⚖️','weight'],
      food:['Питание','Новая запись','🍴','food']
    }[type];
    if (!cfg) return;
    var title=document.getElementById('modal-title'); if(title) title.textContent=cfg[0];
    var subtitle=document.getElementById('modal-subtitle'); if(subtitle) subtitle.textContent=cfg[1];
    var icon=document.getElementById('modal-icon'); if(icon){icon.textContent=cfg[2];icon.className='quick-icon '+cfg[3];}
    modal.hidden=false;
    document.body.style.overflow='hidden';
    var form=document.getElementById(type+'-form');
    if(form){var dt=form.querySelector('.dt');if(dt)dt.value=nowLocalDateTime();}
  }
  function fallbackPage(name) {
    ['input','history','settings'].forEach(function(p){
      var page=document.getElementById('page-'+p); if(page) page.hidden=(p!==name);
      var btn=document.getElementById('tab-btn-'+p); if(btn) btn.classList.toggle('active',p===name);
    });
  }
  function callOrFallback(fn, args, fallback) {
    try {
      if (typeof window[fn] === 'function') { window[fn].apply(window,args||[]); }
      else { fallback.apply(window,args||[]); }
    } catch (e) { fallback.apply(window,args||[]); }
  }
  function bind(id, handler) {
    var el=document.getElementById(id);
    if(!el || el.__coreBound) return;
    el.__coreBound=true;
    el.addEventListener('click', function(e){
      try {
        e.preventDefault();
        e.stopImmediatePropagation();
        handler(e);
      } catch (_) {}
    }, true);
  }
  bind('card-glucose', function(e){ callOrFallback('openEntry',['glucose'],fallbackOpen); });
  bind('card-vitals', function(e){ callOrFallback('openEntry',['vitals'],fallbackOpen); });
  bind('card-temperature', function(e){ callOrFallback('openEntry',['temperature'],fallbackOpen); });
  bind('card-weight', function(e){ callOrFallback('openEntry',['weight'],fallbackOpen); });
  bind('card-food', function(e){ callOrFallback('openEntry',['food'],fallbackOpen); });
  bind('tab-btn-input', function(e){ callOrFallback('showPage',['input'],fallbackPage); });
  bind('tab-btn-history', function(e){ callOrFallback('showPage',['history'],fallbackPage); });
  bind('tab-btn-settings', function(e){ callOrFallback('showPage',['settings'],fallbackPage); });
  bind('modal-close-core', function(e){ var m=document.getElementById('entry-modal');if(m){m.hidden=true;document.body.style.overflow='';} });
  bind('header-logout-core', function(e){
    try {
      if (typeof window.logout === 'function') { window.logout(); return; }
      var meta=document.querySelector('meta[name=csrf-token]');
      var token=meta ? meta.content : '';
      fetch('/logout',{method:'POST',headers:{'X-CSRF-Token':token}}).finally(function(){window.location='/login';});
    } catch (_) { window.location='/login'; }
  });
})();


function openEntry(type){
  var modal=document.getElementById('entry-modal'); if(!modal)return;
  ['glucose','vitals','temperature','weight','food'].forEach(function(k){var f=document.getElementById(k+'-form'); if(f)f.hidden=(k!==type);});
  var cfg={glucose:['Глюкоза','Ввод показателя','💧','glucose'],vitals:['Давление и пульс','Ввод показателей','♥','vitals'],temperature:['Температура','Ввод показателя','🌡️','temperature'],weight:['Вес','Ввод показателя','⚖️','weight'],food:['Питание','Новая запись','🍴','food']}[type];
  document.getElementById('modal-title').textContent=cfg[0]; document.getElementById('modal-subtitle').textContent=cfg[1];
  var icon=document.getElementById('modal-icon'); icon.textContent=cfg[2]; icon.className='quick-icon '+cfg[3];
  modal.hidden=false; document.body.style.overflow='hidden';
  var form=document.getElementById(type+'-form'); if(form){var dt=form.querySelector('.dt');if(dt)dt.value=localDateTime();}
  loadOneLastStatus(type);
}
function closeEntry(){var m=document.getElementById('entry-modal');if(m)m.hidden=true;document.body.style.overflow='';}
document.getElementById('entry-modal').addEventListener('click',function(e){if(e.target===this)closeEntry();});

// Десктопные Chrome/Opera открывают нативный календарь/таймпикер только по
// клику на маленькую иконку внутри поля, а не по клику в любом месте поля.
// Для date_from/date_to иконка визуально скрыта (поле растянуто прозрачным
// слоем поверх кастомной кнопки), поэтому клик по кнопке не всегда
// попадает в зону иконки и пикер не открывается. showPicker() открывает
// пикер программно по любому клику в пределах поля — работает во всех
// Chromium-браузерах (Chrome, Opera, Edge); там, где showPicker()
// недоступен (например, Firefox, Safari), просто ничего не делаем и
// оставляем обычное поведение браузера как было.
document.addEventListener('click', function(e) {
  var t = e.target;
  if (!t || t.tagName !== 'INPUT') return;
  if (t.type !== 'date' && t.type !== 'datetime-local' && t.type !== 'time') return;
  if (t.disabled || t.readOnly) return;
  if (typeof t.showPicker !== 'function') return;
  try { t.showPicker(); } catch (err) { /* пикер уже открыт или вызван не из пользовательского жеста — игнорируем */ }
}, true);

// Регистрация service worker для установки приложения на Android/iOS как
// PWA (иконка на экране, полноэкранный режим). Полностью необязательна:
// если /sw.js ещё не размещён на сервере или браузер не поддерживает
// Service Worker API — просто ничего не произойдёт, остальной функционал
// приложения не зависит от этого блока.
if ('serviceWorker' in navigator) {
  window.addEventListener('load', function () {
    navigator.serviceWorker.register('/sw.js').catch(function () {
      // Например, файл ещё не выложен в static/ — не мешаем работе приложения.
    });
  });
}

function todayHuman(){var d=new Date();var months=['января','февраля','марта','апреля','мая','июня','июля','августа','сентября','октября','ноября','декабря'];return d.getDate()+' '+months[d.getMonth()]+' '+d.getFullYear();}
document.getElementById('today-label').textContent='Сегодня, '+todayHuman();
function loadDashboard(){
  var box=document.getElementById('dashboard-recent'); if(!box)return;
  fetch('/api/history?date_from='+encodeURIComponent(localDate(-6))+'&date_to='+encodeURIComponent(localDate(0))+'&type=all&sort=date').then(function(r){if(!r.ok)throw new Error('HTTP '+r.status);return r.json();}).then(function(out){
    var entries=out.entries||[]; box.innerHTML=''; entries.slice(0,3).forEach(function(e){var c=document.createElement('div');c.className='recent-card';var icon=document.createElement('div');icon.className='history-icon '+e.type;icon.textContent=e.type==='glucose'?'💧':e.type==='vitals'?'♥':e.type==='temperature'?'🌡️':e.type==='weight'?'⚖️':'🍴';c.appendChild(icon);var copy=document.createElement('div');copy.className='recent-copy';var t=document.createElement('div');t.className='recent-title';t.textContent=e.type==='glucose'?'Глюкоза':e.type==='vitals'?'Давление и пульс':e.type==='temperature'?'Температура':e.type==='weight'?'Вес':'Питание';copy.appendChild(t);var v=document.createElement('div');v.className='recent-value';if(e.type==='glucose')v.textContent=Number(e.value_mmol_l).toFixed(1)+' ммоль/л · '+(e.glucose_type==='fasting'?'натощак':'после еды');else if(e.type==='vitals')v.textContent=e.systolic_mmhg+' / '+e.diastolic_mmhg+' мм рт. ст. · пульс '+(e.pulse_bpm==null?'—':e.pulse_bpm);else if(e.type==='temperature')v.textContent=Number(e.temperature_c).toFixed(1)+' °C';else if(e.type==='weight')v.textContent=Number(e.weight_kg).toFixed(1)+' кг';else v.textContent=e.food_name+' · '+e.amount_value+' '+unitRu(e.amount_unit);copy.appendChild(v);c.appendChild(copy);var tm=document.createElement('div');tm.className='recent-time';tm.textContent=(e.measured_at||'').substring(11,16);c.appendChild(tm);box.appendChild(c);});if(!entries.length)box.innerHTML='<div class="muted">Пока нет записей</div>';
    var today=localDate(0), counts={glucose:0,vitals:0,temperature:0,weight:0,food:0}; entries.forEach(function(e){if((e.measured_at||'').substring(0,10)===today)counts[e.type]++;}); document.getElementById('sum-glucose').textContent=counts.glucose;document.getElementById('sum-vitals').textContent=counts.vitals;document.getElementById('sum-temperature').textContent=counts.temperature;document.getElementById('sum-weight').textContent=counts.weight;document.getElementById('sum-food').textContent=counts.food;document.getElementById('sum-total').textContent=counts.glucose+counts.vitals+counts.temperature+counts.weight+counts.food;
  }).catch(function(){box.innerHTML='<div class="muted">Не удалось загрузить последние записи</div>';});
}
loadDashboard();
    var csrf = document.querySelector('meta[name="csrf-token"]').content;

function applySettings(s) {
  var map = { glucose: 'card-glucose', vitals: 'card-vitals', temperature: 'card-temperature', weight: 'card-weight', food: 'card-food' };
  for (var k in map) {
    var el = document.getElementById(map[k]);
    if (el) { el.style.display = s[k] ? '' : 'none'; }
  }
  var cg = document.getElementById('set-glucose'); if (cg) { cg.checked = !!s.glucose; }
  var cv = document.getElementById('set-vitals'); if (cv) { cv.checked = !!s.vitals; }
  var ct = document.getElementById('set-temperature'); if (ct) { ct.checked = !!s.temperature; }
  var cw = document.getElementById('set-weight'); if (cw) { cw.checked = !!s.weight; }
  var cf = document.getElementById('set-food'); if (cf) { cf.checked = !!s.food; }
  var cai = document.getElementById('set-ai-enabled'); if (cai) { cai.checked = s.ai_enabled !== false; }
}

function bindSettings() {
  ['glucose', 'vitals', 'temperature', 'weight', 'food', 'ai_enabled'].forEach(function(k) {
    var el = document.getElementById('set-' + k);
    if (!el) { return; }
    el.addEventListener('change', function() {
      USER_SETTINGS[k] = el.checked;
      applySettings(USER_SETTINGS);
      sendJSON('POST', '/api/settings', USER_SETTINGS).then(function() {
        setMsg(k === 'ai_enabled' ? 'ai-msg' : 'settings-msg', 'Сохранено', true);
        if (k === 'ai_enabled') { updateAiStatus(); }
      }).catch(function(err) {
        setMsg(k === 'ai_enabled' ? 'ai-msg' : 'settings-msg', friendlyErrorMessage(err), false);
      });
    });
  });
}

applySettings(USER_SETTINGS || {});
bindSettings();

async function testAi() {
  var msg = document.getElementById('ai-msg');
  if (msg) { msg.textContent = '⏳ Выполняется реальный тест GigaChat…'; msg.className = 'message'; }
  try {
    var res = await fetch('/api/ai-test', {
      method: 'POST',
      headers: { 'X-CSRF-Token': csrf }
    });
    var out = await res.json();
    if (!res.ok || !out.ok) {
      throw new Error(out.message || ('HTTP ' + res.status));
    }
    if (msg) {
      msg.textContent = '🟢 GigaChat отвечает: ' + out.answer + ' · ' + (out.model || GIGACHAT_MODEL);
      msg.className = 'message success';
    }
    updateAiStatus();
  } catch (err) {
    if (msg) {
      msg.textContent = '🔴 ' + friendlyErrorMessage(err);
      msg.className = 'message error';
    }
  }
}

async function updateAiStatus() {
  var box = document.getElementById('ai-status');
  var toggle = document.getElementById('set-ai-enabled');
  if (!box) return;
  if (toggle && !toggle.checked) {
    box.textContent = '⚪ ИИ отключён пользователем';
    return;
  }
  box.textContent = '⏳ Проверка доступности ИИ…';
  try {
    var res = await fetch('/api/ai-status');
    var out = await res.json();
    if (out.available) {
      box.textContent = '🟢 ИИ: GigaChat доступен · ' + (out.model || 'GigaChat');
    } else {
      box.textContent = (out.code === 'disabled' || out.code === 'missing_key')
        ? '⚪ ИИ: ' + (out.message || 'не настроен')
        : '🔴 ИИ: ' + (out.message || 'недоступен');
    }
  } catch (err) {
    box.textContent = '🔴 ИИ: недоступен';
  }
}
updateAiStatus();

var THEME_KEY = 'medical_diary_theme';

function isThemeDark() {
  var stored = null;
  try { stored = localStorage.getItem(THEME_KEY); } catch (e) {}
  if (stored) { return stored === 'dark'; }
  return !!(window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches);
}

function applyTheme() {
  var dark = isThemeDark();
  document.documentElement.classList.toggle('theme-dark', dark);
  var toggle = document.getElementById('theme-toggle');
  if (toggle) { toggle.checked = dark; }
}

function onThemeToggleChange(el) {
  try { localStorage.setItem(THEME_KEY, el.checked ? 'dark' : 'light'); } catch (e) {}
  applyTheme();
}

applyTheme();
if (window.matchMedia) {
  // Старые Safari/iOS используют addListener вместо addEventListener.
  var themeMedia = window.matchMedia('(prefers-color-scheme: dark)');
  var onThemeMediaChange = function() {
    var stored = null;
    try { stored = localStorage.getItem(THEME_KEY); } catch (e) {}
    if (!stored) { applyTheme(); }
  };
  try {
    if (themeMedia.addEventListener) { themeMedia.addEventListener('change', onThemeMediaChange); }
    else if (themeMedia.addListener) { themeMedia.addListener(onThemeMediaChange); }
  } catch (e) {}
}

function showToast(text, ok) {
  var bubble = document.getElementById('toast-bubble');
  if (!bubble) return;
  bubble.textContent = text;
  bubble.className = 'toast-bubble show ' + (ok ? 'ok' : 'error');
  if (ok && navigator.vibrate) { try { navigator.vibrate(15); } catch (e) {} }
  clearTimeout(showToast._t);
  showToast._t = setTimeout(function() { bubble.className = 'toast-bubble'; }, 2200);
}

var TAB_PAGES = ['input', 'history', 'settings'];
function showPage(name) {
  TAB_PAGES.forEach(function(p) {
    var pageEl = document.getElementById('page-' + p);
    var btnEl = document.getElementById('tab-btn-' + p);
    if (pageEl) { pageEl.hidden = (p !== name); }
    if (btnEl) { btnEl.classList.toggle('active', p === name); btnEl.setAttribute('aria-current', p === name ? 'page' : 'false'); }
  });
  try { localStorage.setItem('medical_diary_active_tab', name); } catch (e) {}
  if (name === 'history') { loadHistory(); }
}
(function() {
  var saved = 'input';
  try { saved = localStorage.getItem('medical_diary_active_tab') || 'input'; } catch (e) {}
  if (TAB_PAGES.indexOf(saved) === -1) { saved = 'input'; }
  showPage(saved);
})();

function fmtRangeVal(n) {
  return (Math.round(n * 10) / 10).toString();
}

function updateGlucoseHint() {
  var el = document.getElementById('hint-glucose');
  if (!el) return;
  var checked = document.querySelector('input[name="glucose_type"]:checked');
  var key = (checked && checked.value === 'post_meal') ? 'glucose_post' : 'glucose_fasting';
  var r = RANGES[key];
  el.textContent = r ? ('Обычно ' + fmtRangeVal(r[0]) + '–' + fmtRangeVal(r[1]) + ' ммоль/л') : '';
}
document.querySelectorAll('input[name="glucose_type"]').forEach(function(el) {
  el.addEventListener('change', updateGlucoseHint);
});
updateGlucoseHint();

function updateVitalsHints() {
  var pairs = [['hint-systolic', 'systolic'], ['hint-diastolic', 'diastolic'], ['hint-pulse', 'pulse']];
  pairs.forEach(function(pair) {
    var el = document.getElementById(pair[0]);
    var r = RANGES[pair[1]];
    if (el && r) { el.textContent = 'Обычно ' + fmtRangeVal(r[0]) + '–' + fmtRangeVal(r[1]); }
  });
}
updateVitalsHints();


function fmtDateRuLong(day) {
  var months = ['января','февраля','марта','апреля','мая','июня','июля','августа','сентября','октября','ноября','декабря'];
  try { var d = new Date(day + 'T00:00:00'); return d.getDate() + ' ' + months[d.getMonth()] + ' ' + d.getFullYear() + ' г.'; } catch(e) { return day; }
}

function relativeDayLabel(measuredAt) {
  if (!measuredAt) { return ''; }
  var day = measuredAt.substring(0, 10);
  var time = measuredAt.substring(11, 16);
  var dayLabel;
  if (day === localDate(0)) { dayLabel = 'сегодня'; }
  else if (day === localDate(-1)) { dayLabel = 'вчера'; }
  else { dayLabel = fmtDateLabel(day); }
  return dayLabel + ', ' + time;
}

async function loadOneLastStatus(type) {
  var el = document.getElementById('last-status-' + type);
  if (!el) { return; }
  try {
    var res = await fetch('/api/last?type=' + type);
    var out = await res.json();
    if (!res.ok) { throw new Error(out.error || 'HTTP ' + res.status); }
    if (!out.found) { el.textContent = 'Записей ещё не было'; return; }

    var summary;
    if (type === 'glucose') {
      summary = out.value.toFixed(1) + ' ммоль/л (' + (out.glucose_type === 'fasting' ? 'натощак' : 'после еды') + ')';
    } else if (type === 'vitals') {
      summary = out.systolic + '/' + out.diastolic + (out.pulse != null ? ', пульс ' + out.pulse : '');
    } else if (type === 'temperature') {
      summary = Number(out.value).toFixed(1) + ' °C';
    } else if (type === 'weight') {
      summary = Number(out.value).toFixed(1) + ' кг';
    } else {
      summary = out.food_name + ', ' + out.amount_value + ' ' + out.amount_unit;
    }
    el.textContent = 'Последняя запись: ' + relativeDayLabel(out.measured_at) + ' — ' + summary;
  } catch (err) {
    el.textContent = '';
  }
}

function loadLastEntryStatuses() {
  loadOneLastStatus('glucose');
  loadOneLastStatus('vitals');
  loadOneLastStatus('temperature');
  loadOneLastStatus('weight');
  loadOneLastStatus('food');
}
loadLastEntryStatuses();

var RANGE_KEYS = ['glucose_fasting', 'glucose_post', 'systolic', 'diastolic', 'pulse'];

function setRangeInputsDisabled(disabled) {
  RANGE_KEYS.forEach(function(k) {
    var lowEl = document.getElementById('range-' + k + '-low');
    var highEl = document.getElementById('range-' + k + '-high');
    if (lowEl) { lowEl.disabled = disabled; }
    if (highEl) { highEl.disabled = disabled; }
  });
}

function fillRangeInputsFrom(rangesObj) {
  RANGE_KEYS.forEach(function(k) {
    var r = rangesObj[k];
    if (!r) { return; }
    var lowEl = document.getElementById('range-' + k + '-low');
    var highEl = document.getElementById('range-' + k + '-high');
    if (lowEl) { lowEl.value = r[0]; }
    if (highEl) { highEl.value = r[1]; }
  });
}

function populateRangeInputs() {
  fillRangeInputsFrom(RANGES);
  var toggle = document.getElementById('ranges-default-toggle');
  var usingDefault = USER_SETTINGS ? (USER_SETTINGS.ranges_default !== false) : true;
  if (toggle) { toggle.checked = usingDefault; }
  setRangeInputsDisabled(usingDefault);
}
populateRangeInputs();

// Поля для дробных чисел сделаны type="text" вместо type="number",
// потому что нативный number-инпут не принимает запятую как десятичный
// разделитель ни при какой локали — а это стандартный способ ввода
// дробей на русской клавиатуре. Здесь мягко приводим ввод к пригодному
// виду (не более одного разделителя, только цифры), а запятую сервер и
// так понимает (parse_float на бэкенде уже заменяет её на точку).
document.querySelectorAll('.decimal-input').forEach(function(el) {
  el.addEventListener('input', function() {
    var before = el.value;
    var cleaned = before.replace(/[^0-9.,]/g, '');
    var sepIndex = cleaned.search(/[.,]/);
    if (sepIndex !== -1) {
      cleaned = cleaned.slice(0, sepIndex + 1) + cleaned.slice(sepIndex + 1).replace(/[.,]/g, '');
    }
    if (cleaned !== before) {
      var pos = el.selectionStart - (before.length - cleaned.length);
      el.value = cleaned;
      try { el.setSelectionRange(Math.max(0, pos), Math.max(0, pos)); } catch (e) {}
    }
  });
});

function parseDecimal(str) {
  if (str == null) { return NaN; }
  return parseFloat(String(str).trim().replace(',', '.'));
}

function collectRangeInputs() {
  var ranges = {};
  for (var i = 0; i < RANGE_KEYS.length; i++) {
    var k = RANGE_KEYS[i];
    var low = parseDecimal(document.getElementById('range-' + k + '-low').value);
    var high = parseDecimal(document.getElementById('range-' + k + '-high').value);
    if (isNaN(low) || isNaN(high)) { return null; }
    ranges[k] = [low, high];
  }
  return ranges;
}

async function persistSettings(payload) {
  try {
    var out = await sendJSON('POST', '/api/settings', payload);
    if (out.ranges) {
      RANGES = out.ranges;
      updateGlucoseHint();
      updateVitalsHints();
    }
    if (out.settings && ('ranges_default' in out.settings)) {
      USER_SETTINGS.ranges_default = out.settings.ranges_default;
    }
    setMsg('ranges-msg', 'Сохранено', true);
    showToast('Сохранено', true);
  } catch (err) {
    setMsg('ranges-msg', friendlyErrorMessage(err), false);
    showToast(friendlyErrorMessage(err), false);
  }
}

var scheduleSaveRangesTimer = null;
function scheduleSaveRanges() {
  clearTimeout(scheduleSaveRangesTimer);
  scheduleSaveRangesTimer = setTimeout(function() {
    var ranges = collectRangeInputs();
    if (!ranges) {
      setMsg('ranges-msg', 'Заполните оба значения для всех диапазонов', false);
      return;
    }
    persistSettings({ ranges: ranges, ranges_default: false });
  }, 700);
}

function onRangesDefaultToggleChange(el) {
  if (el.checked) {
    // Не отправляем "ranges" вовсе — сохранённые персональные значения
    // на сервере остаются нетронутыми, просто временно не используются.
    fillRangeInputsFrom(DEFAULT_RANGES_JS);
    setRangeInputsDisabled(true);
    RANGES = JSON.parse(JSON.stringify(DEFAULT_RANGES_JS));
    updateGlucoseHint();
    updateVitalsHints();
    persistSettings({ ranges_default: true });
  } else {
    setRangeInputsDisabled(false);
    var ranges = collectRangeInputs();
    persistSettings({ ranges: ranges, ranges_default: false });
  }
}

    function pad(n) { return String(n).padStart(2, '0'); }

    function localDateTime() {
      var d = new Date();
      return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + 'T' + pad(d.getHours()) + ':' + pad(d.getMinutes());
    }

    function localDate(offsetDays) {
      var d = new Date();
      d.setDate(d.getDate() + offsetDays);
      return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
    }

    function fmtDateLabel(v) {
      if (!v) return '';
      var p = v.split('-');
      return p[2] + '.' + p[1] + '.' + p[0];
    }

    var MONTHS_RU_SHORT = ['янв.', 'февр.', 'мар.', 'апр.', 'мая', 'июня', 'июля', 'авг.', 'сент.', 'окт.', 'нояб.', 'дек.'];
function fmtDateTimeRu(v) {
  if (!v) return '';
  var d = v.substring(0, 10).split('-');
  var t = v.substring(11, 16);
  var m = parseInt(d[1], 10) - 1;
  return d[2] + ' ' + MONTHS_RU_SHORT[m] + ' ' + d[0].substring(2) + ' г. ' + t;
}

function updateDateLabels() {
      document.getElementById('date_from_label').textContent = fmtDateLabel(document.getElementById('date_from').value);
      document.getElementById('date_to_label').textContent = fmtDateLabel(document.getElementById('date_to').value);
    }

    var HISTORY_FILTERS_KEY = 'medical_diary_history_filters';

    function saveHistoryFilters() {
      try {
        localStorage.setItem(HISTORY_FILTERS_KEY, JSON.stringify({
          date_from: document.getElementById('date_from').value,
          date_to: document.getElementById('date_to').value,
          type: document.getElementById('history_type').value,
          sort: document.querySelector('input[name="history_sort"]:checked').value
        }));
      } catch (e) {}
    }

    function highlightActivePreset() {
      var df = document.getElementById('date_from').value;
      var dt = document.getElementById('date_to').value;
      var presetDays = [7, 30, 90, 365];
      document.querySelectorAll('.preset-btn').forEach(function(btn, i) {
        var days = presetDays[i];
        var matches = !!days && dt === localDate(0) && df === localDate(-days + 1);
        btn.classList.toggle('active', matches);
      });
    }

    function setDateRangePreset(days) {
      document.getElementById('date_from').value = localDate(-days + 1);
      document.getElementById('date_to').value = localDate(0);
      updateDateLabels();
      saveHistoryFilters();
      loadHistory();
      highlightActivePreset();
    }

    (function restoreHistoryFilters() {
      var saved = null;
      try { saved = JSON.parse(localStorage.getItem(HISTORY_FILTERS_KEY) || 'null'); } catch (e) {}

      document.querySelectorAll('.dt').forEach(function(el) { el.value = localDateTime(); });
      document.getElementById('date_from').value = (saved && saved.date_from) || localDate(-6);
      document.getElementById('date_to').value = (saved && saved.date_to) || localDate(0);
      if (saved && saved.type) { document.getElementById('history_type').value = saved.type; }
      if (saved && saved.sort) {
        var radio = document.querySelector('input[name="history_sort"][value="' + saved.sort + '"]');
        if (radio) { radio.checked = true; }
      }
      updateDateLabels();
      highlightActivePreset();
    })();

    document.getElementById('date_from').addEventListener('change', function() { updateDateLabels(); saveHistoryFilters(); loadHistory(); highlightActivePreset(); });
    document.getElementById('date_to').addEventListener('change', function() { updateDateLabels(); saveHistoryFilters(); loadHistory(); highlightActivePreset(); });
    document.getElementById('history_type').addEventListener('change', function() { saveHistoryFilters(); loadHistory(); });
    document.querySelectorAll('input[name="history_sort"]').forEach(function(el) {
      el.addEventListener('change', function() { saveHistoryFilters(); loadHistory(); });
    });

    function setMsg(id, text, ok) {
      var el = document.getElementById(id);
      if (!el) return;
      el.textContent = text;
      el.className = 'message ' + (ok ? 'ok' : 'error');
    }

    function friendlyErrorMessage(err) {
      // fetch() отклоняет промис с TypeError при обрыве сети/офлайне —
      // технический текст вроде "Failed to fetch" пользователю ничего не
      // скажет, поэтому подменяем его на понятное сообщение.
      if (err instanceof TypeError || (err && /fetch/i.test(err.message || ''))) {
        return 'Нет соединения с сервером — проверьте интернет и попробуйте ещё раз.';
      }
      return (err && err.message) || 'Неизвестная ошибка';
    }

    async function postJSON(url, data, extraHeaders) {
      var res;
      try {
        res = await fetch(url, {
          method: 'POST',
          headers: Object.assign({ 'Content-Type': 'application/json', 'X-CSRF-Token': csrf }, extraHeaders || {}),
          body: JSON.stringify(data)
        });
      } catch (err) {
        throw new Error(friendlyErrorMessage(err));
      }

      if (res.status === 401) { window.location = '/login'; throw new Error('Требуется вход'); }

      var out = {};
      try { out = await res.json(); } catch (e) {}

      if (!res.ok) { throw new Error(out.error || ('HTTP ' + res.status)); }
      return out;
    }

    function genIdemKey() {
      if (window.crypto && window.crypto.randomUUID) { return window.crypto.randomUUID(); }
      return 'idem-' + Date.now() + '-' + Math.random().toString(36).slice(2);
    }

    document.getElementById('glucose-form').addEventListener('submit', async function(e) {
      e.preventDefault();
      var f = e.target;
      var btn = f.querySelector('button[type="submit"]');
      if (btn) { btn.disabled = true; }
      try {
        await postJSON('/api/glucose', {
          glucose_type: f.glucose_type.value,
          value: f.value.value,
          measured_at: f.measured_at.value.replace('T', ' '),
          comment: f.comment.value
        }, { 'Idempotency-Key': genIdemKey() });
        setMsg('glucose-msg', '', true);
        showToast('Запись глюкозы сохранена', true);
        f.value.value = '';
        f.comment.value = '';
        closeEntry();
        loadHistory();
        loadLastEntryStatuses();
      } catch (err) { setMsg('glucose-msg', friendlyErrorMessage(err), false); showToast(friendlyErrorMessage(err), false); }
      finally { if (btn) { btn.disabled = false; } }
    });

    document.getElementById('vitals-form').addEventListener('submit', async function(e) {
      e.preventDefault();
      var f = e.target;
      var btn = f.querySelector('button[type="submit"]');
      if (btn) { btn.disabled = true; }
      try {
        await postJSON('/api/vitals', {
          systolic: f.systolic.value,
          diastolic: f.diastolic.value,
          pulse: f.pulse.value,
          measured_at: f.measured_at.value.replace('T', ' '),
          comment: f.comment.value
        }, { 'Idempotency-Key': genIdemKey() });
        setMsg('vitals-msg', '', true);
        showToast('Запись давления/пульса сохранена', true);
        f.comment.value = '';
        closeEntry();
        loadHistory();
        loadLastEntryStatuses();
      } catch (err) { setMsg('vitals-msg', friendlyErrorMessage(err), false); showToast(friendlyErrorMessage(err), false); }
      finally { if (btn) { btn.disabled = false; } }
    });

    document.getElementById('temperature-form').addEventListener('submit', async function(e) {
      e.preventDefault();
      var f = e.target;
      var btn = f.querySelector('button[type="submit"]');
      if (btn) { btn.disabled = true; }
      try {
        await postJSON('/api/temperature', {
          value: f.value.value,
          measured_at: f.measured_at.value.replace('T', ' '),
          comment: f.comment.value
        }, { 'Idempotency-Key': genIdemKey() });
        setMsg('temperature-msg', '', true);
        showToast('Запись температуры сохранена', true);
        f.value.value = '';
        f.comment.value = '';
        closeEntry();
        loadHistory();
        loadLastEntryStatuses();
      } catch (err) { setMsg('temperature-msg', friendlyErrorMessage(err), false); showToast(friendlyErrorMessage(err), false); }
      finally { if (btn) { btn.disabled = false; } }
    });

    document.getElementById('weight-form').addEventListener('submit', async function(e) {
      e.preventDefault();
      var f = e.target;
      var btn = f.querySelector('button[type="submit"]');
      if (btn) { btn.disabled = true; }
      try {
        await postJSON('/api/weight', {
          value: f.value.value,
          measured_at: f.measured_at.value.replace('T', ' '),
          comment: f.comment.value
        }, { 'Idempotency-Key': genIdemKey() });
        setMsg('weight-msg', '', true);
        showToast('Запись веса сохранена', true);
        f.value.value = '';
        f.comment.value = '';
        closeEntry();
        loadHistory();
        loadLastEntryStatuses();
      } catch (err) { setMsg('weight-msg', friendlyErrorMessage(err), false); showToast(friendlyErrorMessage(err), false); }
      finally { if (btn) { btn.disabled = false; } }
    });

    document.getElementById('food-form').addEventListener('submit', async function(e) {
      e.preventDefault();
      var f = e.target;
      var btn = f.querySelector('button[type="submit"]');
      if (btn) { btn.disabled = true; }
      try {
        await postJSON('/api/food', {
          food_name: f.food_name.value,
          amount_value: f.amount_value.value,
          amount_unit: f.amount_unit.value,
          consumed_at: f.consumed_at.value.replace('T', ' '),
          comment: f.comment.value
        }, { 'Idempotency-Key': genIdemKey() });
        setMsg('food-msg', '', true);
        showToast('Запись о питании сохранена', true);
        f.food_name.value = '';
        f.amount_value.value = '';
        f.comment.value = '';
        closeEntry();
        loadHistory();
        loadLastEntryStatuses();
      } catch (err) { setMsg('food-msg', friendlyErrorMessage(err), false); showToast(friendlyErrorMessage(err), false); }
      finally { if (btn) { btn.disabled = false; } }
    });

    function historyParams() {
      return {
        df: document.getElementById('date_from').value,
        dt: document.getElementById('date_to').value,
        type: document.getElementById('history_type').value,
        sort: document.querySelector('input[name="history_sort"]:checked').value
      };
    }

    function fmtDateRu(v) {
      if (!v) return '';
      var d = v.substring(0, 10).split('-');
      var m = parseInt(d[1], 10) - 1;
      return d[2] + ' ' + MONTHS_RU_SHORT[m] + ' ' + d[0].substring(2) + ' г.';
    }

    var STATUS_LINE_COLORS = { ok: '#007aff', low: '#ff3b30', high: '#ff3b30' };
    var BAND_COLOR_OUTER = 'rgba(180,235,150,0.40)';
    var BAND_COLOR_INNER = 'rgba(140,220,130,0.55)';

    function fmtDayTick(ts) {
      var d = new Date(ts);
      return pad(d.getDate()) + '.' + pad(d.getMonth() + 1);
    }

    function drawLineChart(canvas, series, bands) {
      if (!canvas) return;
      var dpr = window.devicePixelRatio || 1;
      var cssWidth = canvas.clientWidth || (canvas.parentElement && canvas.parentElement.clientWidth) || 300;
      var cssHeight = 150;
      canvas.width = cssWidth * dpr;
      canvas.height = cssHeight * dpr;
      canvas.style.height = cssHeight + 'px';
      var ctx = canvas.getContext('2d');
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, cssWidth, cssHeight);

      var allPoints = [];
      series.forEach(function(s) { allPoints = allPoints.concat(s.points); });
      if (allPoints.length === 0) { return; }

      var xs = allPoints.map(function(p) { return p.x; });
      var ys = allPoints.map(function(p) { return p.y; });
      (bands || []).forEach(function(b) { ys.push(b.low); ys.push(b.high); });
      var minX = Math.min.apply(null, xs), maxX = Math.max.apply(null, xs);
      var minY = Math.min.apply(null, ys), maxY = Math.max.apply(null, ys);
      if (minX === maxX) { minX -= 1; maxX += 1; }
      if (minY === maxY) { minY -= 1; maxY += 1; }
      var padY = (maxY - minY) * 0.15;
      minY -= padY; maxY += padY;

      var spanDays = (maxX - minX) / 86400000;
      var padding = { left: 34, right: 8, top: 10, bottom: 18 };
      var plotW = cssWidth - padding.left - padding.right;
      var plotH = cssHeight - padding.top - padding.bottom;
      function px(x) { return padding.left + (x - minX) / (maxX - minX) * plotW; }
      function py(y) { return padding.top + (1 - (y - minY) / (maxY - minY)) * plotH; }

      (bands || []).forEach(function(b) {
        var yTop = py(b.high), yBot = py(b.low);
        ctx.fillStyle = b.color;
        ctx.fillRect(padding.left, yTop, plotW, Math.max(1, yBot - yTop));
      });

      ctx.strokeStyle = '#e5e5ea';
      ctx.lineWidth = 1;
      ctx.fillStyle = '#8e8e93';
      ctx.font = '11px -apple-system, BlinkMacSystemFont, sans-serif';
      for (var i = 0; i <= 2; i++) {
        var val = minY + (maxY - minY) * i / 2;
        var yy = py(val);
        ctx.beginPath();
        ctx.moveTo(padding.left, yy);
        ctx.lineTo(cssWidth - padding.right, yy);
        ctx.stroke();
        ctx.fillText(fmtRangeVal(val), 2, yy + 4);
      }

      series.forEach(function(s) {
        if (s.points.length === 0) return;
        ctx.lineWidth = 2;
        for (var j = 0; j < s.points.length - 1; j++) {
          var p1 = s.points[j], p2 = s.points[j + 1];
          ctx.strokeStyle = STATUS_LINE_COLORS[p1.status] || '#007aff';
          ctx.beginPath();
          ctx.moveTo(px(p1.x), py(p1.y));
          ctx.lineTo(px(p2.x), py(p2.y));
          ctx.stroke();
        }
        s.points.forEach(function(p) {
          ctx.fillStyle = STATUS_LINE_COLORS[p.status] || '#007aff';
          ctx.beginPath();
          if (s.shape === 'square') {
            ctx.fillRect(px(p.x) - 3, py(p.y) - 3, 6, 6);
          } else {
            ctx.arc(px(p.x), py(p.y), 3, 0, 2 * Math.PI);
            ctx.fill();
          }
        });
      });

      ctx.fillStyle = '#8e8e93';
      if (spanDays <= 10) {
        // Короткий период — подписываем каждый день на оси X.
        var dayCount = Math.round(spanDays) + 1;
        var oneDay = 86400000;
        var startDay = new Date(minX); startDay.setHours(0, 0, 0, 0);
        var lastLabelX = -1000;
        for (var d = 0; d < dayCount; d++) {
          var ts = startDay.getTime() + d * oneDay;
          if (ts < minX - oneDay || ts > maxX + oneDay) { continue; }
          var xx = px(Math.max(minX, Math.min(maxX, ts)));
          if (xx - lastLabelX < 30) { continue; }
          lastLabelX = xx;
          ctx.textAlign = 'center';
          ctx.fillText(fmtDayTick(ts), Math.min(Math.max(xx, padding.left + 14), cssWidth - padding.right - 14), cssHeight - 4);
        }
      } else {
        ctx.textAlign = 'left';
        ctx.fillText(fmtDayTick(minX), padding.left, cssHeight - 4);
        ctx.textAlign = 'right';
        ctx.fillText(fmtDayTick(maxX), cssWidth - padding.right, cssHeight - 4);
      }
      ctx.textAlign = 'left';
    }

    function toTimestamp(v) {
      return new Date(v.replace(' ', 'T')).getTime();
    }

    function renderTrendCharts(entries) {
      var fastingPoints = entries
        .filter(function(e) { return e.type === 'glucose' && e.glucose_type === 'fasting'; })
        .map(function(e) { return { x: toTimestamp(e.measured_at), y: e.value_mmol_l, status: e.status }; })
        .sort(function(a, b) { return a.x - b.x; });
      var postPoints = entries
        .filter(function(e) { return e.type === 'glucose' && e.glucose_type === 'post_meal'; })
        .map(function(e) { return { x: toTimestamp(e.measured_at), y: e.value_mmol_l, status: e.status }; })
        .sort(function(a, b) { return a.x - b.x; });

      var gWrap = document.getElementById('trend-glucose-wrap');
      var hasGlucose = (fastingPoints.length + postPoints.length) >= 2;
      if (gWrap) { gWrap.hidden = !hasGlucose; }
      if (hasGlucose) {
        var gBands = [];
        if (RANGES.glucose_post) { gBands.push({ low: RANGES.glucose_post[0], high: RANGES.glucose_post[1], color: BAND_COLOR_OUTER }); }
        if (RANGES.glucose_fasting) { gBands.push({ low: RANGES.glucose_fasting[0], high: RANGES.glucose_fasting[1], color: BAND_COLOR_INNER }); }
        drawLineChart(document.getElementById('trend-glucose'), [
          { points: fastingPoints, shape: 'circle' },
          { points: postPoints, shape: 'square' }
        ], gBands);
      }

      var sysPoints = [], diaPoints = [];
      entries.filter(function(e) { return e.type === 'vitals'; }).forEach(function(e) {
        var t = toTimestamp(e.measured_at);
        sysPoints.push({ x: t, y: e.systolic_mmhg, status: e.systolic_status });
        diaPoints.push({ x: t, y: e.diastolic_mmhg, status: e.diastolic_status });
      });
      sysPoints.sort(function(a, b) { return a.x - b.x; });
      diaPoints.sort(function(a, b) { return a.x - b.x; });

      var vWrap = document.getElementById('trend-vitals-wrap');
      var hasVitals = sysPoints.length >= 2;
      if (vWrap) { vWrap.hidden = !hasVitals; }
      if (hasVitals) {
        var vBands = [];
        if (RANGES.systolic) { vBands.push({ low: RANGES.systolic[0], high: RANGES.systolic[1], color: BAND_COLOR_OUTER }); }
        if (RANGES.diastolic) { vBands.push({ low: RANGES.diastolic[0], high: RANGES.diastolic[1], color: BAND_COLOR_INNER }); }
        drawLineChart(document.getElementById('trend-vitals'), [
          { points: sysPoints, shape: 'circle' },
          { points: diaPoints, shape: 'square' }
        ], vBands);
      }
      var tempPoints = entries.filter(function(e) { return e.type === 'temperature'; }).map(function(e) { return { x: toTimestamp(e.measured_at), y: e.temperature_c, status: 'ok' }; }).sort(function(a,b){ return a.x-b.x; });
      var tWrap = document.getElementById('trend-temperature-wrap');
      var hasTemperature = tempPoints.length >= 2;
      if (tWrap) { tWrap.hidden = !hasTemperature; }
      if (hasTemperature) { drawLineChart(document.getElementById('trend-temperature'), [{ points: tempPoints, shape: 'circle' }], []); }

      var weightPoints = entries.filter(function(e) { return e.type === 'weight'; }).map(function(e) { return { x: toTimestamp(e.measured_at), y: e.weight_kg, status: 'ok' }; }).sort(function(a,b){ return a.x-b.x; });
      var wWrap = document.getElementById('trend-weight-wrap');
      var hasWeight = weightPoints.length >= 2;
      if (wWrap) { wWrap.hidden = !hasWeight; }
      if (hasWeight) { drawLineChart(document.getElementById('trend-weight'), [{ points: weightPoints, shape: 'circle' }], []); }
    }

    async function loadHistory() {
      var hp = historyParams();
      var df = hp.df;
      var dt = hp.dt;
      var cards = document.getElementById('history-cards');
      if (cards) { cards.innerHTML = '<div class="muted">Загрузка…</div>'; }
      try {
        var res = await fetch('/api/history?date_from=' + encodeURIComponent(df) + '&date_to=' + encodeURIComponent(dt) + '&type=' + encodeURIComponent(hp.type) + '&sort=' + encodeURIComponent(hp.sort));
        if (res.status === 401) { window.location = '/login'; return; }
        var out = await res.json();
        if (!res.ok) { throw new Error(out.error || ('HTTP ' + res.status)); }

        if (cards) { cards.innerHTML = ''; }
        var lastDay = null;
        var groupByDay = (hp.sort === 'date');

        function addDay(day) {
          if (!cards) return;
          var heading = document.createElement('div');
          heading.className = 'history-day-title';
          heading.innerHTML = '<span>' + fmtDateRuLong(day) + '</span>';
          var today = localDate(0), yesterday = localDate(-1);
          if (day === today) { heading.innerHTML += '<span class="day-chip">Сегодня</span>'; }
          else if (day === yesterday) { heading.innerHTML += '<span class="day-chip">Вчера</span>'; }
          cards.appendChild(heading);
        }

        function makeActionButton(text, cls, label, handler) {
          var b = document.createElement('button'); b.type='button'; b.className='icon-btn' + (cls ? ' '+cls : '');
          b.textContent=text; b.setAttribute('aria-label', label); b.addEventListener('click', handler); return b;
        }
        function statusBadge(status, text) {
          var span = document.createElement('span'); span.className='status-badge status-' + (status || 'ok'); span.textContent=text; return span;
        }
        function appendComment(parent, text) {
          if (!text) return; var c=document.createElement('div'); c.className='comment-line'; c.textContent=text; parent.appendChild(c);
        }
        function renderGlucose(entry) {
          var card=document.createElement('article'); card.className='history-entry';
          var top=document.createElement('div'); top.className='history-entry-top';
          var icon=document.createElement('div'); icon.className='history-icon'; icon.textContent='💧'; top.appendChild(icon);
          var main=document.createElement('div'); main.className='history-entry-main';
          var title=document.createElement('div'); title.className='history-entry-title'; title.textContent='Глюкоза'; main.appendChild(title);
          var meta=document.createElement('div'); meta.className='history-entry-meta'; meta.textContent=(entry.glucose_type === 'fasting' ? 'Натощак' : 'После еды') + ' · ' + (entry.measured_at ? entry.measured_at.substring(11,16) : ''); main.appendChild(meta); top.appendChild(main); card.appendChild(top);
          var val=document.createElement('div'); val.className='history-value';
          var num=document.createElement('span'); num.className='number'; num.textContent=Number(entry.value_mmol_l).toFixed(1); val.appendChild(num);
          var unit=document.createElement('span'); unit.className='unit'; unit.textContent='ммоль/л'; val.appendChild(unit);
          card.appendChild(val);
          var text = entry.status === 'high' ? '▲ Выше диапазона' : entry.status === 'low' ? '▼ Ниже диапазона' : '✓ В пределах диапазона';
          card.appendChild(statusBadge(entry.status, text)); appendComment(card, entry.comment);
          return card;
        }
        function renderVitals(entry) {
          var card=document.createElement('article'); card.className='history-entry';
          var top=document.createElement('div'); top.className='history-entry-top';
          var icon=document.createElement('div'); icon.className='history-icon'; icon.textContent='💓'; top.appendChild(icon);
          var main=document.createElement('div'); main.className='history-entry-main';
          var title=document.createElement('div'); title.className='history-entry-title'; title.textContent='Давление и пульс'; main.appendChild(title);
          var meta=document.createElement('div'); meta.className='history-entry-meta'; meta.textContent=(entry.measured_at ? entry.measured_at.substring(11,16) : ''); main.appendChild(meta); top.appendChild(main); card.appendChild(top);
          var vals=document.createElement('div'); vals.className='vitals-values';
          var bp=document.createElement('div'); var lab=document.createElement('div'); lab.className='vital-label'; lab.textContent='Давление'; bp.appendChild(lab); var pn=document.createElement('div'); pn.className='pressure-number'; pn.textContent=entry.systolic_mmhg + ' / ' + entry.diastolic_mmhg; bp.appendChild(pn); var pu=document.createElement('div'); pu.className='pressure-unit'; pu.textContent='мм рт. ст.'; bp.appendChild(pu); vals.appendChild(bp);
          var div=document.createElement('div'); div.className='vitals-divider'; vals.appendChild(div);
          var pulse=document.createElement('div'); var pl=document.createElement('div'); pl.className='vital-label'; pl.textContent='Пульс'; pulse.appendChild(pl); var pnum=document.createElement('div'); pnum.className='pressure-number'; pnum.textContent=entry.pulse_bpm == null ? '—' : entry.pulse_bpm; pulse.appendChild(pnum); var punit=document.createElement('div'); punit.className='pulse-unit'; punit.textContent='уд/мин'; pulse.appendChild(punit); vals.appendChild(pulse); card.appendChild(vals);
          var statuses=document.createElement('div'); statuses.className='vital-statuses';
          function addVitalStatus(st, labelText, caption){ var item=document.createElement('div'); item.className='vital-status-item'; item.appendChild(statusBadge(st, st==='high'?'▲ Выше':st==='low'?'▼ Ниже':'✓ В норме')); var cap=document.createElement('div'); cap.className='vital-caption'; cap.textContent=caption; item.appendChild(cap); statuses.appendChild(item); }
          addVitalStatus(entry.systolic_status,'', 'систолическое'); addVitalStatus(entry.diastolic_status,'', 'диастолическое');
          if (entry.pulse_bpm != null) { addVitalStatus(entry.pulse_status || 'ok','', 'пульс'); } else { var empty=document.createElement('div'); empty.className='vital-status-item'; empty.innerHTML='<div class="vital-caption">Пульс не указан</div>'; statuses.appendChild(empty); }
          card.appendChild(statuses); appendComment(card, entry.comment);
          return card;
        }
        function renderTemperature(entry) {
          var card=document.createElement('article'); card.className='history-entry';
          var top=document.createElement('div'); top.className='history-entry-top';
          var icon=document.createElement('div'); icon.className='history-icon temperature'; icon.textContent='🌡️'; top.appendChild(icon);
          var main=document.createElement('div'); main.className='history-entry-main';
          var title=document.createElement('div'); title.className='history-entry-title'; title.textContent='Температура'; main.appendChild(title);
          var meta=document.createElement('div'); meta.className='history-entry-meta'; meta.textContent=(entry.measured_at ? entry.measured_at.substring(11,16) : ''); main.appendChild(meta); top.appendChild(main); card.appendChild(top);
          var val=document.createElement('div'); val.className='history-value';
          var num=document.createElement('span'); num.className='number'; num.textContent=Number(entry.temperature_c).toFixed(1); val.appendChild(num);
          var unit=document.createElement('span'); unit.className='unit'; unit.textContent='°C'; val.appendChild(unit); card.appendChild(val);
          appendComment(card, entry.comment); return card;
        }

        function renderWeight(entry) {
          var card=document.createElement('article'); card.className='history-entry';
          var top=document.createElement('div'); top.className='history-entry-top';
          var icon=document.createElement('div'); icon.className='history-icon weight'; icon.textContent='⚖️'; top.appendChild(icon);
          var main=document.createElement('div'); main.className='history-entry-main';
          var title=document.createElement('div'); title.className='history-entry-title'; title.textContent='Вес'; main.appendChild(title);
          var meta=document.createElement('div'); meta.className='history-entry-meta'; meta.textContent=(entry.measured_at ? entry.measured_at.substring(11,16) : ''); main.appendChild(meta); top.appendChild(main); card.appendChild(top);
          var val=document.createElement('div'); val.className='history-value';
          var num=document.createElement('span'); num.className='number'; num.textContent=Number(entry.weight_kg).toFixed(1); val.appendChild(num);
          var unit=document.createElement('span'); unit.className='unit'; unit.textContent='кг'; val.appendChild(unit); card.appendChild(val);
          appendComment(card, entry.comment); return card;
        }

        function renderFood(entry) {
          var card=document.createElement('article'); card.className='history-entry';
          var top=document.createElement('div'); top.className='history-entry-top'; var icon=document.createElement('div'); icon.className='history-icon'; icon.textContent='🥗'; top.appendChild(icon); var main=document.createElement('div'); main.className='history-entry-main'; var title=document.createElement('div'); title.className='history-entry-title'; title.textContent='Питание'; main.appendChild(title); var meta=document.createElement('div'); meta.className='history-entry-meta'; meta.textContent=(entry.measured_at ? entry.measured_at.substring(11,16) : ''); main.appendChild(meta); top.appendChild(main); card.appendChild(top);
          var line=document.createElement('div'); line.className='food-line'; line.textContent=entry.food_name + ' · '; var amount=document.createElement('span'); amount.className='food-amount'; amount.textContent=(Number(entry.amount_value).toLocaleString('ru-RU') + ' ' + unitRu(entry.amount_unit)); line.appendChild(amount); card.appendChild(line); appendComment(card, entry.comment); return card;
        }

        out.entries.forEach(function(entry) {
          if (groupByDay) { var day=(entry.measured_at||'').substring(0,10); if(day!==lastDay){lastDay=day; addDay(day);} }
          var card = entry.type==='glucose' ? renderGlucose(entry) : entry.type==='vitals' ? renderVitals(entry) : entry.type==='temperature' ? renderTemperature(entry) : entry.type==='weight' ? renderWeight(entry) : renderFood(entry);
          var actions=document.createElement('div'); actions.className='history-actions';
          actions.appendChild(makeActionButton('✏️','', 'Редактировать запись', function(){startEdit(entry);}));
          actions.appendChild(makeActionButton('🗑️','delete', 'Удалить запись', function(){deleteEntry(entry);})); card.appendChild(actions);
          if (cards) cards.appendChild(card);
        });

        if (out.entries.length === 0 && cards) { var empty=document.createElement('div'); empty.className='muted'; empty.style.padding='16px 2px'; empty.textContent='Нет записей за выбранный период'; cards.appendChild(empty); }
        var count=document.getElementById('history-count'); if(count) count.textContent='Записей: ' + out.entries.length;
        renderTrendCharts(out.entries);
        currentExportUrl = '/export.pdf?date_from=' + encodeURIComponent(df) + '&date_to=' + encodeURIComponent(dt) + '&type=' + encodeURIComponent(hp.type) + '&sort=' + encodeURIComponent(hp.sort);
        setMsg('history-msg', '', true);
      } catch (err) {
        if (cards) { cards.innerHTML=''; }
        var count=document.getElementById('history-count'); if(count) count.textContent='Записей: —';
        setMsg('history-msg', friendlyErrorMessage(err), false);
      }
    }

    async function loadUsers() {
      if (!IS_ADMIN) return;
      try {
        var res = await fetch('/api/admin/users');
        if (res.status === 401) { window.location = '/login'; return; }
        var out = await res.json();
        if (!res.ok) { throw new Error(out.error || ('HTTP ' + res.status)); }

        var tbody = document.querySelector('#users-table tbody');
        tbody.innerHTML = '';
        out.users.forEach(function(u) {
          var tr = document.createElement('tr');

          var td1 = document.createElement('td');
          td1.textContent = u.display_name || u.username;
          tr.appendChild(td1);

          var td2 = document.createElement('td');
          td2.textContent = u.username + (u.is_admin ? ' (админ)' : '');
          tr.appendChild(td2);

          var td3 = document.createElement('td');
          var wrap = document.createElement('div');
          wrap.className = 'cell-actions';

          var eb2 = document.createElement('button');
          eb2.type = 'button';
          eb2.className = 'edit-btn';
          eb2.textContent = '✏️';
          eb2.setAttribute('aria-label', 'Редактировать пользователя');
          eb2.addEventListener('click', function() { editUser(u); });
          wrap.appendChild(eb2);

          if (!u.is_admin) {
            var b = document.createElement('button');
            b.type = 'button';
            b.className = 'del-btn';
            b.textContent = '🗑️';
            b.setAttribute('aria-label', 'Удалить пользователя');
            b.addEventListener('click', function() { deleteUser(u.id, u.username); });
            wrap.appendChild(b);
          }

          td3.appendChild(wrap);
          tr.appendChild(td3);

          tbody.appendChild(tr);
        });
      } catch (err) { setMsg('user-msg', friendlyErrorMessage(err), false); }
    }

    var userEditId = null;

    function editUser(u) {
      userEditId = u.id;
      var f = document.getElementById('user-edit-form');
      f.hidden = false;
      f.username.value = u.username;
      f.display_name.value = u.display_name || '';
      f.password.value = '';
      document.getElementById('user-edit-title').textContent = '✏️ ' + (u.display_name || u.username);
      setMsg('user-edit-msg', '', true);
      f.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }

    function cancelUserEdit() {
      userEditId = null;
      document.getElementById('user-edit-form').hidden = true;
    }

    async function deleteUser(id, name) {
      if (!confirm('Удалить пользователя ' + name + ' и все его записи? Действие необратимо.')) return;
      try {
        var res = await fetch('/api/admin/users/' + id, {
          method: 'DELETE',
          headers: { 'X-CSRF-Token': csrf }
        });
        var out = {};
        try { out = await res.json(); } catch (e) {}
        if (!res.ok) { throw new Error(out.error || ('HTTP ' + res.status)); }
        setMsg('user-msg', 'Пользователь удалён', true);
        loadUsers();
      } catch (err) { setMsg('user-msg', friendlyErrorMessage(err), false); }
    }

    var userForm = document.getElementById('user-form');
    if (userForm) {
      userForm.addEventListener('submit', async function(e) {
        e.preventDefault();
        var f = e.target;
        try {
          await postJSON('/api/admin/users', {
            username: f.username.value,
            display_name: f.display_name.value,
            password: f.password.value
          });
          setMsg('user-msg', 'Пользователь добавлен', true);
          f.username.value = '';
          f.display_name.value = '';
          f.password.value = '';
          loadUsers();
        } catch (err) { setMsg('user-msg', friendlyErrorMessage(err), false); }
      });
      loadUsers();
    }

    async function loadBackupStatus() {
      if (!IS_ADMIN) return;
      try {
        var res = await fetch('/api/admin/backups');
        if (res.status === 401) { window.location = '/login'; return; }
        var out = await res.json();
        if (!res.ok) { throw new Error(out.error || ('HTTP ' + res.status)); }

        var statusEl = document.getElementById('backup-status');
        if (statusEl) {
          statusEl.textContent = out.enabled
            ? ('Автобэкап включён: каждый день в ' + out.scheduled_time + ' (время сервера), хранение ' + out.retention_days + ' дн., папка ' + out.backup_dir)
            : 'Автобэкап отключён на сервере (BACKUP_ENABLED=false)';
        }

        var tbody = document.querySelector('#backup-table tbody');
        tbody.innerHTML = '';
        if (out.backups.length === 0) {
          var tr0 = document.createElement('tr');
          var td0 = document.createElement('td');
          td0.colSpan = 3;
          td0.textContent = 'Копий пока нет';
          tr0.appendChild(td0);
          tbody.appendChild(tr0);
        }
        out.backups.forEach(function(b) {
          var tr = document.createElement('tr');
          var td1 = document.createElement('td');
          td1.textContent = b.name;
          tr.appendChild(td1);
          var td2 = document.createElement('td');
          td2.textContent = b.created_at;
          tr.appendChild(td2);
          var td3 = document.createElement('td');
          td3.textContent = (b.size_bytes / (1024 * 1024)).toFixed(1) + ' МБ';
          tr.appendChild(td3);

          var td4 = document.createElement('td');
          var restoreBtn = document.createElement('button');
          restoreBtn.type = 'button';
          restoreBtn.className = 'backup-restore-btn';
          restoreBtn.textContent = 'Восстановить';
          restoreBtn.addEventListener('click', function() { restoreBackup(b.name); });
          td4.appendChild(restoreBtn);
          tr.appendChild(td4);
          tbody.appendChild(tr);
        });
      } catch (err) {
        setMsg('backup-msg', friendlyErrorMessage(err), false);
      }
    }

    async function runBackupNow() {
      try {
        var out = await sendJSON('POST', '/api/admin/backups/run', {});
        setMsg('backup-msg', 'Копия создана: ' + out.file, true);
        showToast('Резервная копия создана', true);
        loadBackupStatus();
      } catch (err) {
        setMsg('backup-msg', friendlyErrorMessage(err), false);
        showToast(friendlyErrorMessage(err), false);
      }
    }

    async function restoreBackup(filename) {
      var warning = 'Восстановить базу данных из копии «' + filename + '»?\\n\\n'
        + 'Текущее состояние сначала будет сохранено в аварийную копию. '
        + 'Данные, созданные после выбранной резервной копии, будут заменены её содержимым.\\n\\n'
        + 'После восстановления потребуется повторно войти в приложение.';
      if (!window.confirm(warning)) return;

      try {
        var out = await sendJSON('POST', '/api/admin/backups/restore', { filename: filename });
        setMsg('backup-msg', 'База восстановлена из ' + out.restored_file
          + '. Аварийная копия: ' + out.emergency_backup, true);
        showToast('База восстановлена. Выполняется выход…', true);
        setTimeout(function() { window.location = '/login'; }, 900);
      } catch (err) {
        setMsg('backup-msg', friendlyErrorMessage(err), false);
        showToast(friendlyErrorMessage(err), false);
      }
    }
    loadBackupStatus();

    var userEditForm = document.getElementById('user-edit-form');
    if (userEditForm) {
      userEditForm.addEventListener('submit', async function(e) {
        e.preventDefault();
        if (!userEditId) return;
        var f = e.target;
        var payload = {
          username: f.username.value,
          display_name: f.display_name.value
        };
        if (f.password.value) { payload.password = f.password.value; }
        try {
          await sendJSON('PATCH', '/api/admin/users/' + userEditId, payload);
          setMsg('user-edit-msg', 'Сохранено', true);
          cancelUserEdit();
          loadUsers();
        } catch (err) {
          setMsg('user-edit-msg', friendlyErrorMessage(err), false);
        }
      });
    }

    function b64uToBuf(s) {
      s = s.replace(/-/g, '+').replace(/_/g, '/');
      while (s.length % 4) s += '=';
      var bin = atob(s);
      var buf = new Uint8Array(bin.length);
      for (var i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
      return buf.buffer;
    }
    function bufToB64u(buf) {
      var b = new Uint8Array(buf);
      var s = '';
      for (var i = 0; i < b.length; i++) s += String.fromCharCode(b[i]);
      return btoa(s).replace(/\\+/g, '-').replace(/\\//g, '_').replace(/=+$/, '');
    }

    function biometricName() {
      var ua = navigator.userAgent;
      if (/Android/i.test(ua)) return 'отпечаток пальца';
      var isIOS = /iPhone|iPad|iPod/i.test(ua) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
      if (isIOS) {
        var w = Math.min(screen.width, screen.height);
        var h = Math.max(screen.width, screen.height);
        if ((w === 320 && h === 568) || (w === 375 && h === 667)) return 'Touch ID';
        return 'Face ID';
      }
      return 'биометрию (Windows Hello / Touch ID)';
    }

    var waBioSupported = false;

    function updateWaToggle(registered) {
      var toggle = document.getElementById('wa-toggle');
      if (!toggle) { return; }
      toggle.checked = !!registered;
      // Включить можно только там, где есть биометрия. Отключить —
      // это просто удаление ключей на сервере, для этого биометрия на
      // текущем устройстве не нужна, поэтому если ключ уже есть
      // (зарегистрирован хоть на каком-то устройстве), переключатель
      // остаётся доступен в любом случае.
      toggle.disabled = !registered && !waBioSupported;
    }

    function refreshWaStatus() {
      return fetch('/api/webauthn/status').then(function(r) { return r.json(); }).then(function(out) {
        updateWaToggle(!!out.registered);
        return !!out.registered;
      }).catch(function() { return null; });
    }

    (function() {
      var sum = document.getElementById('wa-summary');
      var label = document.getElementById('wa-toggle-label');
      var avail = document.getElementById('wa-availability');
      var n = biometricName();
      if (sum) { sum.textContent = '🔐 ' + n; }
      if (label) { label.textContent = 'Вход по ' + n; }

      var supportCheck = (window.PublicKeyCredential && PublicKeyCredential.isUserVerifyingPlatformAuthenticatorAvailable)
        ? PublicKeyCredential.isUserVerifyingPlatformAuthenticatorAvailable().catch(function() { return false; })
        : Promise.resolve(false);

      supportCheck.then(function(av) {
        waBioSupported = !!av;
        if (!av && avail) {
          avail.textContent = 'На этом устройстве нет биометрии — включить здесь нельзя, но отключить для аккаунта можно.';
        }
        // Реальное состояние ("включена ли биометрия") спрашиваем у
        // сервера — ключ мог быть зарегистрирован на другом устройстве
        // этого аккаунта, а отключение удаляет все ключи целиком.
        refreshWaStatus();
      });
    })();

    async function onWaToggleChange(el) {
      el.disabled = true;
      try {
        if (el.checked) {
          await waRegister();
        } else {
          if (!confirm('Отключить вход по биометрии для этого аккаунта?')) {
            el.checked = true;
            return;
          }
          await waDelete();
        }
      } finally {
        await refreshWaStatus();
      }
    }

    async function waRegister() {
      try {
        if (!window.PublicKeyCredential) throw new Error('WebAuthn не поддерживается на этом устройстве/браузере');
        var res = await fetch('/api/webauthn/register/options', { method: 'POST', headers: { 'X-CSRF-Token': csrf } });
        var opts = await res.json();
        if (!res.ok) throw new Error(opts.error || 'HTTP ' + res.status);
        opts.challenge = b64uToBuf(opts.challenge);
        opts.user.id = b64uToBuf(opts.user.id);
        opts.excludeCredentials = (opts.excludeCredentials || []).map(function(c) { c.id = b64uToBuf(c.id); return c; });
        var cred = await navigator.credentials.create({ publicKey: opts });
        var res2 = await fetch('/api/webauthn/register', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
          body: JSON.stringify({
            id: cred.id,
            rawId: bufToB64u(cred.rawId),
            type: cred.type,
            response: {
              attestationObject: bufToB64u(cred.response.attestationObject),
              clientDataJSON: bufToB64u(cred.response.clientDataJSON)
            }
          })
        });
        var out = await res2.json();
        if (!res2.ok) throw new Error(out.error || 'HTTP ' + res2.status);
        try { localStorage.setItem('medical_diary_wa_credential_id', cred.id); } catch (e) {}
        setMsg('wa-msg', biometricName() + ' включён для этого устройства', true);
      } catch (err) {
        setMsg('wa-msg', friendlyErrorMessage(err), false);
      }
    }

    async function waDelete() {
      try {
        await sendJSON('DELETE', '/api/webauthn/credentials', {});
        try { localStorage.removeItem('medical_diary_wa_credential_id'); } catch (e) {}
        setMsg('wa-msg', 'Вход по биометрии отключён', true);
      } catch (err) {
        setMsg('wa-msg', friendlyErrorMessage(err), false);
      }
    }

    var UNIT_RU_JS = {
      'g':'г','gram':'г','grams':'г','kg':'кг','kilogram':'кг','kilograms':'кг',
      'mg':'мг','milligram':'мг','milligrams':'мг','mcg':'мкг','µg':'мкг',
      'ml':'мл','milliliter':'мл','milliliters':'мл','l':'л','liter':'л','liters':'л',
      'pcs':'шт','pc':'шт','piece':'шт','pieces':'шт',
      'portion':'порция','portions':'порция',
      'г':'г','кг':'кг','мг':'мг','мкг':'мкг','мл':'мл','л':'л','шт':'шт','порция':'порция'
    };
    function unitRu(value) { var k = String(value || '').trim(); return UNIT_RU_JS[k] || k; }
    var editState = { type: null, id: null };

    async function sendJSON(method, url, data, extraHeaders) {
      var res;
      try {
        res = await fetch(url, {
          method: method,
          headers: Object.assign({ 'Content-Type': 'application/json', 'X-CSRF-Token': csrf }, extraHeaders || {}),
          body: JSON.stringify(data)
        });
      } catch (err) {
        throw new Error(friendlyErrorMessage(err));
      }

      if (res.status === 401) { window.location = '/login'; throw new Error('Требуется вход'); }

      var out = {};
      try { out = await res.json(); } catch (e) {}

      if (!res.ok) { throw new Error(out.error || ('HTTP ' + res.status)); }
      return out;
    }

    function startEdit(entry) {
      editState.type = entry.type;
      editState.id = entry.id;

      document.getElementById('edit-card').hidden = false;
      document.getElementById('edit-glucose').hidden = entry.type !== 'glucose';
      document.getElementById('edit-vitals').hidden = entry.type !== 'vitals';
      document.getElementById('edit-temperature').hidden = entry.type !== 'temperature';
      document.getElementById('edit-weight').hidden = entry.type !== 'weight';
      document.getElementById('edit-food').hidden = entry.type !== 'food';
      document.getElementById('edit-title').textContent = '✏️ ' + entry.type_label + ' · ' + (entry.measured_at_ru || entry.measured_at);

      document.getElementById('edit_measured_at').value = entry.measured_at.replace(' ', 'T').substring(0, 16);
      document.getElementById('edit_comment').value = entry.comment || '';

      if (entry.type === 'glucose') {
        document.getElementById('edit_glucose_type').value = entry.glucose_type;
        document.getElementById('edit_glucose_value').value = entry.value_mmol_l;
      } else if (entry.type === 'vitals') {
        document.getElementById('edit_systolic').value = entry.systolic_mmhg;
        document.getElementById('edit_diastolic').value = entry.diastolic_mmhg;
        document.getElementById('edit_pulse').value = (entry.pulse_bpm == null) ? '' : entry.pulse_bpm;
      } else if (entry.type === 'temperature') {
        document.getElementById('edit_temperature_value').value = entry.temperature_c;
      } else if (entry.type === 'weight') {
        document.getElementById('edit_weight_value').value = entry.weight_kg;
      } else if (entry.type === 'food') {
        document.getElementById('edit_food_name').value = entry.food_name;
        document.getElementById('edit_amount_value').value = entry.amount_value;
        document.getElementById('edit_amount_unit').value = UNIT_RU_JS[entry.amount_unit] || entry.amount_unit;
      }

      setMsg('edit-msg', '', true);
      document.getElementById('edit-card').scrollIntoView({ behavior: 'smooth', block: 'start' });
    }

    function closeEdit() {
      editState.type = null;
      editState.id = null;
      document.getElementById('edit-card').hidden = true;
    }

    async function saveEdit() {
      if (!editState.id) return;

      var url = '';
      var payload = {};
      var ma = document.getElementById('edit_measured_at').value.replace('T', ' ');
      var cm = document.getElementById('edit_comment').value;

      if (editState.type === 'glucose') {
        url = '/api/glucose/' + editState.id;
        payload = {
          glucose_type: document.getElementById('edit_glucose_type').value,
          value: document.getElementById('edit_glucose_value').value,
          measured_at: ma,
          comment: cm
        };
      } else if (editState.type === 'vitals') {
        url = '/api/vitals/' + editState.id;
        payload = {
          systolic: document.getElementById('edit_systolic').value,
          diastolic: document.getElementById('edit_diastolic').value,
          pulse: document.getElementById('edit_pulse').value,
          measured_at: ma,
          comment: cm
        };
      } else if (editState.type === 'temperature') {
        url = '/api/temperature/' + editState.id;
        payload = {
          value: document.getElementById('edit_temperature_value').value,
          measured_at: ma,
          comment: cm
        };
      } else if (editState.type === 'weight') {
        url = '/api/weight/' + editState.id;
        payload = {
          value: document.getElementById('edit_weight_value').value,
          measured_at: ma,
          comment: cm
        };
      } else if (editState.type === 'food') {
        url = '/api/food/' + editState.id;
        payload = {
          food_name: document.getElementById('edit_food_name').value,
          amount_value: document.getElementById('edit_amount_value').value,
          amount_unit: unitRu(document.getElementById('edit_amount_unit').value),
          consumed_at: ma,
          comment: cm
        };
      }

      try {
        await sendJSON('PATCH', url, payload);
        setMsg('edit-msg', '', true);
        showToast('Изменения сохранены', true);
        closeEdit();
        loadHistory();
      } catch (err) {
        setMsg('edit-msg', friendlyErrorMessage(err), false);
        showToast(friendlyErrorMessage(err), false);
      }
    }

    async function deleteEntry(entry) {
      if (!confirm('Удалить запись «' + entry.type_label + '» от ' + (entry.measured_at_ru || entry.measured_at) + '? Она скроется из истории и PDF.')) return;

      var url = '';
      if (entry.type === 'glucose') { url = '/api/glucose/' + entry.id; }
      else if (entry.type === 'vitals') { url = '/api/vitals/' + entry.id; }
      else if (entry.type === 'temperature') { url = '/api/temperature/' + entry.id; }
      else if (entry.type === 'weight') { url = '/api/weight/' + entry.id; }
      else if (entry.type === 'food') { url = '/api/food/' + entry.id; }

      try {
        await sendJSON('DELETE', url, {});
        showToast('Запись удалена', true);
        loadHistory();
      } catch (err) {
        setMsg('history-msg', friendlyErrorMessage(err), false);
        showToast(friendlyErrorMessage(err), false);
      }
    }

    var currentExportUrl = '';
    var pdfBlob = null;
    var pdfDoc = null;
    var pdfPages = [];
    var currentZoom = 1;
    var renderedZoom = 1;
    var pinchState = null;
    var pinchBound = false;
    var rerenderTimer = null;
    var pdfJsLoadPromise = null;

    function ensurePdfJsLoaded() {
      if (typeof pdfjsLib !== 'undefined') {
        try {
          if (pdfjsLib.GlobalWorkerOptions) { pdfjsLib.GlobalWorkerOptions.workerSrc = '/pdf.worker.min.js'; }
        } catch (e) {}
        return Promise.resolve(pdfjsLib);
      }
      if (pdfJsLoadPromise) { return pdfJsLoadPromise; }
      pdfJsLoadPromise = new Promise(function(resolve, reject) {
        var script = document.createElement('script');
        script.src = '/pdf.min.js';
        script.async = true;
        script.onload = function() {
          if (typeof pdfjsLib === 'undefined') {
            reject(new Error('Модуль PDF не загрузился')); return;
          }
          try {
            if (pdfjsLib.GlobalWorkerOptions) { pdfjsLib.GlobalWorkerOptions.workerSrc = '/pdf.worker.min.js'; }
          } catch (e) {}
          resolve(pdfjsLib);
        };
        script.onerror = function() { reject(new Error('Не удалось загрузить модуль PDF')); };
        document.head.appendChild(script);
      });
      return pdfJsLoadPromise;
    }

    function clampZoom(z) { return Math.min(4, Math.max(1, z)); }

    function updateZoomLabel() {
      var el = document.getElementById('zoom-label');
      if (el) { el.textContent = Math.round(currentZoom * 100) + '%'; }
    }

    function applyCssZoom(z) {
      var k = z / renderedZoom;
      for (var i = 0; i < pdfPages.length; i++) {
        var c = pdfPages[i].canvas;
        c.style.width = Math.floor(pdfPages[i].cssW * k) + 'px';
        c.style.height = Math.floor(pdfPages[i].cssH * k) + 'px';
      }
    }

    async function renderPdfPages(zoom) {
      var pagesEl = document.getElementById('pdf-pages');
      var containerWidth = Math.max(pagesEl.clientWidth - 20, 200);
      var dprCap = Math.min(window.devicePixelRatio || 1, 2);

      for (var i = 1; i <= pdfDoc.numPages; i++) {
        var page = await pdfDoc.getPage(i);
        var base = page.getViewport({ scale: 1 });
        var scale = (containerWidth / base.width) * zoom;
        var viewport = page.getViewport({ scale: scale });

        var item = pdfPages[i - 1];
        var canvas = item ? item.canvas : document.createElement('canvas');
        canvas.width = Math.floor(viewport.width * dprCap);
        canvas.height = Math.floor(viewport.height * dprCap);
        var cssW = Math.floor(viewport.width);
        var cssH = Math.floor(viewport.height);
        canvas.style.width = cssW + 'px';
        canvas.style.height = cssH + 'px';
        if (!canvas.parentNode) { pagesEl.appendChild(canvas); }

        await page.render({
          canvasContext: canvas.getContext('2d'),
          viewport: viewport,
          transform: dprCap !== 1 ? [dprCap, 0, 0, dprCap, 0, 0] : null
        }).promise;

        pdfPages[i - 1] = { canvas: canvas, cssW: cssW, cssH: cssH };
      }
      renderedZoom = zoom;
    }

    function scheduleRerender() {
      clearTimeout(rerenderTimer);
      rerenderTimer = setTimeout(async function() {
        if (pdfDoc) { await renderPdfPages(currentZoom); }
      }, 250);
    }

    function zoomPdf(dir) {
      currentZoom = clampZoom(currentZoom * (dir > 0 ? 1.25 : 0.8));
      applyCssZoom(currentZoom);
      updateZoomLabel();
      scheduleRerender();
    }

    function pinchDist(e) {
      var dx = e.touches[0].clientX - e.touches[1].clientX;
      var dy = e.touches[0].clientY - e.touches[1].clientY;
      return Math.sqrt(dx * dx + dy * dy);
    }

    function bindPinch() {
      if (pinchBound) return;
      pinchBound = true;
      var pagesEl = document.getElementById('pdf-pages');

      pagesEl.addEventListener('touchstart', function(e) {
        if (e.touches.length === 2) {
          pinchState = { d: pinchDist(e), z: currentZoom };
          e.preventDefault();
        }
      }, { passive: false });

      pagesEl.addEventListener('touchmove', function(e) {
        if (pinchState && e.touches.length === 2) {
          e.preventDefault();
          currentZoom = clampZoom(pinchState.z * pinchDist(e) / pinchState.d);
          applyCssZoom(currentZoom);
          updateZoomLabel();
        }
      }, { passive: false });

      pagesEl.addEventListener('touchend', function(e) {
        if (pinchState && e.touches.length < 2) {
          pinchState = null;
          if (Math.abs(currentZoom - renderedZoom) > 0.01) { scheduleRerender(); }
        }
      });
    }

    function openPdfTypeModal() {
      var currentType = (document.getElementById('history_type') || {}).value || 'all';
      var preselect = currentType === 'all' ? null : currentType.split(',');
      document.querySelectorAll('.pdf-type-cb').forEach(function (cb) {
        cb.checked = preselect ? (preselect.indexOf(cb.value) !== -1) : true;
      });
      setMsg('pdf-type-msg', '', true);
      document.getElementById('pdf-type-modal').hidden = false;
      document.body.style.overflow = 'hidden';
    }

    function closePdfTypeModal() {
      document.getElementById('pdf-type-modal').hidden = true;
      document.body.style.overflow = '';
    }

    document.getElementById('pdf-type-modal').addEventListener('click', function (e) {
      if (e.target === this) { closePdfTypeModal(); }
    });

    function confirmPdfTypeSelection() {
      var selected = Array.prototype.slice.call(document.querySelectorAll('.pdf-type-cb:checked')).map(function (cb) { return cb.value; });
      if (!selected.length) {
        setMsg('pdf-type-msg', 'Выберите хотя бы один тип записей', false);
        return;
      }

      var allValues = ['glucose', 'vitals', 'temperature', 'weight', 'food'];
      var typeParam = (selected.length === allValues.length) ? 'all' : selected.join(',');

      var df = document.getElementById('date_from').value;
      var dt = document.getElementById('date_to').value;
      var sortInput = document.querySelector('input[name="history_sort"]:checked');
      var sort = sortInput ? sortInput.value : 'date';

      currentExportUrl = '/export.pdf?date_from=' + encodeURIComponent(df) + '&date_to=' + encodeURIComponent(dt) + '&type=' + encodeURIComponent(typeParam) + '&sort=' + encodeURIComponent(sort);

      closePdfTypeModal();
      openPdfViewer();
    }

    async function openPdfViewer() {
      if (!currentExportUrl) { setMsg('history-msg', 'Сначала дождитесь загрузки истории', false); return; }

      var overlay = document.getElementById('pdf-overlay');
      var pages = document.getElementById('pdf-pages');
      overlay.hidden = false;
      document.body.style.overflow = 'hidden';
      pages.innerHTML = '<div class="pdf-status">⏳ Формирование PDF…</div>';

      currentZoom = 1;
      renderedZoom = 1;
      pdfPages = [];
      updateZoomLabel();

      try {
        var pdfLib = await ensurePdfJsLoaded();
        var res = await fetch(currentExportUrl);
        if (res.status === 401) { window.location = '/login'; return; }
        if (!res.ok) { throw new Error('HTTP ' + res.status); }
        pdfBlob = await res.blob();
        var data = await pdfBlob.arrayBuffer();
        pdfDoc = await pdfLib.getDocument({ data: data }).promise;
        pages.innerHTML = '';
        pdfPages = [];
        await renderPdfPages(1);
        bindPinch();
      } catch (err) {
        pages.innerHTML = '<div class="pdf-status">Ошибка просмотра: ' + friendlyErrorMessage(err) + '</div>';
      }
    }

    function downloadPdf() {
      if (!pdfBlob) {
        alert('PDF ещё не сформирован.');
        return;
      }
      try {
        var url = URL.createObjectURL(pdfBlob);
        var a = document.createElement('a');
        a.href = url;
        a.download = 'medical_diary.pdf';
        a.style.display = 'none';
        document.body.appendChild(a);
        a.click();
        setTimeout(function() {
          document.body.removeChild(a);
          URL.revokeObjectURL(url);
        }, 1000);
      } catch (err) {
        alert('Не удалось сохранить PDF: ' + friendlyErrorMessage(err));
      }
    }

    async function sharePdf() {
      if (!pdfBlob) return;
      try {
        var file = new File([pdfBlob], 'medical_diary.pdf', { type: 'application/pdf' });
        if (navigator.canShare && navigator.canShare({ files: [file] })) {
          await navigator.share({ files: [file], title: 'Медицинский дневник' });
        } else {
          var a = document.createElement('a');
          a.href = URL.createObjectURL(pdfBlob);
          a.download = 'medical_diary.pdf';
          document.body.appendChild(a);
          a.click();
          document.body.removeChild(a);
          setTimeout(function() { URL.revokeObjectURL(a.href); }, 5000);
        }
      } catch (err) {
        if (err && err.name !== 'AbortError') { alert('Не удалось поделиться: ' + friendlyErrorMessage(err)); }
      }
    }

    function closePdfViewer() {
      document.getElementById('pdf-overlay').hidden = true;
      document.body.style.overflow = '';
      if (pdfDoc) { pdfDoc.destroy(); pdfDoc = null; }
      pdfBlob = null;
      pdfPages = [];
      currentZoom = 1;
      renderedZoom = 1;
      document.getElementById('pdf-pages').innerHTML = '';
    }

    async function logout() {
      try {
        await fetch('/logout', { method: 'POST', headers: { 'X-CSRF-Token': csrf } });
      } catch (e) {}
      window.location = '/login';
    }

    (function() {
  // details manual toggle: гарантированное сворачивание/разворачивание
  // блоков по тапу на заголовок, независимо от нативного поведения iOS.
  document.querySelectorAll('summary').forEach(function(s) {
    s.addEventListener('click', function(e) {
      var d = s.closest('details');
      if (!d) { return; }
      e.preventDefault();
      if (d.hasAttribute('open')) { d.removeAttribute('open'); } else { d.setAttribute('open', ''); }
    });
  });
})();

loadHistory();

(function(){
  var originalLoadLast=window.loadLastEntryStatuses;
  if(typeof originalLoadLast==='function') window.loadLastEntryStatuses=function(){originalLoadLast();loadDashboard();};
  document.addEventListener('visibilitychange',function(){if(!document.hidden)loadDashboard();});
})();
