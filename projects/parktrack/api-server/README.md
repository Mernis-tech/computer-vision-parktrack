# ParkTrack API Server

Backend для ParkTrack: авторизация, пользователи, партнеры, камеры, зоны парковки, источники данных, наблюдения занятости, прогнозы и маршруты. API написан под админ-панель/лейбелер и следует контрактам `/api/v1`.

## Что есть в backend

- Auth: регистрация, логин, logout, текущая сессия, восстановление пароля через reset-link.
- Users: список пользователей, профиль, смена пароля, создание и админское редактирование.
- Partners: партнеры, сотрудники партнера и права доступа внутри партнера.
- Cameras: список, карта, создание/редактирование, snapshot камеры.
- Zones: парковочные зоны, геометрия на кадре и на карте, фильтры по типу, статусу, доступности и платности.
- Sources: подключенные источники данных.
- Occupancy: наблюдения занятости парковочных зон.
- Forecasts: прогнозные точки по занятости/доступности.
- Routing: сохраненные маршруты и поиск маршрута.
- Admin: агрегированные админские представления для мониторинга.
- System: health-check и версия API.

## Основные маршруты

Все бизнес-ручки доступны под базовым префиксом:

```text
http://localhost:8000/api/v1
```

Ключевые группы:

```text
GET    /api/v1/health
GET    /api/v1/version

/api/v1/auth
/api/v1/users
/api/v1/partners
/api/v1/cameras
/api/v1/zones
/api/v1/sources
/api/v1/occupancy
/api/v1/forecasts
/api/v1/routing
/api/v1/admin
```

FastAPI также отдает интерактивную документацию:

```text
http://localhost:8000/docs
http://localhost:8000/openapi.json
```

## Стек

- Python 3.10
- FastAPI
- SQLAlchemy 2
- PostgreSQL
- Pydantic
- Uvicorn
- Docker Compose
- SQL migrations через `migrate/migrate`
- Staircase-проверка миграций через `seqwall`

## Окружение

Скопируйте пример окружения:

```powershell
cd C:\phyproject\api-server
Copy-Item example.env .env
```

Основные переменные:

```text
POSTGRES_DB=cars_db
POSTGRES_USER=user
POSTGRES_PASSWORD=Passw0rd
POSTGRES_PORT=5252
POSTGRES_TEST_DB=cars_test_db

DATABASE_HOST_URL=postgresql://user:Passw0rd@postgres:5432
DATABASE_URL=postgresql://user:Passw0rd@postgres:5432/cars_db?sslmode=disable
```

Внутри Docker Compose backend ходит в Postgres по host `postgres:5432`. Если запускаете `uvicorn` локально на Windows, используйте host-порт из compose:

```powershell
$env:DATABASE_URL = "postgresql://user:Passw0rd@localhost:5252/cars_db?sslmode=disable"
```

Переменные восстановления пароля:

```text
PASSWORD_RESET_LOGIN_URL=http://localhost:5173/#/password-reset
PASSWORD_RESET_TTL_MINUTES=30
PASSWORD_RESET_RETURN_TOKEN=0

SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USERNAME=parktrack@example.com
SMTP_PASSWORD=secret
SMTP_FROM_EMAIL=parktrack@example.com
SMTP_FROM_NAME=ParkTrack
SMTP_USE_TLS=1
SMTP_USE_SSL=0
```

`PASSWORD_RESET_LOGIN_URL` должен указывать на страницу frontend, куда пользователь попадает из письма. В production `PASSWORD_RESET_RETURN_TOKEN=0`, чтобы reset-token не возвращался в JSON-ответе API.

Не коммитьте реальные `.env`, SMTP-креды и локальные данные из `data/`.

## Локальный запуск

Типичный dev-сценарий: база и миграции в Docker, API локально через `uvicorn`.

```powershell
cd C:\phyproject\api-server

python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

Copy-Item example.env .env
docker compose up -d postgres
docker compose up migrate

$env:DATABASE_URL = "postgresql://user:Passw0rd@localhost:5252/cars_db?sslmode=disable"
uvicorn src.main:app --host 0.0.0.0 --port 8000
```

После запуска проверьте:

```powershell
Invoke-RestMethod http://localhost:8000/api/v1/health
Invoke-RestMethod http://localhost:8000/api/v1/version
```

Ожидаемо:

```json
{ "status": "healthy", "database": "connected" }
```

## Запуск через Docker Compose

Можно поднять весь backend-стек:

```powershell
cd C:\phyproject\api-server
Copy-Item example.env .env
docker compose up --build app
```

Текущий `docker-compose.yml` не публикует порт `app` наружу. Для локальной разработки проще запускать API через `uvicorn`, либо добавить в `app` секцию `ports`, если нужно открыть контейнерный API с хоста.

## Миграции

SQL-миграции лежат в `migrations/`. Для staircase-проверки также используется каталог `migrations/up/`.

Применить все миграции:

```powershell
docker compose up migrate
```

Применить до конкретной версии:

```env
MIGRATION_VERSION=7
```

После этого снова запустите:

```powershell
docker compose up migrate
```

Мигратор пересоздает test DB, прогоняет staircase test и затем применяет target migration к основной базе.

## Smoke-проход

Минимальная проверка после изменений:

1. Поднять Postgres:

```powershell
docker compose up -d postgres
```

2. Прогнать миграции:

```powershell
docker compose up migrate
```

3. Запустить API:

```powershell
$env:DATABASE_URL = "postgresql://user:Passw0rd@localhost:5252/cars_db?sslmode=disable"
uvicorn src.main:app --host 0.0.0.0 --port 8000
```

4. Проверить health/version:

```powershell
Invoke-RestMethod http://localhost:8000/api/v1/health
Invoke-RestMethod http://localhost:8000/api/v1/version
```

5. Открыть Swagger:

```text
http://localhost:8000/docs
```

6. Проверить auth flow:

```powershell
$body = @{
  email = "test@example.com"
  password = "password123"
  full_name = "Test User"
  phone = "+79991234567"
} | ConvertTo-Json

Invoke-RestMethod `
  -Uri http://localhost:8000/api/v1/auth/register `
  -Method Post `
  -ContentType "application/json" `
  -Body $body
```

## Связь с frontend

Админ-панель в `labeler` должна смотреть на backend через:

```env
VITE_API_BASE_URL=http://localhost:8000/api/v1
```

Для production/staging это значение задается на этапе сборки frontend. Backend ожидает `Authorization: Bearer <token>` на защищенных ручках.

## Подключение к базе

Для DataGrip или другого клиента при дефолтном `example.env`:

```text
Host: localhost
Port: 5252
Database: cars_db
User: user
Password: Passw0rd
```

## Данные и git

В репозиторий не должны попадать:

```text
.env
.venv/
data/
__pycache__/
```

Миграции, схемы, роутеры и документацию коммитим обычным образом. Для README подойдет коммит:

```text
docs: expand api server readme
```
