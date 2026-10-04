# Хранение снапшотов камер в S3

Пайплайн сохраняет для каждого наблюдения три связанных объекта:

- `raw` — исходный кадр камеры без разметки;
- `annotated` — тот же кадр с зонами, рамками машин и легендой;
- `labels` — текстовая разметка всех итоговых детекций автомобилей в формате YOLOv12.

Незашифрованные JPEG- и TXT-файлы на локальный диск сервера не записываются. Оба кадра кодируются в JPEG, labels кодируются в UTF-8, после чего все три объекта шифруются в оперативной памяти процесса до вызова S3 `PutObject`. В бакете находятся только контейнеры `PTSNAP01` с типом `application/octet-stream`.

## Формат labels

`labels.txt` использует стандартный detection-формат YOLOv12. Каждая непустая строка описывает один найденный автомобиль:

```text
<class_id> <x_center> <y_center> <width> <height>
```

Координаты центра, ширина и высота рамки нормализованы в диапазон `[0, 1]` относительно ширины и высоты полного `raw`-кадра и записываются с шестью знаками после запятой. Confidence в labels не включается. Для используемой одноклассовой модели `car` имеет `class_id=0`. Если автомобили не найдены, после расшифровки получается пустой файл.

Labels строятся из тех же итоговых детекций после агрегации трёх кадров, рамки которых показаны на `annotated`.

## Конфигурация

Обязательные переменные окружения:

| Переменная | Назначение |
| --- | --- |
| `SNAPSHOT_S3_BUCKET` | Имя существующего S3-бакета |
| `SNAPSHOT_ENCRYPTION_KEY_BASE64` | Текущий 32-байтовый ключ AES в Base64 |
| `SNAPSHOT_ENCRYPTION_KEY_ID` | Стабильный идентификатор ключа, например `snapshot-key-2026-01` |

Необязательные переменные:

| Переменная | Значение по умолчанию / назначение |
| --- | --- |
| `SNAPSHOT_S3_PREFIX` | `camera-snapshots` |
| `SNAPSHOT_S3_ENDPOINT_URL` | URL S3-совместимого хранилища; для AWS S3 не задаётся |
| `SNAPSHOT_S3_REGION` | Регион S3 |

Учётные данные S3 передаются стандартным способом boto3: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` и, если нужен, `AWS_SESSION_TOKEN`; в AWS предпочтительна IAM role задачи/инстанса. Учётной записи достаточно разрешения `s3:PutObject` на настроенный префикс. Бакет и его lifecycle/retention создаются отдельно.

Новый ключ можно создать так:

```shell
openssl rand -base64 32
```

Ключ нельзя хранить в репозитории, Docker-образе, имени объекта или metadata S3. Его следует передавать через менеджер секретов. При ротации меняются одновременно `SNAPSHOT_ENCRYPTION_KEY_BASE64` и `SNAPSHOT_ENCRYPTION_KEY_ID`; старые ключи нужно сохранять, пока существуют зашифрованные ими объекты.

## Имена объектов

Каждая тройка получает общий случайный `snapshot_id`, поэтому несколько workers не перезаписывают результаты друг друга:

```text
<prefix>/camera-<camera_id>/YYYY/MM/DD/<UTC timestamp>_<snapshot_id>/raw.jpg.aesgcm
<prefix>/camera-<camera_id>/YYYY/MM/DD/<UTC timestamp>_<snapshot_id>/annotated.jpg.aesgcm
<prefix>/camera-<camera_id>/YYYY/MM/DD/<UTC timestamp>_<snapshot_id>/labels.txt.aesgcm
```

Пример:

```text
camera-snapshots/camera-17/2026/07/22/20260722T080910.123456Z_f4c2.../raw.jpg.aesgcm
camera-snapshots/camera-17/2026/07/22/20260722T080910.123456Z_f4c2.../annotated.jpg.aesgcm
camera-snapshots/camera-17/2026/07/22/20260722T080910.123456Z_f4c2.../labels.txt.aesgcm
```

Результат пайплайна содержит `snapshots.raw`, `snapshots.annotated` и `snapshots.labels` с бакетом, ключом объекта, временем кадра и `encryption_key_id`.

## Бинарный формат `PTSNAP01`

Все целые числа имеют порядок байтов big-endian.

| Поле | Размер | Описание |
| --- | ---: | --- |
| Magic | 8 байт | ASCII `PTSNAP01` |
| Header length | 4 байта | Длина JSON-заголовка беззнаковым `uint32` |
| Header | N байт | UTF-8 JSON в канонической форме |
| Nonce | 12 байт | Случайный уникальный nonce AES-GCM |
| Ciphertext + tag | переменный + 16 байт | Зашифрованный JPEG или TXT и authentication tag |

JSON-заголовок содержит `format_version`, `algorithm`, `key_id`, `camera_id`, `captured_at`, `variant` и исходный `content_type`. Для варианта `labels` дополнительно записывается `annotation_format=yolo-v12-detection`. Последовательность `Magic + Header length + Header` используется как AAD в AES-256-GCM: изменение заголовка, ciphertext или tag обнаруживается при расшифровке. Nonce не является секретом и хранится открыто; для каждого объекта создаётся новый nonce.

S3 metadata дублирует только несекретные данные для эксплуатации: формат, ID ключа, ID камеры, вариант и время кадра. Источником истины после проверки целостности остаётся аутентифицированный заголовок контейнера.

## Расшифровка

Сначала скачайте объект (доступ на чтение приложению детекции не требуется):

```shell
aws s3 cp \
  s3://parktrack-snapshots/camera-snapshots/camera-17/2026/07/22/<snapshot>/raw.jpg.aesgcm \
  /tmp/raw.jpg.aesgcm
```

Выберите ключ по `encryption-key-id` в metadata объекта или по `key_id` заголовка и передайте его через окружение:

```shell
export SNAPSHOT_ENCRYPTION_KEY_BASE64='<base64-key>'
python -m detection.decrypt_snapshot /tmp/raw.jpg.aesgcm /tmp/raw.jpg
```

Для labels используется та же команда:

```shell
aws s3 cp \
  s3://parktrack-snapshots/camera-snapshots/camera-17/2026/07/22/<snapshot>/labels.txt.aesgcm \
  /tmp/labels.txt.aesgcm
python -m detection.decrypt_snapshot /tmp/labels.txt.aesgcm /tmp/labels.txt
```

Команда выводит проверенный JSON-заголовок и создаёт исходный JPEG или TXT только после успешной проверки AES-GCM. Существующий выходной файл не перезаписывается; для осознанной перезаписи добавьте `--force`. Ошибка `InvalidTag` означает неверный ключ либо повреждение/подмену контейнера.

Оператор, расшифровывающий данные, отвечает за безопасное удаление созданного открытого JPEG или TXT. Для обычной работы сервиса расшифровка и `s3:GetObject` не нужны.
