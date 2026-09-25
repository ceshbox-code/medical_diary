"""База данных: путь к файлу, схема, доступ per-request через Flask `g`,
инициализация при старте. Никакой бизнес-логики — только хранение.

Извлечено из app.py на шаге 4 модуляризации.

Регистрация close_db как @app.teardown_appcontext делается в app.py
(`app.teardown_appcontext(close_db)`), т.к. этот модуль не создаёт
объект Flask-приложения и не должен на него ссылаться.
"""

import os
import sqlite3

from flask import g
from werkzeug.security import generate_password_hash


DATABASE = os.getenv("DATABASE_PATH", "/data/medical_diary.db")


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  display_name TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  is_admin INTEGER NOT NULL DEFAULT 0,
  settings_json TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS glucose_entries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  measured_at TEXT NOT NULL,
  glucose_type TEXT NOT NULL CHECK (glucose_type IN ('fasting', 'post_meal')),
  value_mmol_l REAL NOT NULL CHECK (value_mmol_l BETWEEN 0.1 AND 100.0),
  comment TEXT,
  source TEXT NOT NULL DEFAULT 'manual',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  deleted_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_glucose_user_time ON glucose_entries(user_id, measured_at);

CREATE TABLE IF NOT EXISTS blood_pressure_entries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  measured_at TEXT NOT NULL,
  systolic_mmhg INTEGER NOT NULL CHECK (systolic_mmhg BETWEEN 30 AND 400),
  diastolic_mmhg INTEGER NOT NULL CHECK (diastolic_mmhg BETWEEN 10 AND 300),
  pulse_bpm INTEGER CHECK (pulse_bpm IS NULL OR pulse_bpm BETWEEN 20 AND 300),
  comment TEXT,
  source TEXT NOT NULL DEFAULT 'manual',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  deleted_at TEXT,
  CHECK (systolic_mmhg > diastolic_mmhg)
);

CREATE INDEX IF NOT EXISTS idx_bp_user_time ON blood_pressure_entries(user_id, measured_at);

CREATE TABLE IF NOT EXISTS food_entries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  food_name TEXT NOT NULL,
  consumed_at TEXT NOT NULL,
  amount_value REAL NOT NULL CHECK (amount_value > 0),
  amount_unit TEXT NOT NULL,
  comment TEXT,
  source TEXT NOT NULL DEFAULT 'manual',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  deleted_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_food_user_time ON food_entries(user_id, consumed_at);

CREATE TABLE IF NOT EXISTS temperature_entries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  measured_at TEXT NOT NULL,
  temperature_c REAL NOT NULL,
  comment TEXT,
  source TEXT NOT NULL DEFAULT 'manual',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  deleted_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_temperature_user_time ON temperature_entries(user_id, measured_at);

CREATE TABLE IF NOT EXISTS weight_entries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  measured_at TEXT NOT NULL,
  weight_kg REAL NOT NULL CHECK (weight_kg BETWEEN 1.0 AND 500.0),
  comment TEXT,
  source TEXT NOT NULL DEFAULT 'manual',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  deleted_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_weight_user_time ON weight_entries(user_id, measured_at);

CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER,
  action TEXT NOT NULL,
  entity_type TEXT,
  entity_id INTEGER,
  ip_address TEXT,
  user_agent TEXT,
  details_json TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS login_throttle (
  throttle_key TEXT PRIMARY KEY,
  fail_count INTEGER NOT NULL DEFAULT 0,
  locked_until TEXT,
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  endpoint TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  entity_type TEXT,
  entity_id INTEGER,
  response_json TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (user_id, endpoint, idempotency_key)
);

-- Кэш ИИ-оценок (GigaChat) по каждой записи дневника. input_hash — хэш
-- ровно тех данных, что отправляются модели (значение, тип, дата/время
-- записи + справочные диапазоны). Пока хэш совпадает — запрос к GigaChat
-- повторно не делается, при экспорте PDF используется сохранённый текст.
-- Если пользователь изменит запись или свои диапазоны, хэш изменится, и
-- при следующем экспорте оценка будет сгенерирована заново автоматически.
CREATE TABLE IF NOT EXISTS ai_assessment_cache (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  entry_type TEXT NOT NULL,
  entry_id INTEGER NOT NULL,
  input_hash TEXT NOT NULL,
  assessment TEXT NOT NULL,
  recommendation TEXT NOT NULL,
  model TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (user_id, entry_type, entry_id)
);

-- Кэш ИИ-оценки динамики показателей за период (вкладка "История" →
-- "Оценка динамики от ИИ"). В отличие от ai_assessment_cache (кэш по
-- каждой отдельной записи), здесь input_hash считается по уже
-- агрегированной статистике всего периода (compute_period_stats) —
-- см. ai_utils._dynamics_input_hash. Пока период, фильтр типа и сами
-- данные не изменились — повторный запрос к GigaChat не делается.
CREATE TABLE IF NOT EXISTS ai_dynamics_cache (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  input_hash TEXT NOT NULL,
  summary TEXT NOT NULL,
  observations_json TEXT NOT NULL,
  caution TEXT,
  model TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (user_id, input_hash)
);

-- Троттлинг запросов динамики к GigaChat: одна строка на каждый
-- "свежий" (не из кэша) запрос пользователя. Считается через COUNT(*)
-- за последние N секунд/час — см. AI_DYNAMICS_COOLDOWN_SECONDS /
-- AI_DYNAMICS_HOURLY_LIMIT в assessments.py. Строки не удаляются
-- намеренно: объём крайне мал (одна запись на реальный вызов ИИ, а не
-- на каждое открытие вкладки — попадания в ai_dynamics_cache строк не
-- добавляют), исторический след запросов к платному API полезен и для
-- аудита.
CREATE TABLE IF NOT EXISTS ai_dynamics_throttle (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  requested_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_ai_dynamics_throttle_user_time ON ai_dynamics_throttle(user_id, requested_at);

CREATE TABLE IF NOT EXISTS webauthn_credentials (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  credential_id BLOB NOT NULL UNIQUE,
  public_key BLOB NOT NULL,
  sign_count INTEGER NOT NULL DEFAULT 0,
  rp_id TEXT NOT NULL,
  origin TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  last_used_at TEXT
);

-- Лекарства пользователя. Название, дозу и инструкцию вводит сам
-- пользователь; сервис их не проверяет и не даёт рекомендаций.
-- days_mask: бит 0 = понедельник ... бит 6 = воскресенье (127 = каждый день).
CREATE TABLE IF NOT EXISTS medications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  dose_value REAL CHECK (dose_value IS NULL OR dose_value > 0),
  dose_unit TEXT,
  instructions TEXT,
  start_date TEXT NOT NULL,
  end_date TEXT,
  is_active INTEGER NOT NULL DEFAULT 1,
  comment TEXT,
  days_mask INTEGER NOT NULL DEFAULT 127 CHECK (days_mask BETWEEN 1 AND 127),
  source TEXT NOT NULL DEFAULT 'manual',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  deleted_at TEXT,
  CHECK ((dose_value IS NULL) = (dose_unit IS NULL)),
  CHECK (end_date IS NULL OR end_date >= start_date)
);

CREATE INDEX IF NOT EXISTS idx_medications_user ON medications(user_id, deleted_at);

-- Времена суток приёма (HH:MM). Одна строка на каждое время.
CREATE TABLE IF NOT EXISTS medication_schedule (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  medication_id INTEGER NOT NULL REFERENCES medications(id) ON DELETE CASCADE,
  time_of_day TEXT NOT NULL,
  UNIQUE (medication_id, time_of_day)
);

-- ЖУРНАЛ ФАКТОВ приёма: только то, что пользователь отметил сам.
-- Название и доза сохраняются снимком на момент отметки, поэтому
-- последующее редактирование лекарства историю не меняет.
-- scheduled_at = NULL — приём вне графика (только status = 'taken').
-- «Не принял» автоматически не записывается: это расчётное состояние в API.
CREATE TABLE IF NOT EXISTS medication_intakes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  medication_id INTEGER NOT NULL REFERENCES medications(id) ON DELETE CASCADE,
  medication_name TEXT NOT NULL,
  dose_value REAL,
  dose_unit TEXT,
  scheduled_at TEXT,
  status TEXT NOT NULL CHECK (status IN ('taken', 'skipped')),
  taken_at TEXT,
  comment TEXT,
  source TEXT NOT NULL DEFAULT 'manual',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  deleted_at TEXT,
  CHECK ((status = 'taken' AND taken_at IS NOT NULL) OR (status = 'skipped' AND taken_at IS NULL)),
  CHECK (status = 'taken' OR scheduled_at IS NOT NULL)
);

-- Один плановый приём — одна активная отметка (идемпотентность повторов).
CREATE UNIQUE INDEX IF NOT EXISTS ux_intake_slot
  ON medication_intakes(medication_id, scheduled_at)
  WHERE scheduled_at IS NOT NULL AND deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_intakes_user_time ON medication_intakes(user_id, scheduled_at);

-- Личный справочник «GTIN упаковки -> название/доза», который пользователь
-- сам наполняет при первом сканировании штрихкода/DataMatrix конкретной
-- упаковки (см. reminders.py: /api/medication-barcodes). Никаких обращений
-- к внешним реестрам (ИС МДЛП/«Честный знак») не выполняется — только то,
-- что пользователь один раз подтвердил вручную.
CREATE TABLE IF NOT EXISTS medication_barcodes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  gtin TEXT NOT NULL,
  name TEXT NOT NULL,
  dose_value REAL CHECK (dose_value IS NULL OR dose_value > 0),
  dose_unit TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  CHECK ((dose_value IS NULL) = (dose_unit IS NULL)),
  UNIQUE (user_id, gtin)
);

CREATE INDEX IF NOT EXISTS idx_medication_barcodes_user ON medication_barcodes(user_id, gtin);

-- Локальный офлайн-кэш официальных открытых данных ЦРПТ (GTIN -> лекарство),
-- еженедельная выгрузка "Сведения о лекарственных препаратах для медицинского
-- применения, подлежащих обязательной маркировке" (не путать с платным
-- BI-сервисом "Датамаркет" — это отдельный, официальный open-data раздел).
-- Наполняется офлайн скриптом import_mdlp_gtins.py, сервис никогда не
-- обращается за этими данными в интернет при обработке запроса пользователя.
-- ВАЖНО: колонка inn в исходном файле ЦРПТ — это ИНН (налоговый номер)
-- организации, зарегистрировавшей препарат, а НЕ международное непатентованное
-- наименование (для него есть prod_name) — поэтому здесь она не хранится.
-- prod_sell_name_norm — то же название в нижнем регистре (приведено в Python
-- через str.lower(), а не SQL LOWER()/LIKE: у SQLite без расширения ICU
-- регистронезависимость LIKE работает только для ASCII, кириллицу не
-- сворачивает). Используется для поиска-подсказки при ручном вводе.
CREATE TABLE IF NOT EXISTS mdlp_gtins (
  gtin TEXT PRIMARY KEY,
  prod_sell_name TEXT,   -- торговое наименование
  prod_sell_name_norm TEXT, -- то же в нижнем регистре, для регистронезависимого поиска
  prod_desc TEXT,        -- наименование товара на этикетке (полное)
  prod_name TEXT,        -- МНН
  dose_raw TEXT,         -- дозировка как есть у ЦРПТ, свободный текст (см. примечание в import_mdlp_gtins.py)
  form_name TEXT,        -- лекарственная форма
  gnvlp TEXT,            -- признак ЖНВЛП ('Да'/'Нет')
  reg_status TEXT,       -- статус записи в ЕСКЛП ('Действующий'/'Недействующий')
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Индекс на prod_sell_name_norm создаётся не здесь, а в init_db() ниже,
-- ПОСЛЕ возможного ALTER TABLE — иначе на уже существующей базе (где эта
-- колонка ещё не появилась) CREATE TABLE IF NOT EXISTS окажется no-op'ом,
-- а этот CREATE INDEX упадёт с "no such column" при старте приложения.

-- Напоминания об измерениях (не о лекарствах — те строятся из графика).
CREATE TABLE IF NOT EXISTS reminders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK (kind IN ('glucose', 'vitals', 'weight', 'temperature', 'food', 'custom')),
  title TEXT NOT NULL,
  time_of_day TEXT NOT NULL,
  days_mask INTEGER NOT NULL DEFAULT 127 CHECK (days_mask BETWEEN 1 AND 127),
  is_active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  deleted_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_reminders_user ON reminders(user_id, deleted_at);

-- Подписки устройств на Web Push. endpoint — секретный адрес push-сервиса
-- (в журнал аудита не пишется). Одна строка на устройство/браузер.
CREATE TABLE IF NOT EXISTS push_subscriptions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  endpoint TEXT NOT NULL UNIQUE,
  p256dh TEXT NOT NULL,
  auth TEXT NOT NULL,
  user_agent TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  last_success_at TEXT,
  failure_count INTEGER NOT NULL DEFAULT 0,
  last_error TEXT
);

CREATE INDEX IF NOT EXISTS idx_push_subs_user ON push_subscriptions(user_id);

-- Журнал отправленных уведомлений: одна строка на событие (лекарство или
-- напоминание + плановое время + фаза). UNIQUE не даёт отправить одно и то же
-- событие дважды, в том числе при повторных проходах планировщика.
-- status: claimed (взято в работу), sent, retry (временный сбой, будет повтор),
-- failed, suppressed (уже отмечено/записано — уведомление не нужно).
CREATE TABLE IF NOT EXISTS notification_deliveries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK (kind IN ('medication', 'reminder')),
  ref_id INTEGER NOT NULL,
  due_at TEXT NOT NULL,
  phase TEXT NOT NULL CHECK (phase IN ('first', 'repeat')),
  status TEXT NOT NULL CHECK (status IN ('claimed', 'sent', 'retry', 'failed', 'suppressed')),
  attempts INTEGER NOT NULL DEFAULT 1,
  sent_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (kind, ref_id, due_at, phase)
);
"""


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        g.db.execute("PRAGMA journal_mode = WAL")
    return g.db


def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    os.makedirs(os.path.dirname(DATABASE), exist_ok=True)
    conn = sqlite3.connect(DATABASE)
    conn.executescript(SCHEMA)
    conn.execute("PRAGMA journal_mode = WAL")

    cols = [r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
    if "is_admin" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")
    if "settings_json" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN settings_json TEXT")

    mdlp_cols = [r[1] for r in conn.execute("PRAGMA table_info(mdlp_gtins)").fetchall()]
    if mdlp_cols and "prod_sell_name_norm" not in mdlp_cols:
        conn.execute("ALTER TABLE mdlp_gtins ADD COLUMN prod_sell_name_norm TEXT")
        conn.execute(
            "UPDATE mdlp_gtins SET prod_sell_name_norm = LOWER(prod_sell_name) WHERE prod_sell_name IS NOT NULL"
        )
        # LOWER() в SQLite сворачивает только ASCII, кириллицу оставляет как
        # есть — для уже загруженных строк это не страшно (следующий запуск
        # import_mdlp_gtins.py всё равно перезапишет колонку корректным,
        # посчитанным в Python значением).
    if mdlp_cols:
        # Вне if выше и без "IF NOT EXISTS" колонки в PRAGMA — иначе на
        # только что созданной этим же executescript(SCHEMA) таблице (колонка
        # уже есть с самого начала) индекс не создался бы никогда. Условие
        # "if mdlp_cols" тут по сути всегда истинно на этом этапе — таблица
        # к этому моменту уже гарантированно существует (только что созданная
        # SCHEMA или ранее существовавшая, только что мигрированная выше).
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mdlp_gtins_name_norm ON mdlp_gtins(prod_sell_name_norm)")

    username = os.getenv("ADMIN_USERNAME", "admin")
    password = os.getenv("ADMIN_PASSWORD")

    if username and password:
        try:
            conn.execute(
                "INSERT INTO users (username, password_hash, display_name, status) VALUES (?, ?, ?, ?)",
                (username, generate_password_hash(password), username, "active"),
            )
        except sqlite3.IntegrityError:
            pass

    # Пользователь из ADMIN_USERNAME всегда получает права администратора
    # при каждом старте приложения. Это намеренный механизм восстановления
    # доступа (например, если admin-флаг был случайно снят), а не ошибка —
    # но учитывайте это при ротации ADMIN_USERNAME в окружении.
    #
    # ВАЖНО: этот UPDATE обязан идти ПОСЛЕ INSERT выше, а не до него. При
    # самом первом запуске (пустая БД, только что после install_synology.sh)
    # строки администратора ещё не существует в момент UPDATE — INSERT
    # создаёт её со значением is_admin по умолчанию (0, см. SCHEMA), и
    # админ-панель недоступна вплоть до следующего перезапуска контейнера
    # (только тогда UPDATE находит уже существующую строку). Раньше UPDATE
    # шёл первым и ловил ровно эту ситуацию на каждой свежей установке.
    conn.execute(
        "UPDATE users SET is_admin = 1 WHERE username = ?",
        (username,),
    )

    conn.commit()
    conn.close()
