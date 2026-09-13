#!/bin/sh
set -Eeuo pipefail

REPO_URL="https://github.com/ceshbox-code/medical_diary.git"
INSTALL_DIR="${MEDICAL_DIARY_DIR:-/volume1/docker/medical_diary}"
BRANCH="${MEDICAL_DIARY_BRANCH:-main}"

log() { printf '\n[medical-diary] %s\n' "$*"; }
die() { printf '\n[medical-diary] ERROR: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Запустите скрипт от root. Например: sudo sh install_synology.sh"

command -v git >/dev/null 2>&1 || die "Не найден git. Установите Git Server/Git через Package Center."
command -v openssl >/dev/null 2>&1 || die "Не найден openssl. Он нужен для генерации секретов."

if command -v docker >/dev/null 2>&1; then
    DOCKER="docker"
else
    die "Не найден Docker. На Synology установите Container Manager и убедитесь, что команда docker доступна в PATH."
fi

if docker compose version >/dev/null 2>&1; then
    COMPOSE="$DOCKER compose"
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE="docker-compose"
else
    die "Не найден Docker Compose. Проверьте установку Container Manager."
fi

case "$INSTALL_DIR" in
    /volume1/docker/*) ;;
    *) die "Каталог установки должен находиться под /volume1/docker: $INSTALL_DIR" ;;
esac

PARENT_DIR=$(dirname "$INSTALL_DIR")
mkdir -p "$PARENT_DIR"

if [ -e "$INSTALL_DIR/.git" ]; then
    log "Обновляю существующий репозиторий: $INSTALL_DIR"
    git -C "$INSTALL_DIR" fetch --prune origin
    git -C "$INSTALL_DIR" checkout "$BRANCH"
    git -C "$INSTALL_DIR" pull --ff-only origin "$BRANCH"
elif [ -e "$INSTALL_DIR" ]; then
    # Никогда не удаляем существующий каталог: в нём могут находиться БД/бэкапы.
    if [ "$(find "$INSTALL_DIR" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]; then
        die "Каталог уже существует и не является git-репозиторием: $INSTALL_DIR. Ничего не удалено."
    fi
    log "Каталог пустой, клонирую репозиторий"
    rmdir "$INSTALL_DIR"
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
    ADMIN_PASSWORD=$(openssl rand -base64 24 | tr -dc 'A-Za-z0-9@#%+=_' | cut -c1-20)
    [ "${#ADMIN_PASSWORD}" -ge 16 ] || die "Не удалось сгенерировать пароль администратора"

    # Не перезаписываем строки, если формат .env.example когда-нибудь изменится.
    sed -i "s|^SECRET_KEY=.*$|SECRET_KEY=$SECRET_KEY|" .env
    sed -i "s|^ADMIN_USERNAME=.*$|ADMIN_USERNAME=admin|" .env
    sed -i "s|^ADMIN_PASSWORD=.*$|ADMIN_PASSWORD=$ADMIN_PASSWORD|" .env
    sed -i "s|^HTTP_PORT=.*$|HTTP_PORT=${HTTP_PORT:-8000}|" .env
    sed -i "s|^TZ=.*$|TZ=${TZ:-Europe/Moscow}|" .env
    sed -i "s|^SESSION_COOKIE_SECURE=.*$|SESSION_COOKIE_SECURE=false|" .env
    sed -i "s|^WA_RP_ID=.*$|WA_RP_ID=localhost|" .env
    sed -i "s|^WA_ORIGIN=.*$|WA_ORIGIN=http://localhost:${HTTP_PORT:-8000}|" .env

    chmod 600 .env
    printf '\n[medical-diary] Сгенерирован первоначальный пароль администратора: %s\n' "$ADMIN_PASSWORD"
    printf '[medical-diary] Сохранён в %s/.env — после первого входа смените его через настройки администратора.\n' "$INSTALL_DIR"
else
    log ".env уже существует — секреты и настройки не изменяю"
    chmod 600 .env 2>/dev/null || true
fi

# Если пользователь передал HTTP_PORT снаружи и .env создаётся впервые, он уже записан выше.
# Для существующего .env сознательно ничего не меняем.
HTTP_PORT_VALUE=$(awk -F= '$1=="HTTP_PORT" {print $2}' .env | tail -n1)
HTTP_PORT_VALUE=${HTTP_PORT_VALUE:-8000}

case "$HTTP_PORT_VALUE" in
    ''|*[!0-9]*) die "Некорректный HTTP_PORT в .env: $HTTP_PORT_VALUE" ;;
esac
[ "$HTTP_PORT_VALUE" -ge 1 ] && [ "$HTTP_PORT_VALUE" -le 65535 ] || die "HTTP_PORT вне диапазона 1..65535: $HTTP_PORT_VALUE"

if command -v ss >/dev/null 2>&1; then
    if ss -lnt 2>/dev/null | awk -v p=":$HTTP_PORT_VALUE" '$4 ~ p"$" {found=1} END {exit found ? 0 : 1}'; then
        # Контейнер проекта может уже занимать порт — это допустимо при обновлении.
        if ! docker ps --format '{{.Names}} {{.Ports}}' | grep -q "medical-diary-app"; then
            die "Порт $HTTP_PORT_VALUE уже занят другим процессом. Измените HTTP_PORT в $INSTALL_DIR/.env и повторите запуск."
        fi
    fi
fi

log "Проверяю compose-конфигурацию"
$COMPOSE config >/dev/null || die "docker compose config завершился с ошибкой"

log "Собираю и запускаю Medical Diary"
$COMPOSE up -d --build

log "Ожидаю health-check"
OK=0
for i in $(seq 1 60); do
    if curl -fsS --max-time 3 "http://127.0.0.1:${HTTP_PORT_VALUE}/health" >/dev/null 2>&1; then
        OK=1
        break
    fi
    sleep 2
done

if [ "$OK" -ne 1 ]; then
    printf '\n[medical-diary] Контейнер запущен, но health-check не прошёл.\n' >&2
    printf '[medical-diary] Последние логи:\n' >&2
    $COMPOSE logs --tail=100 >&2 || true
    die "Проверьте логи командой: cd '$INSTALL_DIR' && $COMPOSE logs -f"
fi

log "Установка завершена успешно"
printf '\nURL: http://<IP-SYNOLOGY>:%s/\n' "$HTTP_PORT_VALUE"
printf 'Каталог: %s\n' "$INSTALL_DIR"
printf 'База данных: %s/data/medical_diary.db\n' "$INSTALL_DIR"
printf 'Резервные копии: %s/backups/\n' "$INSTALL_DIR"
printf '\nПроверка: cd %s && %s ps\n' "$INSTALL_DIR" "$COMPOSE"
printf 'Логи:     cd %s && %s logs -f\n' "$INSTALL_DIR" "$COMPOSE"
printf '\nВажно: при обновлении повторный запуск этого скрипта сохраняет .env, data/ и backups/.\n'
