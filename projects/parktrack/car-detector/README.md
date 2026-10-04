# Car detection and zone occupancy update

Запуск:

```shell
python3 -m detection.main \
  --model path/to/openvino/model.xml \
  --base-api-url http://localhost:8080 \
  --api-token YOUR_TOKEN \
 
```

Перед запуском необходимо настроить S3 и ключ шифрования снапшотов. Для каждого
снапшота сохраняются исходный кадр, размеченный кадр и labels всех найденных
автомобилей в формате YOLOv12. Формат объектов, переменные окружения и процедура расшифровки описаны в
[SNAPSHOT_STORAGE.md](SNAPSHOT_STORAGE.md).
