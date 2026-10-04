from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..database import get_db
from ..db_models import Camera, LocationType, ParkingZone, Partner, User, ZoneType
from ..dependencies import get_effective_permissions, require, resolve_current_user
from ..schemas.zones import (
    CreateZoneRequest,
    UpdateZoneRequest,
    ZoneListResponse,
    ZoneMapItemResponse,
    ZoneResponse,
)

router = APIRouter(prefix="/zones", tags=["Parking Zones"])


def _serialize(z: ParkingZone, db: Session) -> ZoneResponse:
    # free_count — GENERATED ALWAYS AS в БД, читаем свежим SELECT
    free_count = db.execute(
        text("SELECT free_count FROM parking_zones WHERE parking_zone_id = :id"),
        {"id": z.parking_zone_id},
    ).scalar_one_or_none() or 0

    return ZoneResponse(
        zone_id=z.parking_zone_id,
        camera_id=z.camera_id,
        zone_type=z.zone_type.value,
        capacity=z.capacity,
        occupied=z.occupied,
        free_count=free_count if free_count >= 0 else 0,
        confidence=z.confidence or 0.0,
        confidence_level=z.confidence_level.value if z.confidence_level else None,
        pay=z.pay,
        geometry=z.geometry,
        image_polygon=z.image_polygon,
        partner_id=z.partner_id,
        created_by_user_id=z.created_by_user_id,
        is_active=z.is_active,
        location_type=z.location_type.value if z.location_type else None,
        is_private=z.is_private,
        is_accessible=z.is_accessible,
        occupancy_updated_at=z.occupancy_updated_at,
        created_at=z.created_at,
        updated_at=z.updated_at,
    )


def _serialize_map(z: ParkingZone, db: Session) -> ZoneMapItemResponse:
    free_count = db.execute(
        text("SELECT free_count FROM parking_zones WHERE parking_zone_id = :id"),
        {"id": z.parking_zone_id},
    ).scalar_one_or_none() or 0

    return ZoneMapItemResponse(
        zone_id=z.parking_zone_id,
        zone_type=z.zone_type.value,
        capacity=z.capacity,
        occupied=z.occupied,
        free_count=free_count if free_count >= 0 else 0,
        confidence=z.confidence or 0.0,
        confidence_level=z.confidence_level.value if z.confidence_level else None,
        pay=z.pay,
        geometry=z.geometry,
        location_type=z.location_type.value if z.location_type else None,
        is_private=z.is_private,
        is_accessible=z.is_accessible,
        occupancy_updated_at=z.occupancy_updated_at,
        is_active=z.is_active,
    )

def _get_zone_or_404(db: Session, zone_id: int) -> ParkingZone:
    zone = db.query(ParkingZone).filter(ParkingZone.parking_zone_id == zone_id).one_or_none()
    if zone is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            detail={"error_description": "Zone not found"})
    return zone


# ---------------------------------------------------------------------------
# GET /zones
# ---------------------------------------------------------------------------

@router.get("", response_model=list[ZoneResponse] | list[ZoneMapItemResponse])
def list_zones(
    db: Annotated[Session, Depends(get_db)],
    camera_id:      int   | None = None,
    partner_id:     int   | None = None,
    is_active:      bool  | None = None,
    min_free_count: int   | None = None,
    max_pay:        int   | None = None,
    bbox:           str   | None = None,
    view:           str          = "full",
    top:            int          = 100,
    offset:         int          = 0,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(HTTPBearer(auto_error=False))] = None,
):
    if view != "map":
        current_user = resolve_current_user(credentials=credentials, db=db)
        if "zones.view" not in get_effective_permissions(current_user):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                detail={"error_description": "Missing permissions: zones.view"},
            )

    query = db.query(ParkingZone)

    if camera_id is not None:
        query = query.filter(ParkingZone.camera_id == camera_id)
    if partner_id is not None:
        query = query.filter(ParkingZone.partner_id == partner_id)
    if is_active is not None:
        query = query.filter(ParkingZone.is_active == is_active)
    if max_pay is not None:
        query = query.filter(ParkingZone.pay <= max_pay)
    if min_free_count is not None:
        query = query.filter(
            (ParkingZone.capacity - ParkingZone.occupied) >= min_free_count
        )

    zones = query.order_by(ParkingZone.parking_zone_id).offset(offset).limit(top).all()

    if view == "map":
        return [_serialize_map(z, db) for z in zones]
    return [_serialize(z, db) for z in zones]


def _to_int(value: Any, default: int, min_value: int | None = None) -> int:
    try:
        if value is None or isinstance(value, bool):
            result = default
        else:
            result = int(float(value))
    except (TypeError, ValueError):
        result = default

    if min_value is not None:
        result = max(result, min_value)

    return result


def _to_bool(value: Any, default: bool | None = None) -> bool | None:
    if isinstance(value, bool):
        return value

    if value is None:
        return default

    if isinstance(value, int):
        return bool(value)

    if isinstance(value, str):
        normalized = value.strip().lower()

        if normalized in {"true", "1", "yes", "y", "да"}:
            return True

        if normalized in {"false", "0", "no", "n", "нет"}:
            return False

    return default


def _normalize_zone_type(value: Any) -> ZoneType:
    if isinstance(value, ZoneType):
        return value

    if isinstance(value, str):
        normalized = value.strip().lower()

        if normalized == "parallel":
            return ZoneType.parallel

        if normalized == "standard":
            return ZoneType.standard

    return ZoneType.standard


def _normalize_location_type(value: Any) -> LocationType | None:
    if isinstance(value, LocationType):
        return value

    if not isinstance(value, str):
        return None

    normalized = value.strip().lower()

    try:
        return LocationType(normalized)
    except ValueError:
        return None


def _json_or_default(value: Any, default: Any) -> Any:
    if value is None:
        return default

    return value


def _get_camera_for_zone(db: Session, raw_camera_id: Any) -> Camera | None:
    camera_id = _to_int(raw_camera_id, default=0, min_value=1)

    camera = db.query(Camera).filter(Camera.camera_id == camera_id).one_or_none()

    if camera is not None:
        return camera

    # Неубиваемый режим: если camera_id плохой или камеры нет,
    # привязываем зону к первой существующей камере.
    return db.query(Camera).order_by(Camera.camera_id.asc()).first()


def _get_partner_id_or_none(db: Session, raw_partner_id: Any) -> int | None:
    if raw_partner_id is None:
        return None

    partner_id = _to_int(raw_partner_id, default=0, min_value=1)

    partner = db.query(Partner).filter(Partner.partner_id == partner_id).one_or_none()

    if partner is None:
        return None

    return partner.partner_id


def _compute_geometry_centroid(geometry: Any) -> tuple[float | None, float | None]:
    """
    Возвращает (latitude, longitude) для GeoJSON Polygon.

    GeoJSON хранит точки как [longitude, latitude].
    Если полигон плохой или вырожденный, fallback — среднее по валидным точкам.
    """
    if not isinstance(geometry, dict):
        return None, None

    if geometry.get("type") != "Polygon":
        return None, None

    coordinates = geometry.get("coordinates")

    if not isinstance(coordinates, list) or not coordinates:
        return None, None

    outer_ring = coordinates[0]

    if not isinstance(outer_ring, list):
        return None, None

    points: list[tuple[float, float]] = []

    for raw_point in outer_ring:
        if not isinstance(raw_point, list | tuple) or len(raw_point) < 2:
            continue

        try:
            longitude = float(raw_point[0])
            latitude = float(raw_point[1])
        except (TypeError, ValueError):
            continue

        if -180 <= longitude <= 180 and -90 <= latitude <= 90:
            points.append((longitude, latitude))

    if not points:
        return None, None

    if len(points) > 1 and points[0] == points[-1]:
        points = points[:-1]

    if not points:
        return None, None

    # Если полигон нормальный, считаем геометрический центроид.
    # Для маленьких парковочных зон приближение по lon/lat достаточно.
    area2 = 0.0
    centroid_lon_sum = 0.0
    centroid_lat_sum = 0.0

    if len(points) >= 3:
        for index, current in enumerate(points):
            next_point = points[(index + 1) % len(points)]

            lon1, lat1 = current
            lon2, lat2 = next_point

            cross = lon1 * lat2 - lon2 * lat1

            area2 += cross
            centroid_lon_sum += (lon1 + lon2) * cross
            centroid_lat_sum += (lat1 + lat2) * cross

    if abs(area2) > 1e-12:
        centroid_longitude = centroid_lon_sum / (3.0 * area2)
        centroid_latitude = centroid_lat_sum / (3.0 * area2)

        if -180 <= centroid_longitude <= 180 and -90 <= centroid_latitude <= 90:
            return centroid_latitude, centroid_longitude

    # Fallback для вырожденных зон, например когда все точки одинаковые.
    centroid_longitude = sum(point[0] for point in points) / len(points)
    centroid_latitude = sum(point[1] for point in points) / len(points)

    return centroid_latitude, centroid_longitude

# ---------------------------------------------------------------------------
# POST /zones/new
# ---------------------------------------------------------------------------

@router.post("/new", status_code=status.HTTP_201_CREATED)
def create_zone(
    body: CreateZoneRequest,
    current_user: Annotated[User, require("zones.create")],
    db: Annotated[Session, Depends(get_db)],
):
    camera = _get_camera_for_zone(db, body.camera_id)

    if camera is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "error_description": "Cannot create parking zone: no cameras exist"
            },
        )

    capacity = _to_int(body.capacity, default=0, min_value=0)
    pay = _to_int(body.pay, default=0, min_value=0)

    geometry = _json_or_default(
        body.geometry,
        {
            "type": "Polygon",
            "coordinates": [[]],
        },
    )

    image_polygon = _json_or_default(
        body.image_polygon,
        [],
    )

    centroid_latitude, centroid_longitude = _compute_geometry_centroid(geometry)

    zone = ParkingZone(
        camera_id=camera.camera_id,
        zone_type=_normalize_zone_type(body.zone_type),
        capacity=capacity,
        occupied=0,
        confidence=0.0,
        confidence_level=None,
        pay=pay,
        geometry=geometry,
        image_polygon=image_polygon,
        centroid_latitude=centroid_latitude,
        centroid_longitude=centroid_longitude,
        partner_id=_get_partner_id_or_none(db, body.partner_id),
        created_by_user_id=current_user.user_id,
        is_active=_to_bool(body.is_active, default=True),
        location_type=_normalize_location_type(body.location_type),
        is_private=_to_bool(body.is_private, default=None),
        is_accessible=_to_bool(body.is_accessible, default=None),
    )

    db.add(zone)

    try:
        db.commit()
        db.refresh(zone)
    except SQLAlchemyError as exc:
        db.rollback()

        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error_description": "Parking zone payload could not be saved after normalization",
                "details": str(exc.__class__.__name__),
            },
        )

    return {"zone_id": zone.parking_zone_id}


# ---------------------------------------------------------------------------
# GET /zones/{zone_id}
# ---------------------------------------------------------------------------

@router.get("/{zone_id}", response_model=ZoneResponse)
def get_zone(
    zone_id: int,
    db: Annotated[Session, Depends(get_db)],
):
    # 2026-05-16: открыто без авторизации (по запросу) — как и GET /zones?view=map.
    return _serialize(_get_zone_or_404(db, zone_id), db)


# ---------------------------------------------------------------------------
# PUT /zones/{zone_id}
# ---------------------------------------------------------------------------

@router.put("/{zone_id}", response_model=ZoneResponse)
def update_zone(
    zone_id: int,
    body: UpdateZoneRequest,
    current_user: Annotated[User, require("zones.update")],
    db: Annotated[Session, Depends(get_db)],
):
    zone = _get_zone_or_404(db, zone_id)

    update_data = body.model_dump(exclude_none=True)

    occupied_changed = "occupied" in update_data

    for field, value in update_data.items():
        setattr(zone, field, value)

    if "geometry" in update_data:
        centroid_latitude, centroid_longitude = _compute_geometry_centroid(zone.geometry)
        zone.centroid_latitude = centroid_latitude
        zone.centroid_longitude = centroid_longitude

    now = datetime.now(timezone.utc)
    if occupied_changed:
        zone.occupancy_updated_at = now
    else:
        zone.updated_at = now

    db.commit()
    db.refresh(zone)
    return _serialize(zone, db)


# ---------------------------------------------------------------------------
# DELETE /zones/{zone_id}
# ---------------------------------------------------------------------------

@router.delete("/{zone_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_zone(
    zone_id: int,
    current_user: Annotated[User, require("zones.delete")],
    db: Annotated[Session, Depends(get_db)],
):
    zone = _get_zone_or_404(db, zone_id)
    db.delete(zone)
    db.commit()
    return None
