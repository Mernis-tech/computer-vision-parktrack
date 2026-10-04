# Как загрузить проект в GitHub на Windows

## Вариант 1. Через ZIP и Git Bash

1. Создайте на GitHub новый пустой репозиторий.
2. Выберите видимость `Private`, пока не решён вопрос с лицензиями.
3. Не добавляйте на GitHub README, `.gitignore` и лицензию, потому что они уже находятся в проекте.
4. Скачайте и распакуйте `cv-parktrack-integration.zip`, например в `C:\Projects\cv-parktrack-integration`.
5. Откройте распакованную папку, нажмите правой кнопкой мыши и выберите `Open Git Bash here`.
6. Выполните команды:

```bash
git init
git add .
git commit -m "chore: create integration monorepo"
git branch -M main
git remote add origin https://github.com/ИМЯ_ПОЛЬЗОВАТЕЛЯ/ИМЯ_РЕПОЗИТОРИЯ.git
git push -u origin main
```

Вместо адреса в примере вставьте HTTPS-ссылку, которую покажет GitHub.

## Вариант 2. Через готовый Git bundle

Bundle уже содержит первый коммит и ветку `main`.

```bash
cd /c/Projects
git clone /c/Users/ИМЯ/Downloads/cv-parktrack-integration.bundle cv-parktrack-integration
cd cv-parktrack-integration
git remote add origin https://github.com/ИМЯ_ПОЛЬЗОВАТЕЛЯ/ИМЯ_РЕПОЗИТОРИЯ.git
git push -u origin main
```

## Создание ветки для общей разработки

После загрузки основной ветки:

```bash
git switch -c develop
git push -u origin develop
```

В настройках GitHub рекомендуется защитить `main`, чтобы изменения попадали туда только через Pull Request.

## Как добавить изменения позже

```bash
git status
git add .
git commit -m "docs: update integration plan"
git push
```

Перед `git add .` проверяйте вывод `git status`. В коммит не должны попадать `.env`, пароли, закрытые ссылки на камеры, снимки, `node_modules`, базы данных и журналы.

