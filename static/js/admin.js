/* Медицинский дневник — интерфейс администратора. */
(function () {
  'use strict';

  var csrfMeta = document.querySelector('meta[name="csrf-token"]');
  var csrf = csrfMeta ? csrfMeta.content : '';

  function friendlyErrorMessage(err) {
    var text = err && err.message ? String(err.message) : 'Неизвестная ошибка';
    if (/Failed to fetch|NetworkError|Load failed/i.test(text)) {
      return 'Не удалось связаться с сервером';
    }
    return text;
  }

  function setMsg(id, text, ok) {
    var el = document.getElementById(id);
    if (!el) return;
    el.textContent = text || '';
    el.className = 'message ' + (ok ? 'ok' : 'error');
  }

  async function sendJSON(method, url, data) {
    var res;
    try {
      res = await fetch(url, {
        method: method,
        headers: {
          'Content-Type': 'application/json',
          'X-CSRF-Token': csrf
        },
        body: JSON.stringify(data || {})
      });
    } catch (err) {
      throw new Error(friendlyErrorMessage(err));
    }

    if (res.status === 401) {
      window.location = '/login';
      throw new Error('Требуется вход');
    }

    var out = {};
    try { out = await res.json(); } catch (e) {}
    if (!res.ok) throw new Error(out.error || ('HTTP ' + res.status));
    return out;
  }

  async function loadUsers() {
    try {
      var res = await fetch('/api/admin/users');
      if (res.status === 401) { window.location = '/login'; return; }
      var out = await res.json();
      if (!res.ok) throw new Error(out.error || ('HTTP ' + res.status));

      var tbody = document.querySelector('#users-table tbody');
      if (!tbody) return;
      tbody.innerHTML = '';

      (out.users || []).forEach(function (u) {
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

        var editBtn = document.createElement('button');
        editBtn.type = 'button';
        editBtn.className = 'edit-btn';
        editBtn.textContent = '✏️';
        editBtn.setAttribute('aria-label', 'Редактировать пользователя');
        editBtn.addEventListener('click', function () { editUser(u); });
        wrap.appendChild(editBtn);

        if (!u.is_admin) {
          var deleteBtn = document.createElement('button');
          deleteBtn.type = 'button';
          deleteBtn.className = 'del-btn';
          deleteBtn.textContent = '🗑️';
          deleteBtn.setAttribute('aria-label', 'Удалить пользователя');
          deleteBtn.addEventListener('click', function () { deleteUser(u.id, u.username); });
          wrap.appendChild(deleteBtn);
        }

        td3.appendChild(wrap);
        tr.appendChild(td3);
        tbody.appendChild(tr);
      });
    } catch (err) {
      setMsg('user-msg', friendlyErrorMessage(err), false);
    }
  }

  var userEditId = null;

  function editUser(u) {
    userEditId = u.id;
    var form = document.getElementById('user-edit-form');
    if (!form) return;
    form.hidden = false;
    form.username.value = u.username;
    form.display_name.value = u.display_name || '';
    form.password.value = '';
    var title = document.getElementById('user-edit-title');
    if (title) title.textContent = '✏️ ' + (u.display_name || u.username);
    setMsg('user-edit-msg', '', true);
    form.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  function cancelUserEdit() {
    userEditId = null;
    var form = document.getElementById('user-edit-form');
    if (form) form.hidden = true;
  }
  window.cancelUserEdit = cancelUserEdit;

  async function deleteUser(id, name) {
    if (!confirm('Удалить пользователя ' + name + ' и все его записи? Действие необратимо.')) return;
    try {
      await sendJSON('DELETE', '/api/admin/users/' + id, {});
      setMsg('user-msg', 'Пользователь удалён', true);
      await loadUsers();
    } catch (err) {
      setMsg('user-msg', friendlyErrorMessage(err), false);
    }
  }

  async function logout() {
    try {
      await fetch('/logout', { method: 'POST', headers: { 'X-CSRF-Token': csrf } });
    } catch (e) {}
    window.location = '/login';
  }
  window.logout = logout;

  async function runBackupNow() {
    try {
      var out = await sendJSON('POST', '/api/admin/backups/run', {});
      setMsg('backup-msg', 'Копия создана: ' + out.file, true);
      await loadBackupStatus();
    } catch (err) {
      setMsg('backup-msg', friendlyErrorMessage(err), false);
    }
  }
  window.runBackupNow = runBackupNow;

  async function loadBackupStatus() {
    try {
      var res = await fetch('/api/admin/backups');
      if (res.status === 401) { window.location = '/login'; return; }
      var out = await res.json();
      if (!res.ok) throw new Error(out.error || ('HTTP ' + res.status));

      var statusEl = document.getElementById('backup-status');
      if (statusEl) {
        statusEl.textContent = out.enabled
          ? ('Автобэкап включён: каждый день в ' + out.scheduled_time + ' (время сервера), хранение ' + out.retention_days + ' дн., папка ' + out.backup_dir)
          : 'Автобэкап отключён на сервере (BACKUP_ENABLED=false)';
      }

      var tbody = document.querySelector('#backup-table tbody');
      if (!tbody) return;
      tbody.innerHTML = '';

      var backups = out.backups || [];
      if (!backups.length) {
        var emptyRow = document.createElement('tr');
        var emptyCell = document.createElement('td');
        emptyCell.colSpan = 4;
        emptyCell.textContent = 'Копий пока нет';
        emptyRow.appendChild(emptyCell);
        tbody.appendChild(emptyRow);
      }

      backups.forEach(function (b) {
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
        restoreBtn.addEventListener('click', function () { restoreBackup(b.name); });
        td4.appendChild(restoreBtn);
        tr.appendChild(td4);

        tbody.appendChild(tr);
      });
    } catch (err) {
      setMsg('backup-msg', friendlyErrorMessage(err), false);
    }
  }

  async function restoreBackup(filename) {
    var warning = 'Восстановить базу данных из копии «' + filename + '»?\n\n'
      + 'Текущее состояние сначала будет сохранено в аварийную копию. '
      + 'Данные, созданные после выбранной резервной копии, будут заменены её содержимым.\n\n'
      + 'После восстановления потребуется повторно войти в приложение.';
    if (!window.confirm(warning)) return;

    try {
      var out = await sendJSON('POST', '/api/admin/backups/restore', { filename: filename });
      setMsg('backup-msg', 'База восстановлена из ' + out.restored_file
        + '. Аварийная копия: ' + out.emergency_backup, true);
      setTimeout(function () { window.location = '/login'; }, 900);
    } catch (err) {
      setMsg('backup-msg', friendlyErrorMessage(err), false);
    }
  }

  var LOGIN_ACTION_LABELS = {
    login_success: 'Вход выполнен',
    login_failed: 'Неверный пароль',
    login_blocked: 'Заблокировано (много попыток)',
    login_webauthn: 'Вход через passkey/Face ID',
    logout: 'Выход'
  };

  async function loadLoginLog() {
    try {
      var res = await fetch('/api/admin/login-log');
      if (res.status === 401) { window.location = '/login'; return; }
      var out = await res.json();
      if (!res.ok) throw new Error(out.error || ('HTTP ' + res.status));

      var tbody = document.querySelector('#login-log-table tbody');
      if (!tbody) return;
      tbody.innerHTML = '';

      var items = out.items || [];
      if (!items.length) {
        var emptyRow = document.createElement('tr');
        var emptyCell = document.createElement('td');
        emptyCell.colSpan = 5;
        emptyCell.textContent = 'Записей пока нет';
        emptyRow.appendChild(emptyCell);
        tbody.appendChild(emptyRow);
      }

      items.forEach(function (it) {
        var tr = document.createElement('tr');
        if (it.action === 'login_failed' || it.action === 'login_blocked') {
          tr.className = 'login-log-row-warn';
        }

        var td1 = document.createElement('td');
        td1.textContent = it.created_at;
        tr.appendChild(td1);

        var td2 = document.createElement('td');
        td2.textContent = LOGIN_ACTION_LABELS[it.action] || it.action;
        tr.appendChild(td2);

        var td3 = document.createElement('td');
        td3.textContent = it.display_name ? (it.display_name + ' (' + it.username + ')') : (it.username || '—');
        tr.appendChild(td3);

        var td4 = document.createElement('td');
        td4.textContent = it.ip_address || '—';
        tr.appendChild(td4);

        var td5 = document.createElement('td');
        var ua = it.user_agent || '';
        td5.textContent = ua.length > 40 ? (ua.slice(0, 40) + '…') : (ua || '—');
        td5.title = ua;
        tr.appendChild(td5);

        tbody.appendChild(tr);
      });
    } catch (err) {
      setMsg('login-log-msg', friendlyErrorMessage(err), false);
    }
  }

  document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('summary').forEach(function (summary) {
      summary.addEventListener('click', function (e) {
        var details = summary.closest('details');
        if (!details) return;
        e.preventDefault();
        if (details.hasAttribute('open')) details.removeAttribute('open');
        else details.setAttribute('open', '');
      });
    });

    var userForm = document.getElementById('user-form');
    if (userForm) {
      userForm.addEventListener('submit', async function (e) {
        e.preventDefault();
        var form = e.target;
        try {
          await sendJSON('POST', '/api/admin/users', {
            username: form.username.value,
            display_name: form.display_name.value,
            password: form.password.value
          });
          setMsg('user-msg', 'Пользователь добавлен', true);
          form.reset();
          await loadUsers();
        } catch (err) {
          setMsg('user-msg', friendlyErrorMessage(err), false);
        }
      });
    }

    var userEditForm = document.getElementById('user-edit-form');
    if (userEditForm) {
      userEditForm.addEventListener('submit', async function (e) {
        e.preventDefault();
        if (!userEditId) return;
        var form = e.target;
        var payload = {
          username: form.username.value,
          display_name: form.display_name.value
        };
        if (form.password.value) payload.password = form.password.value;
        try {
          await sendJSON('PATCH', '/api/admin/users/' + userEditId, payload);
          setMsg('user-edit-msg', 'Сохранено', true);
          cancelUserEdit();
          await loadUsers();
        } catch (err) {
          setMsg('user-edit-msg', friendlyErrorMessage(err), false);
        }
      });
    }

    loadUsers();
    loadBackupStatus();
    loadLoginLog();
  });
})();
