#!/bin/sh
set -eu

REPO_URL="https://github.com/ceshbox-code/medical_diary.git"
BRANCH="${MEDICAL_DIARY_BRANCH:-main}"
DEFAULT_INSTALL_DIR="/volume1/docker/medical_diary"
DEFAULT_HTTP_PORT="8000"
DEFAULT_CONTAINER_NAME="medical-diary-app"
DEFAULT_TZ="Europe/Moscow"

# Файлы/каталоги, которые относятся к разработке и локальному рабочему
# месту, а не к самой серверной установке — даже если они окажутся
# внутри архива или git-репозитория, на сервере им не место.
# install_synology.sh — сам установщик (нужен только чтобы быть
# скачанным через curl); deploy.sh/deploy.conf — инструмент деплоя с
# git-репозитория на рабочей машине разработчика на GitHub, к серверу
# отношения не имеет.
DEV_ONLY_PATHS="install_synology.sh deploy.sh deploy.conf"

log()  { printf '\n[medical-diary] %s\n' "$*"; }
warn() { printf '\n[medical-diary] WARNING: %s\n' "$*" >&2; }
die()  { printf '\n[medical-diary] ERROR: %s\n' "$*" >&2; exit 1; }

read_tty() {
    _answer=""
    if [ ! -r /dev/tty ]; then
        die "Не удалось открыть /dev/tty для интерактивного ввода. Запустите скрипт из интерактивной SSH-сессии."
    fi
    IFS= read -r _answer < /dev/tty || true
    printf '%s' "$_answer"
}

prompt() {
    _prompt="$1"
    _default="${2:-}"
    if [ -n "$_default" ]; then
        printf '%s [%s]: ' "$_prompt" "$_default" >&2
    else
        printf '%s: ' "$_prompt" >&2
    fi
    read_tty
}

prompt_secret() {
    _prompt="$1"
    if [ -r /dev/tty ] && command -v stty >/dev/null 2>&1; then
        printf '%s: ' "$_prompt" >&2
        stty -echo < /dev/tty 2>/dev/null || true
        IFS= read -r _answer < /dev/tty || _answer=""
        stty echo < /dev/tty 2>/dev/null || true
        printf '\n' >&2
    else
        printf '%s: ' "$_prompt" >&2
        IFS= read -r _answer < /dev/tty || _answer=""
    fi
    printf '%s' "$_answer"
}

confirm() {
    _prompt="$1"
    _default="${2:-y}"
    case "$_default" in
        y) _suffix="[Y/n]" ;;
        *) _suffix="[y/N]" ;;
    esac
    printf '%s %s: ' "$_prompt" "$_suffix" >&2
    _answer=$(read_tty)
    _answer=$(printf '%s' "$_answer" | tr '[:upper:]' '[:lower:]')
    if [ -z "$_answer" ]; then
        _answer="$_default"
    fi
    [ "$_answer" = "y" ] || [ "$_answer" = "yes" ]
}

validate_port() {
    case "$1" in
        ''|*[!0-9]*) return 1 ;;
    esac
    [ "$1" -ge 1 ] 2>/dev/null && [ "$1" -le 65535 ] 2>/dev/null
}

validate_install_dir() {
    case "$1" in
        /volume1/docker/*) ;;
        *) return 1 ;;
    esac
    case "$1" in
        */|*/.|*/..|/volume1/docker) return 1 ;;
    esac
    return 0
}

validate_domain() {
    _domain="$1"
    [ -n "$_domain" ] || return 0
    case "$_domain" in
        http://*|https://*|*/|\?*|*\#*|*\ *|.*|*.) return 1 ;;
    esac
    case "$_domain" in
        *..*|*[!A-Za-z0-9.-]*) return 1 ;;
    esac
    return 0
}

validate_container_name() {
    case "$1" in
        ''|.*|*-|*_) return 1 ;;
    esac
    case "$1" in
        [A-Za-z0-9]*) ;;
        *) return 1 ;;
    esac
    case "$1" in
        *[!A-Za-z0-9._-]*) return 1 ;;
    esac
    [ "${#1}" -le 64 ] || return 1
    return 0
}

# Безопасно записывает/обновляет переменную в .env.
# ВСЕГДА с переводом строки — это устраняет «прилипание» значений.
upsert_env_var() {
    _file="$1"
    _key="$2"
    _val="$3"
    
    # Гарантируем, что файл заканчивается переводом строки
    if [ -s "$_file" ]; then
        # Проверяем последний байт файла
        _last_byte=$(tail -c 1 "$_file" | od -An1 | tr -d ' ')
        if [ "$_last_byte" != "10" ]; then
            printf '\n' >> "$_file"
        fi
    fi
    
    if grep -q "^${_key}=" "$_file"; then
        sed -i "s|^${_key}=.*$|${_key}=${_val}|" "$_file"
    else
        printf '%s=%s\n' "$_key" "$_val" >> "$_file"
    fi
}

# Копирует переменную из .env.example в .env, сохраняя существующее значение
# в .env, если оно уже задано.
copy_or_keep_env_var() {
    _env="$1"
    _example="$2"
    _key="$3"
    if grep -q "^${_key}=" "$_env"; then
        return 0
    fi
    _val=$(awk -F= -v k="$_key" '$1==k {sub(/^[^=]*=/,""); print; exit}' "$_example")
    upsert_env_var "$_env" "$_key" "$_val"
}

require_cmd() {
    command -v "$1" >/dev/null 2>&1 || die "$2"
}

[ "$(id -u)" -eq 0 ] || die "Запустите скрипт от root (SSH на Synology: sudo -i, затем повторите)."
require_cmd openssl "Не найден openssl."
require_cmd curl    "Не найден curl."
require_cmd sed     "Не найден sed."
require_cmd awk     "Не найден awk."
require_cmd find    "Не найден find."

if command -v docker >/dev/null 2>&1; then
    DOCKER="docker"
else
    die "Не найден Docker. Установите Container Manager и убедитесь, что команда docker доступна в PATH."
fi

if "$DOCKER" compose version >/dev/null 2>&1; then
    COMPOSE="$DOCKER compose"
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE="docker-compose"
else
    die "Не найден Docker Compose."
fi

printf '\n=============================================\n'
printf ' Medical Diary — установка на Synology DSM\n'
printf '=============================================\n'

INSTALL_DIR=$(prompt "Папка установки (только /volume1/docker/...)" "${MEDICAL_DIARY_DIR:-$DEFAULT_INSTALL_DIR}")
validate_install_dir "$INSTALL_DIR" || die "Недопустимая папка. Используйте путь вида /volume1/docker/medical_diary."

HTTP_PORT=$(prompt "Внешний порт приложения (в контейнере всегда 8000)" "${MEDICAL_DIARY_HTTP_PORT:-$DEFAULT_HTTP_PORT}")
validate_port "$HTTP_PORT" || die "Недопустимый порт: $HTTP_PORT"

CONTAINER_NAME=$(prompt "Имя Docker-контейнера" "${MEDICAL_DIARY_CONTAINER_NAME:-$DEFAULT_CONTAINER_NAME}")
validate_container_name "$CONTAINER_NAME" || die "Недопустимое имя контейнера. Допустимы буквы, цифры, '.', '_' и '-', начиная с буквы или цифры, длина до 64."

TZ_VALUE=$(prompt "Часовой пояс контейнера (IANA)" "${MEDICAL_DIARY_TZ:-$DEFAULT_TZ}")

DOMAIN=$(prompt "Доменное имя для доступа и Face ID/WebAuthn (пусто — без домена)" "${MEDICAL_DIARY_DOMAIN:-}")
validate_domain "$DOMAIN" || die "Недопустимое доменное имя. Введите только hostname, например diary.example.ru, без http:// и пути."

if [ -n "$DOMAIN" ]; then
    HTTPS_ORIGIN="https://$DOMAIN"
    if confirm "Домен будет опубликован через HTTPS reverse proxy в DSM?" "y"; then
        SESSION_SECURE="true"
        WA_ORIGIN="$HTTPS_ORIGIN"
        WA_RP_ID="$DOMAIN"
    else
        warn "HTTP-домен не подходит для WebAuthn/Face ID."
        if confirm "Записать origin как http://$DOMAIN и продолжить?" "n"; then
            SESSION_SECURE="false"
            WA_ORIGIN="http://$DOMAIN"
            WA_RP_ID="$DOMAIN"
        else
            die "Укажите HTTPS reverse proxy и запустите установщик повторно."
        fi
    fi
else
    SESSION_SECURE="false"
    WA_RP_ID="localhost"
    WA_ORIGIN="http://localhost:$HTTP_PORT"
    warn "Домен не задан. Доступ по http://IP-Synology:$HTTP_PORT. WebAuthn через HTTP работать не будет."
fi

GIGACHAT_ENABLED="false"
GIGACHAT_AUTH_KEY=""
if confirm "Использовать ИИ-ассистент GigaChat (опционально)?" "n"; then
    GIGACHAT_ENABLED="true"
    GIGACHAT_AUTH_KEY=$(prompt_secret "Вставьте GIGACHAT_AUTH_KEY (или пусто — заполните позже в .env)")
    if [ -z "$GIGACHAT_AUTH_KEY" ]; then
        warn "AUTH_KEY не задан. Ассистент будет отключён до ручного заполнения GIGACHAT_AUTH_KEY в .env."
        GIGACHAT_ENABLED="false"
    fi
fi

printf '\nБудет установлено:\n'
printf '  Папка:        %s\n' "$INSTALL_DIR"
printf '  Порт:         %s → 8000\n' "$HTTP_PORT"
printf '  Контейнер:    %s\n' "$CONTAINER_NAME"
printf '  Часовой пояс: %s\n' "$TZ_VALUE"
if [ -n "$DOMAIN" ]; then
    printf '  Домен:        %s\n' "$DOMAIN"
    printf '  WebAuthn:     %s (RP ID: %s)\n' "$WA_ORIGIN" "$WA_RP_ID"
else
    printf '  Домен:        не задан\n'
fi
printf '  GigaChat AI:  %s\n' "$GIGACHAT_ENABLED"
printf '\n'
confirm "Продолжить?" "y" || die "Установка отменена."

PARENT_DIR=$(dirname "$INSTALL_DIR")
mkdir -p "$PARENT_DIR"

if [ -e "$INSTALL_DIR/.git" ]; then
    require_cmd git "Найден Git-репозиторий, но команда git недоступна."
    log "Обновляю существующий репозиторий: $INSTALL_DIR"

    # Предыдущий запуск этого же установщика мог удалить служебные файлы
    # разработки без коммита (см. очистку ниже, после cd "$INSTALL_DIR").
    # Восстанавливаем их из индекса ПЕРЕД проверкой на чистоту репозитория —
    # иначе эта собственная уборка ошибочно считалась бы "незакоммиченными
    # изменениями" и блокировала бы каждое следующее обновление.
    # ВАЖНО: восстанавливаем строго ПО ОДНОМУ пути за раз — если передать
    # все пути одной командой `git checkout -- a b c`, а хотя бы один из
    # них git не отслеживает (например, deploy.conf никогда не был
    # закоммичен), падает вся команда целиком и НИ ОДИН файл не
    # восстанавливается, даже те, что реально отслеживались.
    for _p in $DEV_ONLY_PATHS; do
        git -C "$INSTALL_DIR" checkout -- "$_p" >/dev/null 2>&1 || true
    done

    if ! git -C "$INSTALL_DIR" diff --quiet || ! git -C "$INSTALL_DIR" diff --cached --quiet; then
        die "В репозитории есть незакоммиченные изменения."
    fi
    git -C "$INSTALL_DIR" fetch --prune origin
    git -C "$INSTALL_DIR" checkout "$BRANCH"
    git -C "$INSTALL_DIR" pull --ff-only origin "$BRANCH"
elif [ -e "$INSTALL_DIR" ]; then
    if [ -n "$(find "$INSTALL_DIR" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]; then
        die "Каталог существует и не является git-репозиторием: $INSTALL_DIR"
    fi
    rmdir "$INSTALL_DIR"
    log "Скачиваю архив репозитория"
    TMP_ARCHIVE="$(mktemp /tmp/medical_diary.XXXXXX.tar.gz)"
    trap 'rm -f "$TMP_ARCHIVE"' EXIT HUP INT TERM
    curl -fsSL "https://github.com/ceshbox-code/medical_diary/archive/refs/heads/$BRANCH.tar.gz" -o "$TMP_ARCHIVE" \
        || die "Не удалось скачать репозиторий GitHub."
    mkdir -p "$INSTALL_DIR"
    tar -xzf "$TMP_ARCHIVE" -C "$INSTALL_DIR" --strip-components=1 \
        || die "Не удалось распаковать репозиторий."
    rm -f "$TMP_ARCHIVE"
    trap - EXIT HUP INT TERM
else
    log "Скачиваю архив репозитория"
    TMP_ARCHIVE="$(mktemp /tmp/medical_diary.XXXXXX.tar.gz)"
    trap 'rm -f "$TMP_ARCHIVE"' EXIT HUP INT TERM
    curl -fsSL "https://github.com/ceshbox-code/medical_diary/archive/refs/heads/$BRANCH.tar.gz" -o "$TMP_ARCHIVE" \
        || die "Не удалось скачать репозиторий GitHub."
    mkdir -p "$INSTALL_DIR"
    tar -xzf "$TMP_ARCHIVE" -C "$INSTALL_DIR" --strip-components=1 \
        || die "Не удалось распаковать репозиторий."
    rm -f "$TMP_ARCHIVE"
    trap - EXIT HUP INT TERM
fi

cd "$INSTALL_DIR"
[ -f docker-compose.yml ] || die "В репозитории отсутствует docker-compose.yml"
[ -f Dockerfile ]         || die "В репозитории отсутствует Dockerfile"
[ -f .env.example ]       || die "В репозитории отсутствует .env.example"

# Служебные файлы разработки — не относятся к серверной установке, даже
# если случайно оказались в архиве/репозитории (сам установщик нужен
# только для скачивания через curl, deploy.sh и deploy.conf — это
# инструменты локального рабочего места разработчика, а не сервера).
# Удаляем их безусловно, на каждом запуске, а не только при первой
# установке — на случай, если апстрим когда-нибудь снова их закоммитит.
for _p in $DEV_ONLY_PATHS; do
    if [ -e "$INSTALL_DIR/$_p" ]; then
        rm -f "$INSTALL_DIR/$_p"
        log "Удалено (инструмент разработки, не нужен на сервере): $_p"
    fi
done

mkdir -p data backups
chmod 700 data backups 2>/dev/null || true

ENV_FILE=".env"
EXAMPLE_FILE=".env.example"

# Генерация секретов (только при первом запуске).
# openssl rand -hex 32 даёт ровно 64 символа [0-9a-f] — ничего другого там быть не может.
if [ ! -f "$ENV_FILE" ]; then
    log "Создаю $ENV_FILE из $EXAMPLE_FILE"
    cp "$EXAMPLE_FILE" "$ENV_FILE"

    SECRET_KEY=$(openssl rand -hex 32) \
        || die "Не удалось сгенерировать SECRET_KEY."

    ADMIN_PASSWORD=""
    attempts=0
    while [ "${#ADMIN_PASSWORD}" -lt 24 ] && [ "$attempts" -lt 20 ]; do
        ADMIN_PASSWORD=$(openssl rand -base64 48 \
            | tr -dc 'A-Za-z0-9!@#%^&*_+-=' \
            | cut -c1-28)
        attempts=$((attempts + 1))
    done
    [ "${#ADMIN_PASSWORD}" -ge 24 ] || die "Не удалось сгенерировать пароль администратора."

    # Все значения — через upsert_env_var, гарантирован перевод строки.
    upsert_env_var "$ENV_FILE" "SECRET_KEY"            "$SECRET_KEY"
    upsert_env_var "$ENV_FILE" "ADMIN_USERNAME"         "admin"
    upsert_env_var "$ENV_FILE" "ADMIN_PASSWORD"         "$ADMIN_PASSWORD"
    upsert_env_var "$ENV_FILE" "DATABASE_PATH"          "/app/data/medical_diary.db"
    upsert_env_var "$ENV_FILE" "HTTP_PORT"              "$HTTP_PORT"
    upsert_env_var "$ENV_FILE" "TZ"                     "$TZ_VALUE"
    upsert_env_var "$ENV_FILE" "SESSION_COOKIE_SECURE"  "$SESSION_SECURE"
    upsert_env_var "$ENV_FILE" "WA_RP_ID"               "$WA_RP_ID"
    upsert_env_var "$ENV_FILE" "WA_ORIGIN"              "$WA_ORIGIN"
    upsert_env_var "$ENV_FILE" "FONT_PATH"              "/app/fonts/DejaVuSans.ttf"
    upsert_env_var "$ENV_FILE" "CONTAINER_NAME"         "$CONTAINER_NAME"
    upsert_env_var "$ENV_FILE" "GIGACHAT_AI_ENABLED"    "$GIGACHAT_ENABLED"
    if [ -n "$GIGACHAT_AUTH_KEY" ]; then
        upsert_env_var "$ENV_FILE" "GIGACHAT_AUTH_KEY" "$GIGACHAT_AUTH_KEY"
    fi

    # Копируем любые дополнительные переменные из .env.example,
    # если их ещё нет в .env (модель, scope, таймаут и т.д.).
    for _k in GIGACHAT_MODEL GIGACHAT_SCOPE GIGACHAT_TIMEOUT_SECONDS GIGACHAT_MAX_ENTRIES; do
        copy_or_keep_env_var "$ENV_FILE" "$EXAMPLE_FILE" "$_k"
    done

    chmod 600 "$ENV_FILE"
    printf '\n[medical-diary] Пароль администратора (сохраните его!):\n  %s\n' "$ADMIN_PASSWORD"
    printf '[medical-diary] Сохранён в %s с правами 600.\n' "$(pwd)/$ENV_FILE"
else
    log "$ENV_FILE уже существует — секреты и пароли НЕ меняю."
    upsert_env_var "$ENV_FILE" "HTTP_PORT"              "$HTTP_PORT"
    upsert_env_var "$ENV_FILE" "TZ"                     "$TZ_VALUE"
    upsert_env_var "$ENV_FILE" "SESSION_COOKIE_SECURE"  "$SESSION_SECURE"
    upsert_env_var "$ENV_FILE" "WA_RP_ID"               "$WA_RP_ID"
    upsert_env_var "$ENV_FILE" "WA_ORIGIN"              "$WA_ORIGIN"
    upsert_env_var "$ENV_FILE" "CONTAINER_NAME"         "$CONTAINER_NAME"
    chmod 600 "$ENV_FILE" 2>/dev/null || true
fi

HTTP_PORT_VALUE=$(awk -F= '$1=="HTTP_PORT" {print $2}' "$ENV_FILE" | tail -n1)
HTTP_PORT_VALUE=${HTTP_PORT_VALUE:-8000}
validate_port "$HTTP_PORT_VALUE" || die "Некорректный HTTP_PORT в $ENV_FILE: $HTTP_PORT_VALUE"

if command -v ss >/dev/null 2>&1; then
    if ss -lnt 2>/dev/null | awk -v p=":$HTTP_PORT_VALUE" '$4 ~ p"$" {found=1} END {exit found ? 0 : 1}'; then
        if ! "$DOCKER" ps --format '{{.Names}}' | grep -qx "$CONTAINER_NAME"; then
            die "Порт $HTTP_PORT_VALUE уже занят другим процессом."
        fi
    fi
fi

log "Проверяю compose-конфигурацию"
$COMPOSE config >/dev/null || die "docker compose config завершился с ошибкой."

log "Собираю и запускаю Medical Diary"
$COMPOSE up -d --build

log "Ожидаю готовность приложения"
OK=0
i=1
while [ "$i" -le 60 ]; do
    if curl -fsS --max-time 3 "http://127.0.0.1:$HTTP_PORT_VALUE/health" >/dev/null 2>&1; then
        OK=1
        break
    fi
    sleep 2
    i=$((i + 1))
done

if [ "$OK" -ne 1 ]; then
    printf '\n[medical-diary] Контейнер запущен, но health-check не прошёл.\n' >&2
    $COMPOSE ps >&2 || true
    $COMPOSE logs --tail=100 >&2 || true
    die "Проверьте логи: cd '$INSTALL_DIR' && $COMPOSE logs -f"
fi

# Устанавливаем утилиту управления в системный PATH
if [ -f scripts/mdctl.sh ]; then
    cp scripts/mdctl.sh /usr/local/bin/mdctl
    chmod +x /usr/local/bin/mdctl
    log "Установлена утилита: mdctl (доступна из любой папки)"
fi

log "Установка завершена успешно"

printf '\n---------------------------------------------\n'
printf ' Локальный URL: http://<IP-SYNOLOGY>:%s/\n' "$HTTP_PORT_VALUE"
if [ -n "$DOMAIN" ]; then
    printf ' Домен:         %s\n' "$WA_ORIGIN"
    printf ' WebAuthn RP:   %s\n' "$WA_RP_ID"
fi
printf ' Контейнер:     %s\n' "$CONTAINER_NAME"
printf ' Каталог:       %s\n' "$INSTALL_DIR"
printf ' База данных:   %s/data/medical_diary.db\n' "$INSTALL_DIR"
printf ' Бэкапы:        %s/backups/\n' "$INSTALL_DIR"
printf -- '---------------------------------------------\n'