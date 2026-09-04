# Medical Diary

**Medical Diary** — self-hosted мобильный веб-сервис медицинского дневника для ежедневного учёта показателей здоровья с iPhone или другого смартфона.

Проект предназначен для самостоятельного контроля:

- уровня глюкозы крови натощак и после еды;
- артериального давления и пульса;
- съеденных продуктов с точным количеством;
- истории измерений;
- экспорта данных в PDF для врача.

Работает в Docker, подходит для Synology NAS и обычных Linux-серверов.

---

## Возможности

- Mobile-first интерфейс для iPhone и Safari
- Быстрый ввод одной рукой
- Автоматическое сохранение даты и точного времени записи
- История записей и фильтрация по датам
- Общая таблица всех медицинских данных
- Экспорт выбранного периода в PDF
- Постоянный Docker volume для хранения данных
- Резервное копирование базы данных
- Валидация медицинских значений (CHECK-ограничения в БД)
- Защита доступа логином и паролем
- Поддержка WebAuthn / Face ID при HTTPS
- Healthcheck контейнера
- Единый стартовый скрипт `launcher.sh`

---

## Архитектура

Текущая рабочая архитектура использует SQLite:

    Browser / iPhone Safari
            │
            ▼
    Flask + Gunicorn container
            │
            ▼
    SQLite database file in persistent volume

### Основные компоненты

| Компонент | Назначение |
|---|---|
| `app.py` | Основное Flask-приложение |
| `backup.py` | Создание консистентной копии SQLite базы |
| `Dockerfile` | Сборка Docker-образа приложения |
| `docker-compose.yml` | Запуск контейнера |
| `requirements.txt` | Python-зависимости |
| `static/` | Иконки, manifest, PDF.js и статические файлы |
| `data/` | Постоянное хранилище базы данных |
| `backups/` | Локальные резервные копии |
| `scripts/` | Скрипты резервного копирования и восстановления |
| `launcher.sh` | Единый интерактивный скрипт установки и управления |

### База данных

В рабочей версии используется **SQLite**. Файл базы данных:

- внутри контейнера: `/data/medical_diary.db`
- на хосте: `./data/medical_diary.db`

> ⚠️ Файл `data/medical_diary.db` содержит медицинские данные и не должен попадать в GitHub.

---

## Требования

### Для Synology

- Synology DSM 7+
- Установленный пакет **Container Manager**
- Включённый SSH-доступ (для установки через терминал)

### Для Linux-сервера

- Ubuntu / Debian / CentOS / Rocky / AlmaLinux
- Docker и Docker Compose Plugin
- Git и curl

### Для Windows

- Git for Windows
- Docker Desktop
- Запуск команд через **Open Git Bash here**

---

## Быстрая установка

### 1. Клонировать репозиторий

    git clone https://github.com/ceshbox-code/medical_diary.git
    cd medical_diary

### 2. Запустить единый стартовый скрипт

    chmod +x launcher.sh
    ./launcher.sh

Скрипт предложит выбрать язык:

    1) Русский
    2) English

После выбора появится меню:

    1) Развернуть приложение
    2) Запустить контейнеры
    3) Перезапустить контейнеры
    4) Остановить контейнеры
    5) Показать статус
    6) Показать логи
    7) Создать резервную копию
    8) Восстановить из копии
    9) Обновить до последней версии
    0) Выход

Для первой установки выберите: **1) Развернуть приложение**.

---

## Ручная установка без launcher.sh

### 1. Создать `.env`

    cp .env.example .env

Отредактируйте значения:

    SECRET_KEY=replace_with_long_random_secret
    ADMIN_USERNAME=admin
    ADMIN_PASSWORD=change_me
    HTTP_PORT=8000
    TZ=Europe/Moscow
    SESSION_COOKIE_SECURE=false
    WA_RP_ID=localhost
    WA_ORIGIN=http://localhost:8000
    DATABASE_PATH=/data/medical_diary.db

Для HTTPS-домена:

    SESSION_COOKIE_SECURE=true
    WA_RP_ID=diary.example.com
    WA_ORIGIN=https://diary.example.com

### 2. Создать директории

    mkdir -p data backups

### 3. Собрать и запустить контейнер

    docker compose build
    docker compose up -d

### 4. Проверить статус

    docker compose ps

### 5. Открыть приложение

    http://localhost:8000

Или с учётом порта из `.env`:

    http://SERVER_IP:HTTP_PORT

---

## Установка на Synology

    ssh root@YOUR_SYNOLOGY_HOST
    cd /volume1/docker
    git clone https://github.com/ceshbox-code/medical_diary.git
    cd medical_diary
    chmod +x launcher.sh
    ./launcher.sh

Выберите: `1) Русский` → `1) Развернуть приложение`.

---

## Настройка Reverse Proxy на Synology

Рекомендуется использовать HTTPS. В DSM:

**Control Panel → Login Portal → Advanced → Reverse Proxy**

Пример правила:

| Поле | Значение |
|---|---|
| Source protocol | HTTPS |
| Source hostname | diary.example.com |
| Source port | 443 |
| Destination protocol | HTTP |
| Destination hostname | localhost |
| Destination port | 8000 |

После настройки HTTPS в `.env` установите:

    SESSION_COOKIE_SECURE=true
    WA_RP_ID=diary.example.com
    WA_ORIGIN=https://diary.example.com

Затем перезапустите контейнер:

    docker compose restart

---

## Резервное копирование

### Через launcher

    ./launcher.sh

Выберите: **7) Создать резервную копию**.

### Вручную

    ./scripts/backup.sh

Бэкапы сохраняются в `./backups/` в формате:

    medical_diary_YYYYMMDD_HHMMSS.db

---

## Восстановление из резервной копии

### Через launcher

    ./launcher.sh

Выберите: **8) Восстановить из копии**.

### Вручную

    docker compose down
    cp backups/medical_diary_YYYYMMDD_HHMMSS.db data/medical_diary.db
    docker compose up -d

> ⚠️ Перед восстановлением сделайте свежую резервную копию текущей базы.

---

## Обновление проекта

### Через launcher

    ./launcher.sh

Выберите: **9) Обновить до последней версии**.

### Вручную

    git pull
    docker compose build
    docker compose up -d

---

## Проверка здоровья

    curl http://localhost:8000/health

Или через Docker:

    docker compose ps

---

## Логи

    docker compose logs -f

Через launcher: **6) Показать логи**.

---

## Безопасность

Рекомендации:

1. Никогда не публикуйте `.env`.
2. Никогда не публикуйте `data/medical_diary.db`.
3. Никогда не публикуйте содержимое `backups/`.
4. Используйте HTTPS для внешнего доступа.
5. Используйте сложный пароль администратора.
6. Регулярно проверяйте резервные копии.
7. Храните внешнюю копию бэкапов вне Synology/сервера.
8. Для Synology желательно включить шифрование папки или тома.
9. Для доступа извне используйте reverse proxy и TLS-сертификат.

### Что не должно попадать в GitHub

В репозитории не должно быть:

    .env
    data/medical_diary.db
    backups/*.db
    *.db
    *.sqlite
    @eaDir/
    Thumbs.db
    *.bak.*

Эти файлы исключены через `.gitignore`.

---

## GitHub Pages

Публичная страница проекта:

    https://ceshbox-code.github.io/medical_diary/index.html

Если страница не открывается, включите GitHub Pages:

**GitHub repository → Settings → Pages**

- Source: Deploy from a branch
- Branch: main
- Folder: / (root)

Затем нажмите **Save**.

---

## Лицензия

MIT License.

---

## Важное медицинское предупреждение

> ⚠️ Этот проект является инструментом личного учёта медицинских показателей и **не заменяет консультацию врача**.
>
> Не используйте данные приложения для самостоятельной постановки диагноза или изменения лечения без консультации со специалистом.
