SET NAMES utf8mb4;

CREATE DATABASE IF NOT EXISTS medical_diary
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

USE medical_diary;

CREATE TABLE IF NOT EXISTS users (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  username VARCHAR(64) NOT NULL,
  password_hash VARCHAR(255) NOT NULL,
  display_name VARCHAR(100) NULL,
  status VARCHAR(20) NOT NULL DEFAULT 'active',
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  UNIQUE KEY uq_users_username (username),
  CONSTRAINT chk_users_status CHECK (status IN ('active', 'disabled'))
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_unicode_ci;


CREATE TABLE IF NOT EXISTS glucose_entries (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  user_id BIGINT UNSIGNED NOT NULL,
  measured_at DATETIME(6) NOT NULL,
  glucose_type VARCHAR(20) NOT NULL,
  value_mmol_l DECIMAL(5,2) NOT NULL,
  comment VARCHAR(1000) NULL,
  source VARCHAR(50) NOT NULL DEFAULT 'manual',
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  deleted_at DATETIME(6) NULL,
  PRIMARY KEY (id),
  KEY idx_glucose_user_time (user_id, measured_at),
  KEY idx_glucose_user_deleted_time (user_id, deleted_at, measured_at),
  CONSTRAINT fk_glucose_user
    FOREIGN KEY (user_id)
    REFERENCES users (id)
    ON DELETE CASCADE,
  CONSTRAINT chk_glucose_type CHECK (glucose_type IN ('fasting', 'post_meal')),
  CONSTRAINT chk_glucose_value CHECK (value_mmol_l BETWEEN 0.1 AND 100.0)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_unicode_ci;


CREATE TABLE IF NOT EXISTS blood_pressure_entries (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  user_id BIGINT UNSIGNED NOT NULL,
  measured_at DATETIME(6) NOT NULL,
  systolic_mmhg SMALLINT UNSIGNED NOT NULL,
  diastolic_mmhg SMALLINT UNSIGNED NOT NULL,
  pulse_bpm SMALLINT UNSIGNED NULL,
  comment VARCHAR(1000) NULL,
  source VARCHAR(50) NOT NULL DEFAULT 'manual',
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  deleted_at DATETIME(6) NULL,
  PRIMARY KEY (id),
  KEY idx_bp_user_time (user_id, measured_at),
  KEY idx_bp_user_deleted_time (user_id, deleted_at, measured_at),
  CONSTRAINT fk_bp_user
    FOREIGN KEY (user_id)
    REFERENCES users (id)
    ON DELETE CASCADE,
  CONSTRAINT chk_bp_systolic CHECK (systolic_mmhg BETWEEN 30 AND 400),
  CONSTRAINT chk_bp_diastolic CHECK (diastolic_mmhg BETWEEN 10 AND 300),
  CONSTRAINT chk_bp_order CHECK (systolic_mmhg > diastolic_mmhg),
  CONSTRAINT chk_pulse CHECK (pulse_bpm IS NULL OR pulse_bpm BETWEEN 20 AND 300)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_unicode_ci;


CREATE TABLE IF NOT EXISTS food_items (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  user_id BIGINT UNSIGNED NOT NULL,
  name VARCHAR(150) NOT NULL,
  default_unit VARCHAR(20) NOT NULL DEFAULT 'g',
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  deleted_at DATETIME(6) NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_food_items_user_name (user_id, name),
  KEY idx_food_items_user_deleted (user_id, deleted_at),
  CONSTRAINT fk_food_items_user
    FOREIGN KEY (user_id)
    REFERENCES users (id)
    ON DELETE CASCADE
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_unicode_ci;


CREATE TABLE IF NOT EXISTS food_entries (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  user_id BIGINT UNSIGNED NOT NULL,
  food_item_id BIGINT UNSIGNED NULL,
  food_name VARCHAR(150) NOT NULL,
  consumed_at DATETIME(6) NOT NULL,
  amount_value DECIMAL(10,2) NOT NULL,
  amount_unit VARCHAR(20) NOT NULL,
  comment VARCHAR(1000) NULL,
  source VARCHAR(50) NOT NULL DEFAULT 'manual',
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  deleted_at DATETIME(6) NULL,
  PRIMARY KEY (id),
  KEY idx_food_user_time (user_id, consumed_at),
  KEY idx_food_user_deleted_time (user_id, deleted_at, consumed_at),
  CONSTRAINT fk_food_entries_user
    FOREIGN KEY (user_id)
    REFERENCES users (id)
    ON DELETE CASCADE,
  CONSTRAINT fk_food_entries_item
    FOREIGN KEY (food_item_id)
    REFERENCES food_items (id)
    ON DELETE SET NULL,
  CONSTRAINT chk_food_amount CHECK (amount_value > 0)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_unicode_ci;


CREATE TABLE IF NOT EXISTS audit_log (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  user_id BIGINT UNSIGNED NULL,
  action VARCHAR(100) NOT NULL,
  entity_type VARCHAR(50) NULL,
  entity_id BIGINT UNSIGNED NULL,
  request_id VARCHAR(64) NULL,
  ip_address VARCHAR(45) NULL,
  user_agent VARCHAR(255) NULL,
  details_json JSON NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  KEY idx_audit_user_time (user_id, created_at),
  KEY idx_audit_entity (entity_type, entity_id),
  CONSTRAINT fk_audit_user
    FOREIGN KEY (user_id)
    REFERENCES users (id)
    ON DELETE SET NULL
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_unicode_ci;


CREATE TABLE IF NOT EXISTS idempotency_keys (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  user_id BIGINT UNSIGNED NOT NULL,
  idempotency_key VARCHAR(64) NOT NULL,
  entity_type VARCHAR(50) NOT NULL,
  entity_id BIGINT UNSIGNED NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  UNIQUE KEY uq_idem_user_key (user_id, entity_type, idempotency_key),
  CONSTRAINT fk_idem_user
    FOREIGN KEY (user_id)
    REFERENCES users (id)
    ON DELETE CASCADE
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_unicode_ci;
