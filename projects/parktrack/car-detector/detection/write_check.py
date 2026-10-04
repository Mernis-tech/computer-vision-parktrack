"""
Write-конформанс-чек:
    1) GET    /zones?camera_id=<id>           -> запоминаем (occupied, confidence) зоны.
    2) POST   /occupancy/new                  -> создаём наблюдение, ловим observation_id.
    3) PUT    /zones/{zone_id}                -> ставим тестовые значения.
    4) PUT    /zones/{zone_id}                -> откатываем на оригинальные.
    5) DELETE /occupancy/{observation_id}     -> удаляем созданное наблюдение.

После корректного прохождения зона возвращается в исходное состояние,
а тестовое наблюдение полностью удаляется.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

import requests


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-api-url", default="https://api.parktrack.live/api/v1")
    parser.add_argument("--api-token", default=os.environ.get("PARKTRACK_TOKEN", ""))
    parser.add_argument("--camera-id", type=int, default=1)
    parser.add_argument("--zone-id", type=int, default=1)
    # По умолчанию подберём тестовое значение так, чтобы occupied <= capacity
    # (значение -1 = взять min(capacity, 5)).
    parser.add_argument("--test-occupied", type=int, default=-1)
    parser.add_argument("--test-confidence", type=float, default=0.4242)
    return parser.parse_args()


def session_with_token(token: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {token}"})
    return s


def show(label, response):
    print(f"--- {label} -> {response.status_code} {response.reason}")
    text = response.text
    if len(text) > 500:
        text = text[:500] + "...<truncated>"
    print(text)


def main():
    args = parse_args()
    if not args.api_token:
        print("[ERR] need --api-token or env PARKTRACK_TOKEN", file=sys.stderr)
        sys.exit(2)

    base = args.base_api_url.rstrip("/")
    s = session_with_token(args.api_token)

    print(f"base = {base}")
    print(f"camera_id = {args.camera_id}, zone_id = {args.zone_id}")

    # ---- 1. Read original zone state ----
    print("\n[1/5] GET /zones?camera_id=...")
    r = s.get(f"{base}/zones", params={"camera_id": args.camera_id}, timeout=10)
    show("GET /zones", r)
    r.raise_for_status()
    zones = r.json()

    target_zone = next((z for z in zones if int(z["zone_id"]) == args.zone_id), None)
    if target_zone is None:
        print(f"[ERR] zone {args.zone_id} not found", file=sys.stderr)
        sys.exit(1)

    original_occupied = int(target_zone["occupied"])
    original_confidence = float(target_zone["confidence"])
    capacity = int(target_zone["capacity"])
    print(
        f"original zone state: occupied={original_occupied}, "
        f"confidence={original_confidence}, capacity={capacity}"
    )

    test_occupied = getattr(args, "test_occupied")
    if test_occupied < 0:
        test_occupied = min(capacity, 5)
    elif test_occupied > capacity:
        print(
            f"[WARN] test_occupied={test_occupied} > capacity={capacity}, "
            f"уменьшаю до {capacity}",
            file=sys.stderr,
        )
        test_occupied = capacity
    print(f"test values: occupied={test_occupied}, confidence={args.test_confidence}")

    observation_id = None
    zone_was_modified = False

    try:
        # ---- 2. POST /occupancy/new ----
        print("\n[2/5] POST /occupancy/new")
        observed_at = datetime.now(timezone.utc).isoformat()
        source_ref = f"api-conformance-test-{int(datetime.now().timestamp())}"
        post_payload = {
            "zone_id": args.zone_id,
            "source_type": "camera_cv",
            "observed_at": observed_at,
            "occupied": test_occupied,
            "confidence": args.test_confidence,
            "source_ref": source_ref,
        }
        print("payload:", json.dumps(post_payload, ensure_ascii=False))
        r = s.post(f"{base}/occupancy/new", json=post_payload, timeout=10)
        show("POST /occupancy/new", r)
        r.raise_for_status()
        observation_id = int(r.json()["observation_id"])
        print(f"observation_id = {observation_id}")

        # ---- 3. PUT /zones/{id} with test values ----
        print(f"\n[3/5] PUT /zones/{args.zone_id}  (test values)")
        put_test = {
            "occupied": test_occupied,
            "confidence": args.test_confidence,
        }
        print("payload:", json.dumps(put_test))
        r = s.put(f"{base}/zones/{args.zone_id}", json=put_test, timeout=10)
        show(f"PUT /zones/{args.zone_id}", r)
        r.raise_for_status()
        zone_was_modified = True

        # ---- 4. PUT /zones/{id} restore ----
        print(f"\n[4/5] PUT /zones/{args.zone_id}  (restore original)")
        put_restore = {
            "occupied": original_occupied,
            "confidence": original_confidence,
        }
        print("payload:", json.dumps(put_restore))
        r = s.put(f"{base}/zones/{args.zone_id}", json=put_restore, timeout=10)
        show(f"PUT /zones/{args.zone_id} restore", r)
        r.raise_for_status()
        zone_was_modified = False

        # ---- 5. DELETE /occupancy/{observation_id} ----
        print(f"\n[5/5] DELETE /occupancy/{observation_id}")
        r = s.delete(f"{base}/occupancy/{observation_id}", timeout=10)
        show(f"DELETE /occupancy/{observation_id}", r)
        r.raise_for_status()
        observation_id = None

        print("\nALL STEPS OK — состояние восстановлено, наблюдение удалено.")

    except Exception as e:
        print(f"\n[ERR] step failed: {e}", file=sys.stderr)
        print("Запускаю аварийную очистку…", file=sys.stderr)

        if zone_was_modified:
            try:
                r = s.put(
                    f"{base}/zones/{args.zone_id}",
                    json={
                        "occupied": original_occupied,
                        "confidence": original_confidence,
                    },
                    timeout=10,
                )
                show(f"cleanup PUT /zones/{args.zone_id}", r)
            except Exception as ee:
                print(f"[ERR] cleanup PUT failed: {ee}", file=sys.stderr)

        if observation_id is not None:
            try:
                r = s.delete(f"{base}/occupancy/{observation_id}", timeout=10)
                show(f"cleanup DELETE /occupancy/{observation_id}", r)
            except Exception as ee:
                print(f"[ERR] cleanup DELETE failed: {ee}", file=sys.stderr)

        sys.exit(1)


if __name__ == "__main__":
    main()
