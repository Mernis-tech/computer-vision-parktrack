from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


DetectionStatus = Literal["success", "failed", "partial"]
DetectionFeedbackRating = Literal["correct", "partially_correct", "incorrect"]
DetectionFeedbackErrorType = Literal[
    "false_positive_car",
    "false_negative_car",
    "wrong_zone_assignment",
    "bad_lighting",
    "bad_camera_angle",
    "calibration_problem",
    "other",
]
DetectorHealthStatus = Literal[
    "online",
    "stale",
    "offline",
    "no_data",
    "low_confidence",
    "error",
]


class AnalyticsSummary(BaseModel):
    active_zones_count: int
    total_capacity: int
    current_occupied_count: int | None
    current_free_count: int | None
    avg_occupancy_percent: float | None
    freshest_update_at: datetime | None
    oldest_update_at: datetime | None
    avg_update_interval_sec: float | None
    max_update_interval_sec: float | None
    avg_confidence: float | None


class OccupancyHistoryPoint(BaseModel):
    timestamp: datetime
    zone_id: int
    camera_id: int
    occupied_count: int | None
    free_count: int | None
    capacity: int
    occupancy_percent: float | None
    confidence_avg: float | None
    observations_count: int


class OccupancyHistoryResponse(BaseModel):
    granularity: str
    points: list[OccupancyHistoryPoint]


class OccupancyForecastPoint(BaseModel):
    timestamp: datetime
    zone_id: int
    predicted_occupied_count: float | None
    predicted_free_count: float | None
    predicted_occupancy_percent: float | None
    model_version: str | None
    forecast_created_at: datetime | None


class OccupancyForecastResponse(BaseModel):
    available: bool
    reason: str | None
    points: list[OccupancyForecastPoint]


class ObservationsRatePoint(BaseModel):
    timestamp: datetime
    observations_count: int


class ObservationsRateResponse(BaseModel):
    granularity: str
    points: list[ObservationsRatePoint]


class ConfidencePoint(BaseModel):
    timestamp: datetime
    zone_id: int | None
    camera_id: int | None
    confidence_avg: float | None
    confidence_min: float | None
    confidence_max: float | None
    observations_count: int


class ConfidenceResponse(BaseModel):
    granularity: str
    points: list[ConfidencePoint]


class UpdateFrequencyZone(BaseModel):
    zone_id: int
    camera_id: int
    avg_update_interval_sec: float | None
    max_update_interval_sec: float | None
    last_update_at: datetime | None


class UpdateFrequencyResponse(BaseModel):
    avg_update_interval_sec: float | None
    max_update_interval_sec: float | None
    freshest_update_at: datetime | None
    oldest_update_at: datetime | None
    by_zone: list[UpdateFrequencyZone]


class DetectorHealthRow(BaseModel):
    zone_id: int
    camera_id: int
    camera_title: str
    capacity: int
    occupied_count: int | None
    free_count: int | None
    occupancy_percent: float | None
    confidence_avg: float | None
    last_update_at: datetime | None
    sec_ago: int | None
    avg_update_interval_sec: float | None
    max_update_interval_sec: float | None
    status: DetectorHealthStatus


class DetectorHealthResponse(BaseModel):
    items: list[DetectorHealthRow]


class DetectionRun(BaseModel):
    detection_run_id: int
    camera_id: int
    zone_id: int | None
    started_at: datetime
    finished_at: datetime | None
    status: DetectionStatus
    processing_time_ms: int | None
    model_version: str | None
    detected_cars_count: int | None
    occupied_count: int | None
    free_count: int | None
    capacity: int | None
    confidence_avg: float | None
    error_code: str | None
    error_message: str | None
    has_feedback: bool
    raw_snapshot_url: str | None
    annotated_snapshot_url: str | None
    yolo_labels_url: str | None


class DetectionRunListResponse(BaseModel):
    items: list[DetectionRun]


class CreateDetectionFeedbackRequest(BaseModel):
    rating: DetectionFeedbackRating
    expected_occupied_count: int | None = Field(None, ge=0)
    expected_free_count: int | None = Field(None, ge=0)
    error_type: DetectionFeedbackErrorType | None = None
    comment: str | None = None


class CreateDetectionFeedbackResponse(BaseModel):
    feedback_id: int
    detection_run_id: int
    created_at: datetime


class DetectionFeedback(BaseModel):
    feedback_id: int
    detection_run_id: int
    created_by_user_id: int
    created_by_email: str | None
    rating: DetectionFeedbackRating
    expected_occupied_count: int | None
    expected_free_count: int | None
    error_type: DetectionFeedbackErrorType | None
    comment: str | None
    created_at: datetime
    updated_at: datetime | None


class DetectionFeedbackListResponse(BaseModel):
    items: list[DetectionFeedback]


class ForecastQualityMetrics(BaseModel):
    mae_occupied_count: float | None
    mae_occupancy_percent: float | None
    bias_occupancy_percent: float | None
    points_count: int


class ForecastQualityPoint(BaseModel):
    timestamp: datetime
    zone_id: int
    actual_occupied_count: int | None
    actual_occupancy_percent: float | None
    predicted_occupied_count: float | None
    predicted_occupancy_percent: float | None
    absolute_error_occupancy_percent: float | None


class ForecastQualityResponse(BaseModel):
    granularity: str
    metrics: ForecastQualityMetrics
    points: list[ForecastQualityPoint]
