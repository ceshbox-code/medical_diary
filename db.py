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
