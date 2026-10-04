from ultralytics import YOLO
import cv2
import time
from datetime import datetime
from pathlib import Path
import requests

API_URL = "https://backend-c6pd.onrender.com"

# ================= API =================
def save_to_api(parking_id, count):
    url = f"{API_URL}/cv/occupancy/{parking_id}"
    payload = {"occupancy": count}

    try:
        response = requests.patch(url, json=payload, timeout=10)

        if response.status_code == 200:
            print(f"✅ API обновлено: {parking_id} -> {count}")
        else:
            print(f"⚠️ API ошибка {response.status_code}: {response.text}")

    except Exception as e:
        print(f"⚠️ Ошибка сети: {e}")


# ================= Камеры =================
def get_cameras():
    url = f"{API_URL}/cv/data"

    try:
        r = requests.get(url, timeout=10)

        if r.status_code != 200:
            return []

        data = r.json()
        data = [item for sublist in data for item in sublist]

        for cam in data:
            cam["last_time"] = 0
            cam["name"] = str(cam["name"])

            # ROI защита
            if isinstance(cam["roi"], str):
                cam["roi"] = list(map(int, cam["roi"].split(",")))

        return data

    except Exception as e:
        print(f"⚠️ Ошибка камер: {e}")
        return []


# ================= Основная логика =================
def multi_camera_segmentation():
    model_path = "/root/cv_project/best.pt"
    base_output_dir = "/root/cv_project/output"

    model = YOLO(model_path)
    print("🚀 Модель загружена")

    cameras = get_cameras()

    if not cameras:
        print("❌ Камеры не получены")
        return

    # папки
    for cam in cameras:
        Path(base_output_dir, cam["name"], "cropped").mkdir(parents=True, exist_ok=True)
        Path(base_output_dir, cam["name"], "segmented").mkdir(parents=True, exist_ok=True)

    # подключение камер
    for cam in cameras:
        cam["cap"] = cv2.VideoCapture(cam["url"], cv2.CAP_FFMPEG)
        cam["cap"].set(cv2.CAP_PROP_BUFFERSIZE, 2)

        if not cam["cap"].isOpened():
            print(f"⚠️ Камера не открылась: {cam['name']}")
        else:
            print(f"✓ Камера OK: {cam['name']}")

    print("🔥 Запуск системы...")

    try:
        while True:
            for cam in cameras:

                if "cap" not in cam:
                    continue

                cap = cam["cap"]
                ret, frame = cap.read()

                # 🔴 защита от пустых кадров
                if not ret or frame is None:
                    print(f"⚠️ reconnect {cam['name']}")
                    cap.release()
                    time.sleep(5)
                    cam["cap"] = cv2.VideoCapture(cam["url"], cv2.CAP_FFMPEG)
                    cam["cap"].set(cv2.CAP_PROP_BUFFERSIZE, 2)

                    if not cam["cap"].isOpened():
                        print(f"⚠️ повторное подключение не удалось: {cam['name']}")
                        time.sleep(5)
                    continue

                now = time.time()

                # интервал обработки (20 минут = 1200 сек)
                if now - cam["last_time"] >= 300:

                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

                    print(f"\n📷 Камера: {cam['name']}")

                    # ROI защита
                    x1, y1, x2, y2 = cam["roi"]
                    h, w = frame.shape[:2]

                    x1 = max(0, min(x1, w))
                    x2 = max(0, min(x2, w))
                    y1 = max(0, min(y1, h))
                    y2 = max(0, min(y2, h))

                    crop = frame[y1:y2, x1:x2]

                    # save crop
                    cv2.imwrite(
                        f"{base_output_dir}/{cam['name']}/cropped/{timestamp}.jpg",
                        crop
                    )

                    # YOLO
                    results = model.predict(
                        source=crop,
                        imgsz=640,
                        conf=0.4,
                        verbose=False
                    )

                    car_count = 0

                    if len(results) > 0:
                        annotated = results[0].plot()

                        cv2.imwrite(
                            f"{base_output_dir}/{cam['name']}/segmented/{timestamp}.jpg",
                            annotated
                        )

                        boxes = results[0].boxes
                        car_count = len(boxes) if boxes is not None else 0

                    print(f"🚗 Авто: {car_count}")

                    save_to_api(cam["name"], car_count)

                    cam["last_time"] = now

            time.sleep(1)  # 🔥 снижает нагрузку сервера

    except KeyboardInterrupt:
        print("⛔ Остановлено")

    finally:
        for cam in cameras:
            if "cap" in cam:
                cam["cap"].release()

        cv2.destroyAllWindows()


# ================= Запуск =================
if __name__ == "__main__":
    multi_camera_segmentation()