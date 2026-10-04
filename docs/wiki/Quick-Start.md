# Быстрый старт

## Windows 11

1. Установить Docker Desktop и включить WSL 2.
2. Скопировать `projects/parktrack/.env.example` в `projects/parktrack/.env`.
3. Заменить демонстрационные пароли и токены.
4. Запустить `projects/parktrack/start-parktrack.bat`.
5. Открыть админ-панель по адресу `http://127.0.0.1:5173`.
6. Проверить API по адресу `http://127.0.0.1:8000/docs`.

## Linux

```bash
cd projects/parktrack
cp .env.example .env
docker compose up --build -d
docker compose ps
```

Перед запуском измените значения `change_me` в `.env`.

