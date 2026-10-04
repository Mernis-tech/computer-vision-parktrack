import sys
from typing import Any, Dict, Optional

import requests


def fetch_next_camera(
    http_session: requests.Session,
    base_api_url: str,
    timeout_seconds: float = 5.0
):
    """
    Получает данные о следующей камере через API:
        GET {BASE_API_URL}/cameras/next
    """
    request_url = base_api_url.rstrip("/") + "/cameras/next"
    response = http_session.get(request_url, timeout=timeout_seconds)
    response.raise_for_status()
    camera_data = response.json()
    return camera_data


def fetch_zones_for_camera(
    http_session: requests.Session,
    base_api_url: str,
    camera_id: int,
    timeout_seconds: float = 5.0
):
    """
    Получает список зон для заданной камеры через API:
        GET {BASE_API_URL}/zones?camera_id=<camera_id>
    """
    request_url = base_api_url.rstrip("/") + "/zones"
    response = http_session.get(
        request_url,
        params={"camera_id": camera_id},
        timeout=timeout_seconds
    )
    response.raise_for_status()
    zones_data = response.json()

    if not isinstance(zones_data, list):
        raise RuntimeError("Ожидался список зон от /zones?camera_id=...")

    return zones_data


def update_zone_occupancy(
    http_session: requests.Session,
    base_api_url: str,
    zone_id: int,
    occupied_count: int,
    zone_confidence: float,
    timeout_seconds: float = 5.0
) -> Dict[str, Any]:
    """
    Обновляет текущую занятость конкретной зоны через API:
        PUT {BASE_API_URL}/zones/<zone_id>

    Возвращает обновлённый объект Zone из ответа сервера.
    Если сервер вернул ошибку, пробрасывает исключение requests.HTTPError,
    чтобы вызывающий код мог не создавать неконсистентную запись истории.
    """
    request_url = base_api_url.rstrip("/") + f"/zones/{zone_id}"
    request_payload = {
        "occupied": int(occupied_count),
        "confidence": float(zone_confidence),
    }

    response = http_session.put(
        request_url,
        json=request_payload,
        timeout=timeout_seconds
    )

    if not (200 <= response.status_code < 300):
        print(
            f"[WARN] zone {zone_id} update failed: "
            f"{response.status_code} {response.text}",
            file=sys.stderr
        )
        response.raise_for_status()

    return response.json()


def create_occupancy_observation(
    http_session: requests.Session,
    base_api_url: str,
    zone_id: int,
    occupied_count: int,
    zone_confidence: float,
    observed_at_iso: str,
    source_type: str = "camera_cv",
    source_ref: Optional[str] = None,
    capacity: Optional[int] = None,
    metadata: Optional[Dict[str, Any]] = None,
    timeout_seconds: float = 5.0,
) -> Dict[str, Any]:
    """
    Создаёт запись наблюдения занятости через API:
        POST {BASE_API_URL}/occupancy/new

    Тело запроса соответствует OccupancyCreateRequest:
        required: zone_id, source_type, observed_at, occupied, confidence
        optional: source_ref, capacity, metadata
    """
    request_url = base_api_url.rstrip("/") + "/occupancy/new"
    request_payload = {
        "zone_id": int(zone_id),
        "source_type": source_type,
        "observed_at": observed_at_iso,
        "occupied": int(occupied_count),
        "confidence": float(zone_confidence),
    }
    if source_ref is not None:
        request_payload["source_ref"] = source_ref
    if capacity is not None:
        request_payload["capacity"] = int(capacity)
    if metadata is not None:
        request_payload["metadata"] = metadata

    print("Payload:", request_payload)

    response = http_session.post(
        request_url,
        json=request_payload,
        timeout=timeout_seconds
    )

    if not (200 <= response.status_code < 300):
        print(
            f"[WARN] occupancy create failed for zone {zone_id}: "
            f"{response.status_code} {response.text}",
            file=sys.stderr
        )
        response.raise_for_status()

    return response.json()
