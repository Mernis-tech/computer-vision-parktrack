"""
Read-only smoke-check для соответствия скрипта боевому API ParkTrack.

Делает ТОЛЬКО GET-запросы:
    GET {BASE}/cameras/next
    GET {BASE}/zones?camera_id=<id>

Никаких POST/PUT — никаких записей в прод.

Пример запуска (PowerShell):
    $env:PARKTRACK_TOKEN = "<bearer-token>"
    python detection/dry_check.py --base-api-url https://api.parktrack.live/api/v1

Или явно:
    python detection/dry_check.py `
        --base-api-url https://api.parktrack.live/api/v1 `
        --api-token <bearer-token>
"""

import argparse
import json
import os
import sys

import requests

from api_client import fetch_next_camera, fetch_zones_for_camera


def parse_args():
    parser = argparse.ArgumentParser(description="ParkTrack API read-only smoke")
    parser.add_argument(
        "--base-api-url",
        default="https://api.parktrack.live/api/v1",
        help="Базовый URL API (должен включать /api/v1).",
    )
    parser.add_argument(
        "--api-token",
        default=os.environ.get("PARKTRACK_TOKEN", ""),
        help="Bearer токен (или задайте через env PARKTRACK_TOKEN).",
    )
    return parser.parse_args()


def make_session(api_token: str) -> requests.Session:
    session = requests.Session()
    if api_token:
        session.headers.update({"Authorization": f"Bearer {api_token}"})
    return session


def check_camera_next(session, base_url):
    print("\n=== GET /cameras/next ===")
    camera = fetch_next_camera(session, base_url)
    print(json.dumps(camera, ensure_ascii=False, indent=2, default=str))

    problems = []
    for required in ("camera_id", "source", "image_width", "image_height", "calib"):
        if required not in camera:
            problems.append(f"missing required field: {required}")

    calib = camera.get("calib") or {}
    if not isinstance(calib, dict):
        problems.append(f"calib has unexpected type: {type(calib).__name__}")
    else:
        crop_keys = ("crop_x", "crop_y", "crop_width", "crop_height")
        crop_present = [k for k in crop_keys if k in calib]
        print(f"calib keys: {sorted(calib.keys())}")
        if crop_present and len(crop_present) != 4:
            problems.append(
                f"calib contains partial crop_* keys: {crop_present} "
                "(пайплайн ожидает либо все 4, либо ни одного)"
            )

    if problems:
        print("[WARN] camera issues:")
        for p in problems:
            print("  -", p)

    return int(camera["camera_id"])


def check_zones(session, base_url, camera_id):
    print(f"\n=== GET /zones?camera_id={camera_id} ===")
    zones = fetch_zones_for_camera(session, base_url, camera_id)
    print(f"zones count: {len(zones)}")

    if not zones:
        print("[WARN] нет зон у этой камеры — пайплайн ничего не нарисует и не отправит.")
        return

    issues_total = 0
    for index, zone in enumerate(zones):
        zone_id = zone.get("zone_id")
        image_polygon = zone.get("image_polygon")
        legacy_points = zone.get("points")

        print(f"\n  zone[{index}] zone_id={zone_id}")
        print(f"    keys: {sorted(zone.keys())}")

        if image_polygon is None:
            print("    [FAIL] нет поля image_polygon")
            issues_total += 1
        else:
            print(f"    image_polygon ({len(image_polygon)} pts): {image_polygon}")
            if not isinstance(image_polygon, list):
                print(f"    [FAIL] image_polygon не список: {type(image_polygon).__name__}")
                issues_total += 1
            else:
                for pt_index, pt in enumerate(image_polygon):
                    if (
                        not isinstance(pt, (list, tuple))
                        or len(pt) != 2
                        or not all(isinstance(c, (int, float)) for c in pt)
                    ):
                        print(f"    [FAIL] point[{pt_index}] не [x, y]: {pt!r}")
                        issues_total += 1
                        break

        if legacy_points is not None:
            print(f"    [INFO] также есть legacy-поле 'points': {legacy_points!r}")

    print(f"\nzones with issues: {issues_total}/{len(zones)}")


def main():
    args = parse_args()

    if not args.api_token:
        print(
            "[ERR] нужен токен. Задайте через env PARKTRACK_TOKEN или --api-token.",
            file=sys.stderr,
        )
        sys.exit(2)

    print(f"base_api_url = {args.base_api_url}")
    session = make_session(args.api_token)

    try:
        camera_id = check_camera_next(session, args.base_api_url)
    except requests.HTTPError as e:
        print(f"[ERR] /cameras/next failed: {e.response.status_code} {e.response.text}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"[ERR] /cameras/next failed: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        check_zones(session, args.base_api_url, camera_id)
    except requests.HTTPError as e:
        print(f"[ERR] /zones failed: {e.response.status_code} {e.response.text}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"[ERR] /zones failed: {e}", file=sys.stderr)
        sys.exit(1)

    print("\n=== DONE — никаких записей в API не делалось ===")


if __name__ == "__main__":
    main()
