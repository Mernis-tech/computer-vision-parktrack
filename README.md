# Computer Vision + ParkTrack

Единый учебный репозиторий команды проекта «Компьютерное зрение мультимодального сервиса».

Репозиторий объединяет:

- нашу обученную модель распознавания автомобилей;
- локальную платформу ParkTrack с API, базой данных, админ-панелью, зонами и историей;
- документы по интеграции, запуску и серверной конфигурации.

## Структура

```text
.
├── projects/
│   ├── our-model/       # исходная модель команды и текущие скрипты
│   └── parktrack/       # локальная сборка компонентов ParkTrack
├── integration/         # будущий адаптер нашей модели к API ParkTrack
└── docs/
    ├── INTEGRATION_PLAN.md
    ├── SERVER_REQUIREMENTS.md
    ├── REPOSITORY_WORKFLOW.md
    └── wiki/            # исходники страниц Wiki
```

## Что запускается сейчас

Готовая локальная сборка ParkTrack находится в `projects/parktrack`. На Windows её можно запустить через `start-parktrack.bat`. Отдельные инструкции находятся в `projects/parktrack/README-RU.md`.

Наша модель находится в `projects/our-model`. Пока она запускается отдельно и не использует базу, зоны и историю ParkTrack. Подключение модели описано в [плане интеграции](docs/INTEGRATION_PLAN.md).

## Документация

- [План интеграции](docs/INTEGRATION_PLAN.md)
- [Краткий план интеграции](docs/КРАТКИЙ_ПЛАН_ИНТЕГРАЦИИ.md)
- [Минимальная конфигурация сервера](docs/SERVER_REQUIREMENTS.md)
- [Работа с Git](docs/REPOSITORY_WORKFLOW.md)
- [Загрузка проекта в GitHub на Windows](docs/GITHUB_UPLOAD_WINDOWS.md)
- [Структура Wiki](docs/wiki/Home.md)

## Исходные проекты

- Наша модель: <https://github.com/VadimPopov4/Computer-vision-for-a-multimodal-service>
- ParkTrack: <https://github.com/ParkTrack-Project>

## Важно перед публикацией

Не добавляйте в Git реальные пароли, токены, адреса закрытых камер и содержимое `.env`. В составе ParkTrack есть код под GPL-3.0, а лицензии остальных компонентов нужно проверить до публичного распространения объединённого репозитория.
