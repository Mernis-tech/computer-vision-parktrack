from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
from typing import Annotated

import cv2
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from PIL import Image
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..database import get_db
from ..db_models import Camera, ParkingZone, Partner, User, DataSource
from ..dependencies import require
from ..routers.partners import _serialize_partner, list_partners
from ..routers.users import _serialize as _serialize_user, list_users
from ..routers.zones import _serialize as _serialize_zone
from ..schemas.partners import PartnerListResponse
from ..schemas.users import UserListResponse
from ..schemas.zones import ZoneListResponse

router = APIRouter(prefix="/admin", tags=["Admin"])


# ---------------------------------------------------------------------------
# Inline response schemas (только для Admin-специфичных моделей)
# ---------------------------------------------------------------------------

class AdminOverview(BaseModel):
    users_total:    int
    users_active:   int
    partners_total: int
    partners_active: int
    cameras_total:  int
    cameras_active: int
    zones_total:    int
    zones_active:   int
    sources_total:  int
    sources_active: int
    routes_active:  int
    updated_at:     str


class AdminCameraMonitorItem(BaseModel):
    camera_id:         int
    partner_id:        int | None
    title:             str
    camera_status:     str
    processing_status: str
    last_snapshot_at:  str | None
    last_processed_at: str | None
    last_detection_at: str | None
    last_error:        str | None
    source:            str
    latitude:          float
    longitude:         float
    is_active:         bool


class AdminCameraListResponse(BaseModel):
    items:  list[AdminCameraMonitorItem]
    total:  int
    top:    int
    offset: int


class AdminSnapshotInfo(BaseModel):
    camera_id:         int
    snapshot_at:       str | None
    annotated:         bool
    content_type:      str
    width:             int | None
    height:            int | None
    detections_count:  int | None
    processing_status: str
    model_version:     str | None
    metadata:          dict | None


def _to_monitor_item(c: Camera) -> AdminCameraMonitorItem:
    return AdminCameraMonitorItem(
        camera_id=c.camera_id,
        partner_id=c.partner_id,
        title=c.title,
        camera_status="unknown",     # статус обновляется внешним CV-сервисом
        processing_status="unknown",
        last_snapshot_at=c.last_snapshot_at.isoformat() if c.last_snapshot_at else None,
        last_processed_at=None,
        last_detection_at=None,
        last_error=None,
        source=c.source,
        latitude=c.latitude,
        longitude=c.longitude,
        is_active=c.is_active,
    )


# ---------------------------------------------------------------------------
# GET /admin/overview
# ---------------------------------------------------------------------------

@router.get("/overview", response_model=AdminOverview)
def admin_overview(
    current_user: Annotated[User, require("admin.system.view")],
    db: Annotated[Session, Depends(get_db)],
):
    def count(model, condition=None):
        q = db.query(func.count(model.__mapper__.primary_key[0]))
        if condition is not None:
            q = q.filter(condition)
        return q.scalar() or 0

    return AdminOverview(
        users_total=count(User),
        users_active=count(User, User.is_active.is_(True)),
        partners_total=count(Partner),
        partners_active=count(Partner, Partner.is_active.is_(True)),
        cameras_total=count(Camera),
        cameras_active=count(Camera, Camera.is_active.is_(True)),
        zones_total=count(ParkingZone),
        zones_active=count(ParkingZone, ParkingZone.is_active.is_(True)),
        sources_total=count(DataSource),
        sources_active=count(DataSource, DataSource.is_active.is_(True)),
        routes_active=0,    # routing не реализован в этом спринте
        updated_at=datetime.now(timezone.utc).isoformat(),
    )


# ---------------------------------------------------------------------------
# GET /admin/cameras
# ---------------------------------------------------------------------------

@router.get("/cameras", response_model=AdminCameraListResponse)
def admin_list_cameras(
    current_user: Annotated[User, require("admin.monitoring.view")],
    db: Annotated[Session, Depends(get_db)],
    partner_id:        int  | None = None,
    camera_status:     str  | None = None,
    processing_status: str  | None = None,
    is_active:         bool | None = None,
    q:                 str  | None = None,
    top:               int  = 20,
    offset:            int  = 0,
):
    query = db.query(Camera)
    if partner_id is not None:
        query = query.filter(Camera.partner_id == partner_id)
    if is_active is not None:
        query = query.filter(Camera.is_active == is_active)
    if q:
        query = query.filter(Camera.title.icontains(q))
    # camera_status / processing_status хранятся во внешнем сервисе,
    # в MVP фильтруем только по полям БД

    total = query.count()
    cameras = query.order_by(Camera.camera_id).offset(offset).limit(top).all()

    return AdminCameraListResponse(
        items=[_to_monitor_item(c) for c in cameras],
        total=total,
        top=top,
        offset=offset,
    )


# ---------------------------------------------------------------------------
# GET /admin/cameras/{camera_id}
# ---------------------------------------------------------------------------

@router.get("/cameras/{camera_id}", response_model=AdminCameraMonitorItem)
def admin_get_camera(
    camera_id: int,
    current_user: Annotated[User, require("admin.monitoring.view")],
    db: Annotated[Session, Depends(get_db)],
):
    camera = db.query(Camera).filter(Camera.camera_id == camera_id).one_or_none()
    if camera is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            detail={"error_description": "Camera not found"})
    return _to_monitor_item(camera)


# ---------------------------------------------------------------------------
# GET /admin/cameras/{camera_id}/snapshot
# ---------------------------------------------------------------------------

@router.get("/cameras/{camera_id}/snapshot")
def admin_get_snapshot(
    camera_id: int,
    current_user: Annotated[User, require("admin.monitoring.view")],
    db: Annotated[Session, Depends(get_db)],
    annotated:       bool = False,
    fallback_to_raw: bool = False,
):
    camera = db.query(Camera).filter(Camera.camera_id == camera_id).one_or_none()
    if camera is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            detail={"error_description": "Camera not found"})

    cap = cv2.VideoCapture(camera.source)
    try:
        if not cap.isOpened():
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                                detail={"error_description": "Failed to open video stream"})
        ret, frame = cap.read()
        if not ret:
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                detail={"error_description": "Snapshot not available"})
        _, buf = cv2.imencode(".jpg", frame)
        img = Image.open(BytesIO(buf.tobytes()))
        out = BytesIO()
        img.save(out, format="JPEG")
        out.seek(0)
        return StreamingResponse(out, media_type="image/jpeg")
    finally:
        cap.release()


# ---------------------------------------------------------------------------
# GET /admin/cameras/{camera_id}/snapshot/info
# ---------------------------------------------------------------------------

@router.get("/cameras/{camera_id}/snapshot/info", response_model=AdminSnapshotInfo)
def admin_snapshot_info(
    camera_id: int,
    current_user: Annotated[User, require("admin.monitoring.view")],
    db: Annotated[Session, Depends(get_db)],
):
    camera = db.query(Camera).filter(Camera.camera_id == camera_id).one_or_none()
    if camera is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            detail={"error_description": "Camera not found"})

    # Метаданные снапшота хранятся во внешнем CV-сервисе.
    # В MVP возвращаем то, что есть в БД.
    return AdminSnapshotInfo(
        camera_id=camera.camera_id,
        snapshot_at=camera.last_snapshot_at.isoformat() if camera.last_snapshot_at else None,
        annotated=False,
        content_type="image/jpeg",
        width=camera.image_width,
        height=camera.image_height,
        detections_count=None,
        processing_status="unknown",
        model_version=None,
        metadata=None,
    )


# ---------------------------------------------------------------------------
# POST /admin/cameras/{camera_id}/processing/restart
# ---------------------------------------------------------------------------

@router.post("/cameras/{camera_id}/processing/restart", status_code=status.HTTP_202_ACCEPTED)
def admin_restart_processing(
    camera_id: int,
    current_user: Annotated[User, require("admin.system.manage")],
    db: Annotated[Session, Depends(get_db)],
):
    camera = db.query(Camera).filter(Camera.camera_id == camera_id).one_or_none()
    if camera is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            detail={"error_description": "Camera not found"})
    # Сигнал CV-сервису — реализуется очередью/событием, здесь stub
    return {"status": "accepted", "camera_id": camera_id}


# ---------------------------------------------------------------------------
# GET /admin/zones
# ---------------------------------------------------------------------------

@router.get("/zones", response_model=ZoneListResponse)
def admin_list_zones(
    current_user: Annotated[User, require("admin.system.view")],
    db: Annotated[Session, Depends(get_db)],
    partner_id:   int  | None = None,
    camera_id:    int  | None = None,
    is_active:    bool | None = None,
    updated_from: str  | None = None,
    updated_to:   str  | None = None,
    top:          int  = 20,
    offset:       int  = 0,
):
    query = db.query(ParkingZone)
    if partner_id is not None:
        query = query.filter(ParkingZone.partner_id == partner_id)
    if camera_id is not None:
        query = query.filter(ParkingZone.camera_id == camera_id)
    if is_active is not None:
        query = query.filter(ParkingZone.is_active == is_active)
    if updated_from:
        query = query.filter(ParkingZone.updated_at >= updated_from)
    if updated_to:
        query = query.filter(ParkingZone.updated_at <= updated_to)

    total = query.count()
    zones = query.order_by(ParkingZone.parking_zone_id).offset(offset).limit(top).all()

    return ZoneListResponse(
        items=[_serialize_zone(z, db) for z in zones],
        total=total,
        top=top,
        offset=offset,
    )


# ---------------------------------------------------------------------------
# POST /admin/zones/{zone_id}/recount
# ---------------------------------------------------------------------------

@router.post("/zones/{zone_id}/recount", status_code=status.HTTP_202_ACCEPTED)
def admin_recount_zone(
    zone_id: int,
    current_user: Annotated[User, require("admin.system.manage")],
    db: Annotated[Session, Depends(get_db)],
):
    zone = db.query(ParkingZone).filter(ParkingZone.parking_zone_id == zone_id).one_or_none()
    if zone is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            detail={"error_description": "Zone not found"})
    # Пересчёт инициируется внешним CV-сервисом — здесь stub
    return {"status": "accepted", "zone_id": zone_id}


# ---------------------------------------------------------------------------
# GET /admin/users  — проксируем в /users с admin.users.view
# ---------------------------------------------------------------------------

@router.get("/users", response_model=UserListResponse)
def admin_list_users(
    current_user: Annotated[User, require("admin.users.view")],
    db: Annotated[Session, Depends(get_db)],
    q:         str  | None = None,
    is_active: bool | None = None,
    top:       int  = 20,
    offset:    int  = 0,
):
    return list_users(current_user=current_user, db=db, q=q, is_active=is_active, top=top, offset=offset)


# ---------------------------------------------------------------------------
# GET /admin/partners  — проксируем в /partners с admin.partners.view
# ---------------------------------------------------------------------------

@router.get("/partners", response_model=PartnerListResponse)
def admin_list_partners(
    current_user: Annotated[User, require("admin.partners.view")],
    db: Annotated[Session, Depends(get_db)],
    q:         str  | None = None,
    is_active: bool | None = None,
    top:       int  = 20,
    offset:    int  = 0,
):
    return list_partners(current_user=current_user, db=db, q=q, is_active=is_active, top=top, offset=offset)
