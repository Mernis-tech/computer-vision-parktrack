import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request


API_URL = os.environ.get("API_URL", "http://api:8000/api/v1").rstrip("/")
API_TOKEN = os.environ["API_TOKEN"]
VIDEO_SOURCE = os.environ.get("DEMO_VIDEO_SOURCE", "/media/demo-parking.mp4")
ADMIN_EMAIL = os.environ.get("DEMO_ADMIN_EMAIL", "admin@parktrack-demo.com")
ADMIN_PASSWORD = os.environ.get("DEMO_ADMIN_PASSWORD", "ParkTrack123!")


def request(method: str, path: str, payload=None):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        API_URL + path,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {API_TOKEN}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as response:
        raw = response.read()
        return json.loads(raw) if raw else None


def wait_for_api():
    for _ in range(60):
        try:
            request("GET", "/health")
            return
        except Exception:
            time.sleep(2)
    raise RuntimeError("API не стал доступен за 120 секунд")


def ensure_admin():
    users_response = request("GET", "/users?top=100&offset=0")
    users = users_response.get("items", []) if isinstance(users_response, dict) else users_response
    if not any(user.get("email") == ADMIN_EMAIL for user in (users or [])):
        request(
            "POST",
            "/users",
            {
                "email": ADMIN_EMAIL,
                "password": ADMIN_PASSWORD,
                "full_name": "ParkTrack Local Admin",
                "global_role": "admin",
            },
        )
        print(f"Создан локальный администратор: {ADMIN_EMAIL}")
    else:
        print(f"Локальный администратор уже существует: {ADMIN_EMAIL}")


def ensure_camera() -> int:
    cameras = request("GET", "/cameras") or []
    demo = next((camera for camera in cameras if camera.get("title") == "Демо-парковка"), None)
    camera_payload = {
        "title": "Демо-парковка",
        "source": VIDEO_SOURCE,
        "image_width": 1920,
        "image_height": 1080,
        "calib": {
            "image_width": 1920,
            "image_height": 1080,
            "K": [1920.0, 0.0, 960.0, 0.0, 1920.0, 540.0, 0.0, 0.0, 1.0],
            "D": [0.0, 0.0, 0.0, 0.0],
            "newK": [1920.0, 0.0, 960.0, 0.0, 1920.0, 540.0, 0.0, 0.0, 1.0],
            "confidence": 0.15,
        },
        "latitude": 55.751244,
        "longitude": 37.618423,
    }

    if demo:
        camera_id = int(demo["camera_id"])
        request("PUT", f"/cameras/{camera_id}", camera_payload)
        print(f"Демо-камера обновлена: id={camera_id}")
        return camera_id

    result = request("POST", "/cameras/new", camera_payload)
    camera_id = int(result["camera_id"])
    print(f"Демо-камера создана: id={camera_id}")
    return camera_id


def ensure_zone(camera_id: int):
    zones = request("GET", f"/zones?camera_id={camera_id}") or []
    if zones:
        print(f"Зона для демо-камеры уже существует: id={zones[0]['zone_id']}")
        return

    result = request(
        "POST",
        "/zones/new",
        {
            "camera_id": camera_id,
            "zone_type": "standard",
            "capacity": 16,
            "pay": 0,
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[37.6178, 55.7510], [37.6190, 55.7510], [37.6190, 55.7515], [37.6178, 55.7515], [37.6178, 55.7510]]],
            },
            "image_polygon": [[0, 0], [1919, 0], [1919, 260], [0, 260]],
            "is_active": True,
            "location_type": "street",
            "is_private": False,
            "is_accessible": True,
        },
    )
    print(f"Демо-зона создана: id={result['zone_id']}")


def main():
    wait_for_api()
    ensure_admin()
    camera_id = ensure_camera()
    ensure_zone(camera_id)
    print("Начальные данные ParkTrack успешно подготовлены.")


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as error:
        details = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"API вернул {error.code}: {details}") from error
