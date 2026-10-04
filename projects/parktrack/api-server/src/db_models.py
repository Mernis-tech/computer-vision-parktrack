"""
SQLAlchemy ORM-модели.
Источник истины — SQL-миграции. Этот файл приведён в соответствие с ними.
"""

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, Column, DateTime, Double, Enum,
    ForeignKey, Integer, String, Text, UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import DeclarativeBase, relationship


# ---------------------------------------------------------------------------
# Enum-типы (должны совпадать с PostgreSQL-типами из миграций)
# ---------------------------------------------------------------------------

class GlobalRole(str, enum.Enum):
    user  = "user"
    admin = "admin"


class ZoneType(str, enum.Enum):
    parallel = "parallel"
    standard = "standard"


class ConfidenceLevel(str, enum.Enum):
    very_low = "very_low"
    low      = "low"
    medium   = "medium"
    high     = "high"


class LocationType(str, enum.Enum):
    street      = "street"
    yard        = "yard"
    open_lot    = "open_lot"
    underground = "underground"
    multilevel  = "multilevel"


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------

class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Вспомогательная функция
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

class User(Base):
    __tablename__ = "users"

    user_id         = Column(Integer, primary_key=True, autoincrement=True)
    email           = Column(String(255), unique=True, nullable=False)
    hashed_password = Column(String(255), nullable=False)
    phone           = Column(String(50))
    full_name       = Column(String(255))
    global_role     = Column(Enum(GlobalRole), nullable=False, default=GlobalRole.user)
    is_active       = Column(Boolean, default=True)
    created_at      = Column(DateTime(timezone=True), default=_now)
    updated_at      = Column(DateTime(timezone=True), default=_now, onupdate=_now)

    permissions     = relationship(
        "UserPermission",
        foreign_keys="UserPermission.user_id",
        back_populates="user",
        cascade="all, delete-orphan",
    )
    sessions        = relationship("Session",           back_populates="user", cascade="all, delete-orphan")
    memberships     = relationship("PartnerMembership", back_populates="user", cascade="all, delete-orphan")
    password_reset_tokens = relationship("PasswordResetToken", back_populates="user", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<User id={self.user_id} email={self.email!r}>"


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

class Session(Base):
    __tablename__ = "sessions"

    session_id = Column(Integer, primary_key=True, autoincrement=True)
    user_id    = Column(Integer, ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False)
    token_hash = Column(String(512), unique=True, nullable=False)
    issued_at  = Column(DateTime(timezone=True), default=_now)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    user_agent = Column(Text)
    ip_address = Column(INET)

    user = relationship("User", back_populates="sessions")


# ---------------------------------------------------------------------------
# Password Reset Tokens
# ---------------------------------------------------------------------------

class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"

    token_id   = Column(Integer, primary_key=True, autoincrement=True)
    user_id    = Column(Integer, ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False)
    token_hash = Column(String(128), unique=True, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used_at    = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=_now)

    user = relationship("User", back_populates="password_reset_tokens")


# ---------------------------------------------------------------------------
# User Permissions
# ---------------------------------------------------------------------------

class UserPermission(Base):
    __tablename__ = "user_permissions"

    user_permission_id = Column(Integer, primary_key=True, autoincrement=True)
    user_id            = Column(Integer, ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False)
    permission         = Column(String(100), nullable=False)
    granted_at         = Column(DateTime(timezone=True), default=_now)
    granted_by         = Column(Integer, ForeignKey("users.user_id", ondelete="SET NULL"), nullable=True)

    user = relationship("User", foreign_keys=[user_id], back_populates="permissions")

    __table_args__ = (
        UniqueConstraint("user_id", "permission", name="uq_user_permissions"),
    )


# ---------------------------------------------------------------------------
# Partners
# ---------------------------------------------------------------------------

class Partner(Base):
    __tablename__ = "partners"

    partner_id    = Column(Integer, primary_key=True, autoincrement=True)
    legal_name    = Column(String(255), unique=True, nullable=False)
    slug          = Column(String(255), unique=True, nullable=False)
    contact_email = Column(String(255), unique=True, nullable=False)
    contact_phone = Column(String(255), unique=True, nullable=False)
    is_active     = Column(Boolean, default=True)
    created_at    = Column(DateTime(timezone=True), default=_now)
    updated_at    = Column(DateTime(timezone=True), default=_now, onupdate=_now)

    memberships = relationship("PartnerMembership", back_populates="partner", cascade="all, delete-orphan")
    cameras     = relationship("Camera",            back_populates="partner")
    zones       = relationship("ParkingZone",       back_populates="partner")

    def __repr__(self) -> str:
        return f"<Partner id={self.partner_id} slug={self.slug!r}>"


# ---------------------------------------------------------------------------
# Partner Memberships
# ---------------------------------------------------------------------------

class PartnerMembership(Base):
    __tablename__ = "partner_memberships"

    partner_membership_id = Column(Integer, primary_key=True, autoincrement=True)
    user_id               = Column(Integer, ForeignKey("users.user_id",    ondelete="CASCADE"), nullable=False)
    partner_id            = Column(Integer, ForeignKey("partners.partner_id", ondelete="CASCADE"), nullable=False)
    user_role             = Column(String(50), nullable=False)
    read_scope            = Column(String(50), default="own")
    write_scope           = Column(String(50), default="own")
    delete_scope          = Column(String(50), default="own")
    created_at            = Column(DateTime(timezone=True), default=_now)

    user    = relationship("User",    back_populates="memberships")
    partner = relationship("Partner", back_populates="memberships")

    __table_args__ = (
        UniqueConstraint("user_id", "partner_id", name="uq_partner_membership"),
    )


# ---------------------------------------------------------------------------
# Cameras
# ---------------------------------------------------------------------------

class Camera(Base):
    __tablename__ = "cameras"

    camera_id          = Column(Integer, primary_key=True, autoincrement=True)
    title              = Column(String(200), nullable=False, unique=True)
    source             = Column(Text, nullable=False)
    image_width        = Column(Integer, nullable=False)
    image_height       = Column(Integer, nullable=False)
    calib              = Column(JSONB)
    latitude           = Column(Double, nullable=False)
    longitude          = Column(Double, nullable=False)
    partner_id         = Column(Integer, ForeignKey("partners.partner_id", ondelete="SET NULL"), nullable=True)
    created_by_user_id = Column(Integer, ForeignKey("users.user_id",       ondelete="SET NULL"), nullable=True)
    is_active          = Column(Boolean, default=True)
    last_snapshot_at   = Column(DateTime(timezone=True), nullable=True)
    created_at         = Column(DateTime(timezone=True), default=_now)
    updated_at         = Column(DateTime(timezone=True), default=_now, onupdate=_now)

    partner      = relationship("Partner", back_populates="cameras")
    created_by   = relationship("User",    foreign_keys=[created_by_user_id])
    zones        = relationship("ParkingZone", back_populates="camera", cascade="all, delete-orphan")
    weather_observations = relationship("WeatherObservation", back_populates="camera", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<Camera id={self.camera_id} title={self.title!r}>"


# ---------------------------------------------------------------------------
# Parking Zones
# ---------------------------------------------------------------------------

class ParkingZone(Base):
    __tablename__ = "parking_zones"

    parking_zone_id    = Column(Integer, primary_key=True, autoincrement=True)
    camera_id          = Column(Integer, ForeignKey("cameras.camera_id", ondelete="CASCADE"), nullable=False)
    zone_type          = Column(Enum(ZoneType, name="zone_types"), nullable=False)
    capacity           = Column(Integer, nullable=False)
    occupied           = Column(Integer, nullable=False, default=0)
    # free_count — GENERATED ALWAYS AS (capacity - occupied) STORED, читаем из БД
    confidence         = Column(Double, default=0.0)
    confidence_level   = Column(Enum(ConfidenceLevel, name="confidence_level_types"), nullable=True)
    pay                = Column(Integer, nullable=False, default=0)
    geometry           = Column(JSONB, nullable=False)
    image_polygon      = Column(JSONB, nullable=False)
    centroid_latitude = Column(Double, nullable=True)
    centroid_longitude = Column(Double, nullable=True)
    partner_id         = Column(Integer, ForeignKey("partners.partner_id", ondelete="SET NULL"), nullable=True)
    created_by_user_id = Column(Integer, ForeignKey("users.user_id",       ondelete="SET NULL"), nullable=True)
    is_active          = Column(Boolean, default=True)
    location_type      = Column(Enum(LocationType, name="location_types"), nullable=True)
    is_private         = Column(Boolean, nullable=True)
    is_accessible      = Column(Boolean, nullable=True)
    occupancy_updated_at = Column(DateTime(timezone=True), nullable=True)
    created_at         = Column(DateTime(timezone=True), default=_now)
    updated_at         = Column(DateTime(timezone=True), default=_now, onupdate=_now)

    camera      = relationship("Camera",  back_populates="zones")
    partner     = relationship("Partner", back_populates="zones")
    created_by  = relationship("User",    foreign_keys=[created_by_user_id])

    def __repr__(self) -> str:
        return f"<ParkingZone id={self.parking_zone_id} camera_id={self.camera_id}>"


# ---------------------------------------------------------------------------
# Datasources
# ---------------------------------------------------------------------------

class DataSource(Base):
    __tablename__ = "data_sources"

    source_id          = Column(Integer, primary_key=True, autoincrement=True)
    partner_id         = Column(Integer, ForeignKey("partners.partner_id", ondelete="SET NULL"), nullable=True)
    created_by_user_id = Column(Integer, ForeignKey("users.user_id",       ondelete="SET NULL"), nullable=True)
    source_type        = Column(String(50),  nullable=False)
    entity_type        = Column(String(50),  nullable=False)
    entity_id          = Column(Integer,     nullable=False)
    title              = Column(String(255), nullable=False)
    status             = Column(String(20),  nullable=False, default="unknown")
    last_data_at       = Column(DateTime(timezone=True), nullable=True)
    last_error         = Column(Text, nullable=True)
    is_active          = Column(Boolean, default=True)
    created_at         = Column(DateTime(timezone=True), default=_now)
    updated_at         = Column(DateTime(timezone=True), default=_now, onupdate=_now)

    __table_args__ = (
        UniqueConstraint("entity_type", "entity_id", name="uq_source_entity"),
    )

# ---------------------------------------------------------------------------
# Occupancy Observations
# ---------------------------------------------------------------------------

class OccupancyObservation(Base):
    __tablename__ = "occupancy_observations"

    observation_id     = Column(Integer, primary_key=True, autoincrement=True)
    zone_id            = Column(Integer, ForeignKey("parking_zones.parking_zone_id", ondelete="CASCADE"), nullable=False)
    camera_id          = Column(Integer, ForeignKey("cameras.camera_id",   ondelete="SET NULL"), nullable=True)
    partner_id         = Column(Integer, ForeignKey("partners.partner_id", ondelete="SET NULL"), nullable=True)
    source_type        = Column(String(50),  nullable=False)
    source_ref         = Column(String(255), nullable=True)
    capacity           = Column(Integer, nullable=False)
    occupied           = Column(Integer, nullable=False)
    # free_count — GENERATED ALWAYS AS (capacity - occupied) STORED
    confidence         = Column(Double, nullable=False)
    confidence_level   = Column(Enum(ConfidenceLevel, name="confidence_level_types",
                                     create_type=False), nullable=True)
    observed_at        = Column(DateTime(timezone=True), nullable=False)
    ingested_at        = Column(DateTime(timezone=True), default=_now)
    metadata_json      = Column("metadata", JSONB, nullable=True)
    created_by_user_id = Column(Integer, ForeignKey("users.user_id", ondelete="SET NULL"), nullable=True)

    zone       = relationship("ParkingZone", foreign_keys=[zone_id])
    camera     = relationship("Camera",      foreign_keys=[camera_id])
    partner    = relationship("Partner",     foreign_keys=[partner_id])
    created_by = relationship("User",        foreign_keys=[created_by_user_id])

    __table_args__ = (
        UniqueConstraint("source_type", "source_ref", name="uq_occupancy_source"),
    )

    def __repr__(self) -> str:
        return f"<OccupancyObservation id={self.observation_id} zone_id={self.zone_id}>"


# ---------------------------------------------------------------------------
# Detection Feedback
# ---------------------------------------------------------------------------

class DetectionFeedback(Base):
    __tablename__ = "detection_feedback"

    feedback_id             = Column(Integer, primary_key=True, autoincrement=True)
    detection_run_id        = Column(
        Integer,
        ForeignKey("occupancy_observations.observation_id", ondelete="CASCADE"),
        nullable=False,
    )
    created_by_user_id      = Column(Integer, ForeignKey("users.user_id", ondelete="SET NULL"), nullable=True)
    rating                  = Column(String(32), nullable=False)
    expected_occupied_count = Column(Integer, nullable=True)
    expected_free_count     = Column(Integer, nullable=True)
    error_type              = Column(String(64), nullable=True)
    comment                 = Column(Text, nullable=True)
    created_at              = Column(DateTime(timezone=True), default=_now)
    updated_at              = Column(DateTime(timezone=True), nullable=True)

    detection_run = relationship("OccupancyObservation", foreign_keys=[detection_run_id])
    created_by    = relationship("User", foreign_keys=[created_by_user_id])

    __table_args__ = (
        CheckConstraint(
            "rating IN ('correct', 'partially_correct', 'incorrect')",
            name="ck_detection_feedback_rating",
        ),
        CheckConstraint(
            "expected_occupied_count IS NULL OR expected_occupied_count >= 0",
            name="ck_detection_feedback_expected_occupied_non_negative",
        ),
        CheckConstraint(
            "expected_free_count IS NULL OR expected_free_count >= 0",
            name="ck_detection_feedback_expected_free_non_negative",
        ),
        CheckConstraint(
            """
            error_type IS NULL OR error_type IN (
                'false_positive_car',
                'false_negative_car',
                'wrong_zone_assignment',
                'bad_lighting',
                'bad_camera_angle',
                'calibration_problem',
                'other'
            )
            """,
            name="ck_detection_feedback_error_type",
        ),
    )

    def __repr__(self) -> str:
        return f"<DetectionFeedback id={self.feedback_id} run_id={self.detection_run_id}>"


# ---------------------------------------------------------------------------
# Forecasts
# ---------------------------------------------------------------------------

class Forecast(Base):
    __tablename__ = "forecasts"

    forecast_id            = Column(Integer, primary_key=True, autoincrement=True)
    zone_id                = Column(Integer, ForeignKey("parking_zones.parking_zone_id", ondelete="CASCADE"), nullable=False)
    camera_id              = Column(Integer, ForeignKey("cameras.camera_id",   ondelete="SET NULL"), nullable=True)
    partner_id             = Column(Integer, ForeignKey("partners.partner_id", ondelete="SET NULL"), nullable=True)
    model_type             = Column(String(50),  nullable=False)
    model_version          = Column(String(100), nullable=True)
    generated_at           = Column(DateTime(timezone=True), nullable=False)
    predicted_for          = Column(DateTime(timezone=True), nullable=False)
    capacity               = Column(Integer, nullable=False)
    predicted_occupied     = Column(Integer, nullable=False)
    # predicted_free_count — GENERATED ALWAYS AS (capacity - predicted_occupied) STORED
    probability_free_space = Column(Double, nullable=False)
    confidence             = Column(Double, nullable=False)
    confidence_level       = Column(Enum(ConfidenceLevel, name="confidence_level_types",
                                         create_type=False), nullable=True)
    metadata_json          = Column("metadata", JSONB, nullable=True)
    created_by_user_id     = Column(Integer, ForeignKey("users.user_id", ondelete="SET NULL"), nullable=True)

    zone       = relationship("ParkingZone", foreign_keys=[zone_id])
    camera     = relationship("Camera",      foreign_keys=[camera_id])
    partner    = relationship("Partner",     foreign_keys=[partner_id])
    created_by = relationship("User",        foreign_keys=[created_by_user_id])

    __table_args__ = (
        UniqueConstraint("zone_id", "generated_at", "predicted_for", name="uq_forecast_point"),
    )

    def __repr__(self) -> str:
        return f"<Forecast id={self.forecast_id} zone_id={self.zone_id} for={self.predicted_for}>"


# ---------------------------------------------------------------------------
# Weather Observations
# ---------------------------------------------------------------------------

class WeatherObservation(Base):
    __tablename__ = "weather_observations"

    camera_id     = Column(BigInteger, ForeignKey("cameras.camera_id"), primary_key=True, nullable=False)
    observed_at   = Column(DateTime(timezone=True), primary_key=True, nullable=False)
    temperature   = Column(Double, nullable=False)
    precipitation = Column(Double, nullable=False)

    camera = relationship("Camera", back_populates="weather_observations", foreign_keys=[camera_id])

    __table_args__ = (
        CheckConstraint("precipitation >= 0", name="ck_weather_observations_precipitation_non_negative"),
    )

    def __repr__(self) -> str:
        return f"<WeatherObservation camera_id={self.camera_id} observed_at={self.observed_at}>"

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

class RouteStatus(str, enum.Enum):
    active    = "active"
    completed = "completed"
    cancelled = "cancelled"
    replaced  = "replaced"


class RouteMode(str, enum.Enum):
    find_parking        = "find_parking"
    route_to_destination = "route_to_destination"


class Route(Base):
    __tablename__ = "routes"

    route_id              = Column(Integer, primary_key=True, autoincrement=True)
    user_id               = Column(Integer, ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False)
    mode                  = Column(Enum(RouteMode,   name="route_modes"),   nullable=False)
    provider              = Column(String(50), nullable=False, default="internal")
    origin_latitude       = Column(Double, nullable=False)
    origin_longitude      = Column(Double, nullable=False)
    destination_latitude  = Column(Double, nullable=True)
    destination_longitude = Column(Double, nullable=True)
    selected_zone_id      = Column(Integer, ForeignKey("parking_zones.parking_zone_id",
                                                        ondelete="SET NULL"), nullable=True)
    selected_candidate    = Column(JSONB, nullable=True)   # снапшот RouteCandidate
    eta_seconds           = Column(Integer, nullable=True)
    arrival_time          = Column(DateTime(timezone=True), nullable=True)
    polyline              = Column(Text, nullable=True)
    deeplink_url          = Column(Text, nullable=True)
    status                = Column(Enum(RouteStatus, name="route_statuses"),
                                   nullable=False, default=RouteStatus.active)
    created_at            = Column(DateTime(timezone=True), default=_now)
    updated_at            = Column(DateTime(timezone=True), default=_now, onupdate=_now)

    user          = relationship("User",        foreign_keys=[user_id])
    selected_zone = relationship("ParkingZone", foreign_keys=[selected_zone_id])

    def __repr__(self) -> str:
        return f"<Route id={self.route_id} user_id={self.user_id} status={self.status}>"
