#!/bin/sh
set -eu

REPO_URL="https://github.com/ceshbox-code/medical_diary.git"
BRANCH="${MEDICAL_DIARY_BRANCH:-main}"
DEFAULT_INSTALL_DIR="/volume1/docker/medical_diary"
DEFAULT_HTTP_PORT="8000"
DEFAULT_TZ="UTC"

log() { printf '\n[medical-diary] %s\n' "$*"; }
warn() { printf '\n[medical-diary] WARNING: %s\n' "$*" >&2; }
die() { printf '\n[medical-diary] ERROR: %s\n' "$*" >&2; exit 1; }

prompt() {
    _prompt="$1"
    _default="${2:-}"
    if [ -n "$_default" ]; then
        printf '%s [%s]: ' "$_prompt" "$_default" >&2
    else
        printf '%s: ' "$_prompt" >&2
    fi
    IFS= read -r _answer || true
    if [ -z "$_answer" ]; then
        _answer="$_default"
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
    IFS= read -r _answer || true
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

require_cmd() {
    command -v "$1" >/dev/null 2>&1 || die "$2"
}

[ "$(id -u)" -eq 0 ] || die "Запустите скрипт от root (SSH на Synology: sudo -i, затем повторите)."

require_cmd git "Не найден git. Установите Git Server/Git через Package Center."
require_cmd openssl "Не найден openssl."
require_cmd curl "Не найден curl."
require_cmd sed "Не найден sed."
require_cmd awk "Не найден awk."

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
    die "Не найден Docker Compose. В актуальном Synology Container Manager должна быть доступна команда 'docker compose'."
fi

printf '\n=============================================\n'
printf ' Medical Diary — установка на Synology DSM\n'
printf ' Container Manager / Docker Compose\n'
printf '=============================================\n\n'

INSTALL_DIR=$(prompt "Папка установки (только /volume1/docker/...)" "${MEDICAL_DIARY_DIR:-$DEFAULT_INSTALL_DIR}")
validate_install_dir "$INSTALL_DIR" || die "Недопустимая папка. Используйте путь вида /volume1/docker/medical_diary."

HTTP_PORT=$(prompt "Внешний порт приложения (в контейнере порт 8000)" "${MEDICAL_DIARY_HTTP_PORT:-$DEFAULT_HTTP_PORT}")
validate_port "$HTTP_PORT" || die "Недопустимый порт: $HTTP_PORT"

DOMAIN=$(prompt "Доменное имя для доступа и Face ID/WebAuthn (пусто — без домена)" "${MEDICAL_DIARY_DOMAIN:-}")
validate_domain "$DOMAIN" || die "Недопустимое доменное имя. Введите только hostname, например diary.example.ru, без http://, https:// и пути."

if [ -n "$DOMAIN" ]; then
    HTTPS_ORIGIN="https://$DOMAIN"
    if confirm "Домен будет опубликован через HTTPS reverse proxy в DSM?" "y"; then
        SESSION_SECURE="true"
        WA_ORIGIN="$HTTPS_ORIGIN"
        WA_RP_ID="$DOMAIN"
    else
        warn "HTTP-домен не является подходящей конфигурацией для WebAuthn/Face ID в обычном браузере."
        if confirm "Всё равно записать origin как http://$DOMAIN и продолжить?" "n"; then
            SESSION_SECURE="false"
            WA_ORIGIN="http://$DOMAIN"
            WA_RP_ID="$DOMAIN"
        else
            die "Укажите HTTPS reverse proxy для домена и запустите установщик повторно."
        fi
    fi
else
    SESSION_SECURE="false"
    WA_RP_ID="localhost"
    WA_ORIGIN="http://localhost:$HTTP_PORT"
    warn "Домен не задан. Доступ будет по http://IP-Synology:$HTTP_PORT. Face ID/WebAuthn через этот HTTP-адрес работать не будет."
fi

printf '\nБудет установлено:\n'
printf '  Папка:       %s\n' "$INSTALL_DIR"
printf '  Порт:        %s:8000\n' "$HTTP_PORT"
if [ -n "$DOMAIN" ]; then
    printf '  Домен:       %s\n' "$DOMAIN"
    printf '  WebAuthn:    %s (RP ID: %s)\n' "$WA_ORIGIN" "$WA_RP_ID"
else
    printf '  Домен:       не задан\n'
fi
printf '\n'

confirm "Продолжить?" "y" || die "Установка отменена."

PARENT_DIR=$(dirname "$INSTALL_DIR")
mkdir -p "$PARENT_DIR"

if [ -e "$INSTALL_DIR/.git" ]; then
    log "Обновляю существующий репозиторий: $INSTALL_DIR"
    if ! git -C "$INSTALL_DIR" diff --quiet || ! git -C "$INSTALL_DIR" diff --cached --quiet; then
        die "В репозитории есть незакоммиченные изменения. Ничего не перезаписываю; сохраните изменения или уберите их и повторите."
    fi
    git -C "$INSTALL_DIR" fetch --prune origin
    git -C "$INSTALL_DIR" checkout "$BRANCH"
    git -C "$INSTALL_DIR" pull --ff-only origin "$BRANCH"
elif [ -e "$INSTALL_DIR" ]; then
    if [ -n "$(find "$INSTALL_DIR" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]; then
        die "Каталог уже существует и не является git-репозиторием: $INSTALL_DIR. Ничего не удалено."
    fi
    rmdir "$INSTALL_DIR"
    log "Каталог пустой — клонирую репозиторий"
    git clone --branch "$BRANCH" --single-branch "$REPO_URL" "$INSTALL_DIR"
else
    log "Клонирую репозиторий в $INSTALL_DIR"
    git clone --branch "$BRANCH" --single-branch "$REPO_URL" "$INSTALL_DIR"
fi

cd "$INSTALL_DIR"

[ -f docker-compose.yml ] || die "В репозитории отсутствует docker-compose.yml"
[ -f Dockerfile ] || die "В репозитории отсутствует Dockerfile"
[ -f .env.example ] || die "В репозитории отсутствует .env.example"

mkdir -p data backups
chmod 700 data backups 2>/dev/null || true

if [ ! -f .env ]; then
    log "Создаю .env из .env.example"
    cp .env.example .env

    SECRET_KEY=$(openssl rand -hex 32) || die "Не удалось сгенерировать SECRET_KEY"

    ADMIN_PASSWORD=""
    attempts=0
    while [ "${#ADMIN_PASSWORD}" -lt 20 ] && [ "$attempts" -lt 10 ]; do
        ADMIN_PASSWORD=$(openssl rand -base64 48 | tr -dc 'A-Za-z0-9@#%+=_' | cut -c1-24)
        attempts=$((attempts + 1))
    done
    [ "${#ADMIN_PASSWORD}" -ge 20 ] || die "Не удалось сгенерировать пароль администратора."

    sed -i "s|^SECRET_KEY=.*$|SECRET_KEY=$SECRET_KEY|" .env
    sed -i "s|^ADMIN_USERNAME=.*$|ADMIN_USERNAME=admin|" .env
    sed -i "s|^ADMIN_PASSWORD=.*$|ADMIN_PASSWORD=$ADMIN_PASSWORD|" .env
    sed -i "s|^HTTP_PORT=.*$|HTTP_PORT=$HTTP_PORT|" .env
    sed -i "s|^TZ=.*$|TZ=$DEFAULT_TZ|" .env
    sed -i "s|^SESSION_COOKIE_SECURE=.*$|SESSION_COOKIE_SECURE=$SESSION_SECURE|" .env
    sed -i "s|^WA_RP_ID=.*$|WA_RP_ID=$WA_RP_ID|" .env
    sed -i "s|^WA_ORIGIN=.*$|WA_ORIGIN=$WA_ORIGIN|" .env

    chmod 600 .env
    printf '\n[medical-diary] Первоначальный пароль администратора:\n%s\n' "$ADMIN_PASSWORD"
    printf '[medical-diary] Он также сохранён в %s/.env с правами 600.\n' "$INSTALL_DIR"
else
    log ".env уже существует — секреты не меняю."

    # Обновляем только сетевые параметры, которые пользователь явно выбрал.
    # Остальные настройки (GigaChat, backup, admin и т.д.) сохраняются.
    if grep -q '^HTTP_PORT=' .env; then
        sed -i "s|^HTTP_PORT=.*$|HTTP_PORT=$HTTP_PORT|" .env
    else
        printf '\nHTTP_PORT=%s\n' "$HTTP_PORT" >> .env
    fi

    if grep -q '^SESSION_COOKIE_SECURE=' .env; then
        sed -i "s|^SESSION_COOKIE_SECURE=.*$|SESSION_COOKIE_SECURE=$SESSION_SECURE|" .env
    else
        printf 'SESSION_COOKIE_SECURE=%s\n' "$SESSION_SECURE" >> .env
    fi

    if grep -q '^WA_RP_ID=' .env; then
        sed -i "s|^WA_RP_ID=.*$|WA_RP_ID=$WA_RP_ID|" .env
    else
        printf 'WA_RP_ID=%s\n' "$WA_RP_ID" >> .env
    fi

    if grep -q '^WA_ORIGIN=' .env; then
        sed -i "s|^WA_ORIGIN=.*$|WA_ORIGIN=$WA_ORIGIN|" .env
    else
        printf 'WA_ORIGIN=%s\n' "$WA_ORIGIN" >> .env
    fi

    chmod 600 .env 2>/dev/null || true
fi

HTTP_PORT_VALUE=$(awk -F= '$1=="HTTP_PORT" {print $2}' .env | tail -n1)
HTTP_PORT_VALUE=${HTTP_PORT_VALUE:-8000}
validate_port "$HTTP_PORT_VALUE" || die "Некорректный HTTP_PORT в .env: $HTTP_PORT_VALUE"

if command -v ss >/dev/null 2>&1; then
    if ss -lnt 2>/dev/null | awk -v p=":$HTTP_PORT_VALUE" '$4 ~ p"$" {found=1} END {exit found ? 0 : 1}'; then
        if ! "$DOCKER" ps --format '{{.Names}}' | grep -qx 'medical-diary-app'; then
            die "Порт $HTTP_PORT_VALUE уже занят другим процессом. Выберите другой внешний порт."
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
    printf '[medical-diary] Статус:\n' >&2
    $COMPOSE ps >&2 || true
    printf '[medical-diary] Последние логи:\n' >&2
    $COMPOSE logs --tail=100 >&2 || true
    die "Проверьте логи: cd '$INSTALL_DIR' && $COMPOSE logs -f"
fi

log "Установка завершена успешно"
printf '\n---------------------------------------------\n'
printf ' Локальный URL: http://<IP-SYNOLOGY>:%s/\n' "$HTTP_PORT_VALUE"
if [ -n "$DOMAIN" ]; then
    printf ' Домен:         %s\n' "$WA_ORIGIN"
    printf ' WebAuthn RP:   %s\n' "$WA_RP_ID"
fi
printf ' Каталог:       %s\n' "$INSTALL_DIR"
printf ' База данных:   %s/data/medical_diary.db\n' "$INSTALL_DIR"
printf ' Бэкапы:        %s/backups/\n' "$INSTALL_DIR"
printf '---------------------------------------------\n'
printf '\nКоманды:\n'
printf '  cd "%s" && %s ps\n' "$INSTALL_DIR" "$COMPOSE"
printf '  cd "%s" && %s logs -f\n' "$INSTALL_DIR" "$COMPOSE"
printf '\nПовторный запуск обновляет код, но сохраняет .env, data/ и backups/.\n'
