import sys
import time
from pathlib import Path
from datetime import datetime, timezone
from typing import Tuple, List, Dict, Any

import cv2
import numpy as np
from openvino import Core, Tensor
import requests

from yolo_utils import (
    load_class_names,
    letterbox,
    parse_with_embedded_nms,
    parse_raw_yolo_outputs,
)
from calibration import (
    load_calibration_from_dict,
    compute_fullview_rectified_camera_matrix,
)
from geometry import (
    build_curved_polygon_from_anchors,
    signed_depth_to_polygon,
    overlap_ratio_box_in_polygon,
)
from visualization import (
    vivid_palette,
    draw_polygon_outline,
    put_text_outline,
    draw_legend_bottom_left,
    draw_box_with_alpha,
)
from api_client import (
    fetch_next_camera,
    fetch_zones_for_camera,
    update_zone_occupancy,
    create_occupancy_observation,
)
from public_snapshot_storage import (
    S3SnapshotStorage,
    SnapshotStorageConfig,
    encode_yolo_labels,
)


# ---------- Вспомогательные шаги пайплайна ----------


def fetch_image_bgr(url: str, timeout: float = 10.0, headers: dict | None = None) -> np.ndarray:
    h = headers or {}
    with requests.get(url, headers=h, timeout=timeout) as r:
        r.raise_for_status()
        data = r.content
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        ct = (r.headers.get("Content-Type") or "").lower()
        raise RuntimeError(f"cannot decode image from {url} (Content-Type={ct}, bytes={len(data)})")
    return img


def probe_content_type(url: str, timeout: float = 5.0, headers: dict | None = None) -> str:
    h = dict(headers or {})
    h.setdefault("Range", "bytes=0-2047")
    with requests.get(url, headers=h, timeout=timeout, stream=True) as r:
        # не читаем тело, нам важен только Content-Type
        return (r.headers.get("Content-Type") or "").lower()


def grab_frames_video_opencv(url: str, targets: list[float], timeout_open_sec: float = 5.0) -> list[np.ndarray]:
    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    if not cap.isOpened():
        raise RuntimeError(f"cannot open source: {url}")

    # Файлы и обычные HTTP-MP4 лучше читать по временным меткам. Исходная
    # реализация читала файл быстрее реального времени и ждала 10/20 секунд по
    # настенным часам. За это время короткий файл успевал закончиться, поэтому
    # локальное тестовое видео почти всегда давало got 1/3 frames.
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
    frame_count = float(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0)
    duration_sec = frame_count / fps if fps > 0.0 and frame_count > 0.0 else 0.0

    if duration_sec > 0.0:
        last_safe_position = max(0.0, duration_sec - max(1.0 / fps, 0.05))
        requested_positions = [min(max(float(target), 0.0), last_safe_position) for target in targets]
        frames: list[np.ndarray] = []

        for position_sec in requested_positions:
            cap.set(cv2.CAP_PROP_POS_MSEC, position_sec * 1000.0)
            ok, frame = cap.read()
            if not ok or frame is None:
                cap.release()
                raise RuntimeError(
                    f"cannot read video frame at {position_sec:.2f}s from source: {url}"
                )
            frames.append(frame.copy())

        cap.release()
        return frames

    frames: list[np.ndarray] = []
    start = time.time()
    idx = 0
    deadline = start + max(targets) + max(5.0, timeout_open_sec)

    # дадим немного времени на появление первых кадров
    while time.time() < deadline and idx < len(targets):
        ok, frame = cap.read()
        if not ok or frame is None:
            time.sleep(0.02)
            continue

        elapsed = time.time() - start
        if elapsed >= targets[idx]:
            frames.append(frame.copy())
            idx += 1

    cap.release()

    if len(frames) < len(targets):
        raise RuntimeError(f"cannot read enough frames from video: got {len(frames)}/{len(targets)}")
    return frames


def grab_frames_any(url: str, targets: list[float], headers: dict | None = None) -> list[np.ndarray]:
    """
    Если это image/*: делаем N запросов с паузой (targets как расписание).
    Если это видео: читаем из OpenCV-VideoCapture.
    """
    ctype = ""
    try:
        ctype = probe_content_type(url, headers=headers)
    except requests.RequestException:
        # если проба не удалась — попробуем как видео
        ctype = ""

    if ctype.startswith("image/"):
        frames: list[np.ndarray] = []
        t0 = time.time()
        for t in targets:
            wait = (t0 + t) - time.time()
            if wait > 0:
                time.sleep(wait)
            frames.append(fetch_image_bgr(url, headers=headers))
        return frames

    # иначе пытаемся видео
    try:
        return grab_frames_video_opencv(url, targets)
    except Exception:
        # последний шанс: вдруг это всё-таки картинка (без корректного Content-Type)
        frames: list[np.ndarray] = []
        t0 = time.time()
        for t in targets:
            wait = (t0 + t) - time.time()
            if wait > 0:
                time.sleep(wait)
            frames.append(fetch_image_bgr(url, headers=headers))
        return frames


def setup_http_session(api_token: str) -> requests.Session:
    """
    Создаёт HTTP-сессию с нужным заголовком Authorization.

    Аргументы:
        api_token (str): Bearer токен для доступа к API.

    Возвращает:
        requests.Session: Готовая сессия.
    """
    session = requests.Session()
    if api_token:
        session.headers.update({"Authorization": f"Bearer {api_token}"})
    return session


def fetch_camera_and_calibration(
    http_session: requests.Session,
    base_api_url: str,
):
    """
    Запрашивает следующую камеру и парсит калибровку.

    Аргументы:
        http_session (requests.Session): HTTP-сессия.
        base_api_url (str): Базовый URL API.

    Возвращает:
        tuple:
            camera_id (int),
            video_source_url (str),
            frame_width_from_calib (int),
            frame_height_from_calib (int),
            camera_matrix (np.ndarray),
            distortion_coefficients (np.ndarray),
            rectified_camera_matrix (np.ndarray | None),
            crop_x, crop_y, crop_width, crop_height,
            confidence_threshold (float | None): если задано в calib.confidence
                — используется как порог детекции для этой камеры; иначе None
                и вызывающая сторона должна подставить дефолт из аргументов CLI.
    """
    camera_info = fetch_next_camera(http_session, base_api_url)

    camera_id = int(camera_info["camera_id"])
    video_source_url = camera_info["source"]
    calibration_raw = camera_info.get("calib") or {}

    if not calibration_raw:
        print("[WARN] camera calibration is missing, using full frame", flush=True)

    crop_x = calibration_raw.get("crop_x")
    crop_y = calibration_raw.get("crop_y")
    crop_width = calibration_raw.get("crop_width")
    crop_height = calibration_raw.get("crop_height")

    # Per-camera порог уверенности из calib. Допустимы ключи: confidence,
    # conf_threshold, confidence_threshold — кладёт админка, поле опционально.
    confidence_threshold = None
    for key in ("confidence", "conf_threshold", "confidence_threshold"):
        raw_value = calibration_raw.get(key)
        if raw_value is None:
            continue
        try:
            confidence_threshold = float(raw_value)
        except (TypeError, ValueError):
            continue
        break

    calibration_result = load_calibration_from_dict(calibration_raw)

    if calibration_result is None:
        calibration_image_width = None
        calibration_image_height = None
        camera_matrix = None
        distortion_coefficients = None
        rectified_camera_matrix_opt = None
        balance = 0.0
    else:
        (
            calibration_image_width,
            calibration_image_height,
            camera_matrix,
            distortion_coefficients,
            rectified_camera_matrix_opt,
            balance,
        ) = calibration_result

    return (
        camera_id,
        video_source_url,
        calibration_image_width,
        calibration_image_height,
        camera_matrix,
        distortion_coefficients,
        rectified_camera_matrix_opt,
        crop_x,
        crop_y,
        crop_width,
        crop_height,
        confidence_threshold,
    )


def grab_first_frame(
    video_source_url: str,
    timeout_seconds: float = 5.0
) -> np.ndarray:
    """
    Считывает первый доступный кадр из видеопотока.

    Аргументы:
        video_source_url (str): URL/путь к видеоисточнику.
        timeout_seconds (float): Таймаут на ожидание кадра.

    Возвращает:
        np.ndarray: Первый кадр в формате BGR.

    Исключения:
        RuntimeError: если кадр получить не удалось.
    """
    video_capture = cv2.VideoCapture(video_source_url, cv2.CAP_FFMPEG)
    if not video_capture.isOpened():
        raise RuntimeError(f"cannot open source: {video_source_url}")

    first_frame_bgr = None
    deadline = time.time() + timeout_seconds

    while time.time() < deadline:
        read_success, frame_bgr = video_capture.read()
        if read_success and frame_bgr is not None:
            first_frame_bgr = frame_bgr
            break
        time.sleep(0.05)

    video_capture.release()

    if first_frame_bgr is None:
        raise RuntimeError("failed to read frame from source")

    return first_frame_bgr


def adjust_camera_matrix_to_frame_size(
    camera_matrix: np.ndarray,
    calibration_image_width: int,
    calibration_image_height: int,
    frame_width: int,
    frame_height: int,
) -> np.ndarray:
    """
    Подгоняет матрицу камеры под фактический размер кадра, если он
    отличается от размера, использованного при калибровке.

    Аргументы:
        camera_matrix (np.ndarray): Матрица камеры K (3x3).
        calibration_image_width (int): Ширина, использованная при калибровке.
        calibration_image_height (int): Высота, использованная при калибровке.
        frame_width (int): Реальная ширина кадра.
        frame_height (int): Реальная высота кадра.

    Возвращает:
        np.ndarray: Обновлённая матрица камеры K (3x3).
    """
    if (
        calibration_image_width == frame_width
        and calibration_image_height == frame_height
    ):
        return camera_matrix

    scale_x = frame_width / calibration_image_width
    scale_y = frame_height / calibration_image_height

    camera_matrix = camera_matrix.copy()
    camera_matrix[0, 0] *= scale_x
    camera_matrix[1, 1] *= scale_y
    camera_matrix[0, 2] *= scale_x
    camera_matrix[1, 2] *= scale_y

    return camera_matrix


def build_curved_zones_from_api(
    http_session: requests.Session,
    base_api_url: str,
    camera_id: int,
    camera_matrix: np.ndarray,
    distortion_coefficients: np.ndarray,
    rectified_camera_matrix: np.ndarray,
    samples_per_edge: int,
):
    """
    Получает зоны через API и строит для каждой изогнутый полигон.

    Аргументы:
        http_session (requests.Session): HTTP-сессия.
        base_api_url (str): Базовый URL API.
        camera_id (int): Идентификатор камеры.
        camera_matrix (np.ndarray): Матрица камеры K.
        distortion_coefficients (np.ndarray): Коэффициенты дисторсии D.
        rectified_camera_matrix (np.ndarray): Матрица newK.
        samples_per_edge (int): Плотность дискретизации границ зоны.

    Возвращает:
        tuple:
            curved_zone_polygons (list[np.ndarray]): список полигонов (каждый (M, 2)).
            zone_identifiers (list[int]): соответствующие zone_id.
    """
    zones_from_api = fetch_zones_for_camera(http_session, base_api_url, camera_id)

    if not zones_from_api:
        print(f"[WARN] no zones for camera {camera_id}", file=sys.stderr)

    curved_zone_polygons = []
    zone_identifiers = []

    for zone_description in zones_from_api:
        # По спеке ParkTrack OpenAPI зона несёт image_polygon: массив из 4 точек,
        # где каждая точка — это [x, y] (массив двух integer, не объект).
        zone_image_polygon = zone_description.get("image_polygon") or []
        if len(zone_image_polygon) < 3:
            # игнорируем некорректные зоны из API
            continue

        distorted_anchors_pixels = np.array(
            [[float(point[0]), float(point[1])] for point in zone_image_polygon],
            dtype=np.float64
        )

        curved_zone_polygon = build_curved_polygon_from_anchors(
            distorted_anchors_pixels,
            camera_matrix,
            distortion_coefficients,
            rectified_camera_matrix,
            samples_per_edge=max(8, samples_per_edge),
        )

        curved_zone_polygons.append(curved_zone_polygon)
        zone_identifiers.append(int(zone_description["zone_id"]))

    if not curved_zone_polygons:
        print("[WARN] no valid polygons built from zones", file=sys.stderr)

    return curved_zone_polygons, zone_identifiers


def run_openvino_inference_on_frame(
    frame_bgr: np.ndarray,
    model_xml_path: Path,
    device: str,
    img_size: int,
    confidence_threshold: float,
    car_only: bool,
):
    """
    Запускает инференс YOLO-модели OpenVINO на одном кадре.

    Аргументы:
        frame_bgr (np.ndarray): Входной кадр BGR.
        model_xml_path (Path): Путь к model.xml.
        device (str): Устройство OpenVINO (AUTO, CPU, GPU, ...).
        img_size (int): Размер входного изображения модели.
        confidence_threshold (float): Порог уверенности.
        car_only (bool): Оставлять только класс 'car' (если он есть).

    Возвращает:
        tuple:
            bounding_boxes_xyxy (np.ndarray),
            detection_scores (np.ndarray),
            detection_class_ids (np.ndarray),
            class_names (list[str]),
            resize_ratio (float),
            padding_width (float),
            padding_height (float)
    """
    if not model_xml_path.exists():
        raise RuntimeError(f"model.xml not found: {model_xml_path}")

    class_names = load_class_names(model_xml_path.parent)
    car_class_id = 0  # по умолчанию считаем, что класс 'car' = 0

    openvino_core = Core()
    openvino_model = openvino_core.read_model(model=str(model_xml_path))
    compiled_model = openvino_core.compile_model(
        model=openvino_model,
        device_name=device
    )
    model_input_tensor = compiled_model.inputs[0]

    resized_frame_bgr, resize_ratio, (padding_width, padding_height) = letterbox(
        frame_bgr,
        target_shape=img_size
    )

    resized_frame_rgb = resized_frame_bgr[:, :, ::-1].astype(np.float32) / 255.0
    if model_input_tensor.element_type.type_name == "f16":
        resized_frame_rgb = resized_frame_rgb.astype(np.float16)
    else:
        resized_frame_rgb = resized_frame_rgb.astype(np.float32)

    model_input_blob = np.transpose(resized_frame_rgb, (2, 0, 1))[None, ...]

    infer_request = compiled_model.create_infer_request()
    infer_request.set_tensor(compiled_model.inputs[0], Tensor(model_input_blob))
    infer_request.infer()

    model_outputs = [infer_request.get_tensor(output_node) for output_node in compiled_model.outputs]

    parsed_outputs = parse_with_embedded_nms(
        model_outputs,
        confidence_threshold=confidence_threshold
    )

    if parsed_outputs is None:
        bounding_boxes_xyxy, detection_scores, detection_class_ids = parse_raw_yolo_outputs(
            model_outputs,
            confidence_threshold=confidence_threshold,
            car_only=car_only or (len(class_names) == 1 and class_names[0].lower() == "car"),
            car_class_id=car_class_id,
            nms_iou_threshold=0.5
        )
    else:
        bounding_boxes_xyxy, detection_scores, detection_class_ids = parsed_outputs
        if car_only or (len(class_names) == 1 and class_names[0].lower() == "car"):
            car_mask = (detection_class_ids.astype(int) == car_class_id)
            bounding_boxes_xyxy = bounding_boxes_xyxy[car_mask]
            detection_scores = detection_scores[car_mask]
            detection_class_ids = detection_class_ids[car_mask]

    return (
        bounding_boxes_xyxy,
        detection_scores,
        detection_class_ids,
        class_names,
        resize_ratio,
        padding_width,
        padding_height,
    )


def restore_boxes_to_original_frame(
    bounding_boxes_xyxy: np.ndarray,
    resize_ratio: float,
    padding_width: float,
    padding_height: float,
    frame_width: int,
    frame_height: int,
) -> np.ndarray:
    """
    Переводит координаты боксов после letterbox обратно в систему
    координат исходного кадра.

    Аргументы:
        bounding_boxes_xyxy (np.ndarray): Боксы в координатах letterbox.
        resize_ratio (float): Масштаб.
        padding_width (float): Горизонтальный паддинг.
        padding_height (float): Вертикальный паддинг.
        frame_width (int): Ширина исходного кадра.
        frame_height (int): Высота исходного кадра.

    Возвращает:
        np.ndarray: Боксы в координатах исходного кадра.
    """
    if bounding_boxes_xyxy.shape[0] == 0:
        return bounding_boxes_xyxy

    boxes = bounding_boxes_xyxy.copy()
    boxes[:, [0, 2]] -= padding_width
    boxes[:, [1, 3]] -= padding_height
    boxes[:, :4] /= resize_ratio

    boxes[:, 0::2] = boxes[:, 0::2].clip(0, frame_width - 1)
    boxes[:, 1::2] = boxes[:, 1::2].clip(0, frame_height - 1)

    return boxes


def assign_detections_to_zones(
    bounding_boxes_xyxy: np.ndarray,
    detection_scores: np.ndarray,
    detection_class_ids: np.ndarray,
    curved_zone_polygons: List[np.ndarray],
    zone_identifiers: List[int],
    frame_width: int,
    frame_height: int,
):
    """
    Присваивает каждую детекцию (машину) конкретной зоне
    на основе:
        - центр бокса должен быть внутри полигона;
        - выбираем зону с максимальным overlap.

    Аргументы:
        bounding_boxes_xyxy, detection_scores, detection_class_ids: результаты детекции.
        curved_zone_polygons (list[np.ndarray]): список полигонов зон.
        zone_identifiers (list[int]): идентификаторы зон.
        frame_width (int): ширина кадра.
        frame_height (int): высота кадра.

    Возвращает:
        tuple:
            zone_statistics (list[dict]): статистика по зонам (occupied, cars, confidence=0.0).
            car_assigned_zone_indices (list[int]): индекс зоны для каждой детекции или -1.
    """
    zone_statistics: List[Dict[str, Any]] = []
    for index in range(len(curved_zone_polygons)):
        zone_statistics.append({
            "id": int(zone_identifiers[index]),
            "occupied": 0,
            "cars": [],
            "confidence": 0.0,
        })

    car_assigned_zone_indices = [-1] * bounding_boxes_xyxy.shape[0]

    for detection_index, (bounding_box_xyxy, detection_score, detection_class_id) in enumerate(
        zip(bounding_boxes_xyxy, detection_scores, detection_class_ids)
    ):
        box_center_x = float((bounding_box_xyxy[0] + bounding_box_xyxy[2]) * 0.5)
        box_center_y = float((bounding_box_xyxy[1] + bounding_box_xyxy[3]) * 0.5)
        box_center = (box_center_x, box_center_y)

        best_zone_index = -1
        best_overlap_ratio = 0.0

        for zone_index, curved_polygon in enumerate(curved_zone_polygons):
            depth_inside_polygon = signed_depth_to_polygon(
                box_center,
                curved_polygon
            )
            if depth_inside_polygon <= 0.0:
                continue

            overlap_ratio = overlap_ratio_box_in_polygon(
                bounding_box_xyxy,
                curved_polygon,
                frame_height,
                frame_width
            )
            if overlap_ratio > best_overlap_ratio:
                best_overlap_ratio = overlap_ratio
                best_zone_index = zone_index

        assigned_zone_index = best_zone_index if best_overlap_ratio > 0.0 else -1
        car_assigned_zone_indices[detection_index] = assigned_zone_index

        if assigned_zone_index >= 0:
            depth_inside_assigned_polygon = signed_depth_to_polygon(
                box_center,
                curved_zone_polygons[assigned_zone_index]
            )
            overlap_ratio_assigned = overlap_ratio_box_in_polygon(
                bounding_box_xyxy,
                curved_zone_polygons[assigned_zone_index],
                frame_height,
                frame_width
            )

            zone_statistics[assigned_zone_index]["occupied"] += 1
            zone_statistics[assigned_zone_index]["cars"].append({
                "det_index": detection_index,
                "center": [box_center_x, box_center_y],
                "box": [
                    float(bounding_box_xyxy[0]),
                    float(bounding_box_xyxy[1]),
                    float(bounding_box_xyxy[2]),
                    float(bounding_box_xyxy[3]),
                ],
                "score": float(detection_score),
                "class_id": int(detection_class_id),
                "depth_px": float(depth_inside_assigned_polygon),
                "overlap_ratio": float(overlap_ratio_assigned),
            })

    return zone_statistics, car_assigned_zone_indices


def compute_zone_confidences(zone_statistics: List[Dict[str, Any]]) -> None:
    """
    Заполняет поле "confidence" для каждой зоны: усреднённое score * overlap_ratio
    по всем машинам в зоне.

    Аргументы:
        zone_statistics (list[dict]): Статистика по зонам.
    """
    for zone_info in zone_statistics:
        cars_in_zone = zone_info["cars"]
        if not cars_in_zone:
            zone_info["confidence"] = 0.0
            continue

        weighted_scores_sum = 0.0
        for car_info in cars_in_zone:
            weighted_scores_sum += car_info["score"] * car_info["overlap_ratio"]
        zone_info["confidence"] = float(weighted_scores_sum / len(cars_in_zone))

def aggregate_detections_across_frames(
    list_of_boxes: List[np.ndarray],
    list_of_scores: List[np.ndarray],
    list_of_class_ids: List[np.ndarray],
    iou_threshold: float = 0.5,
    min_appearances: int = 2,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Оставляет только те боксы, которые встретились не меньше min_appearances раз
    на разных кадрах. Координаты и score усредняются.
    """
    clusters: List[Dict[str, Any]] = []

    def iou(box_a, box_b) -> float:
        xa1, ya1, xa2, ya2 = box_a
        xb1, yb1, xb2, yb2 = box_b

        inter_x1 = max(xa1, xb1)
        inter_y1 = max(ya1, yb1)
        inter_x2 = min(xa2, xb2)
        inter_y2 = min(ya2, yb2)

        if inter_x2 <= inter_x1 or inter_y2 <= inter_y1:
            return 0.0

        inter_area = (inter_x2 - inter_x1) * (inter_y2 - inter_y1)
        area_a = (xa2 - xa1) * (ya2 - ya1)
        area_b = (xb2 - xb1) * (yb2 - yb1)
        union = area_a + area_b - inter_area
        if union <= 0.0:
            return 0.0
        return inter_area / union

    for boxes, scores, class_ids in zip(list_of_boxes, list_of_scores, list_of_class_ids):
        for box, score, cls_id in zip(boxes, scores, class_ids):
            best_cluster = None
            best_iou = 0.0
            for cluster in clusters:
                if cluster["class_id"] != int(cls_id):
                    continue
                cluster_box = cluster["mean_box"]
                current_iou = iou(cluster_box, box)
                if current_iou > best_iou:
                    best_iou = current_iou
                    best_cluster = cluster

            if best_cluster is None or best_iou < iou_threshold:
                clusters.append(
                    {
                        "class_id": int(cls_id),
                        "boxes": [box.astype(float)],
                        "scores": [float(score)],
                        "mean_box": box.astype(float),
                    }
                )
            else:
                best_cluster["boxes"].append(box.astype(float))
                best_cluster["scores"].append(float(score))
                best_cluster["mean_box"] = np.mean(best_cluster["boxes"], axis=0)

    aggregated_boxes = []
    aggregated_scores = []
    aggregated_class_ids = []

    for cluster in clusters:
        if len(cluster["boxes"]) >= min_appearances:
            aggregated_boxes.append(cluster["mean_box"])
            aggregated_scores.append(np.mean(cluster["scores"]))
            aggregated_class_ids.append(cluster["class_id"])

    if not aggregated_boxes:
        return (
            np.zeros((0, 4), dtype=np.float32),
            np.zeros((0,), dtype=np.float32),
            np.zeros((0,), dtype=np.int32),
        )

    return (
        np.stack(aggregated_boxes).astype(np.float32),
        np.array(aggregated_scores, dtype=np.float32),
        np.array(aggregated_class_ids, dtype=np.int32),
    )


def render_visualization_frame(
    base_frame_bgr: np.ndarray,
    curved_zone_polygons: List[np.ndarray],
    zone_statistics: List[Dict[str, Any]],
    car_assigned_zone_indices: List[int],
    bounding_boxes_xyxy: np.ndarray,
    detection_scores: np.ndarray,
    detection_class_ids: np.ndarray,
    class_names: List[str],
    zone_colors_bgr: List[Tuple[int, int, int]],
    zone_outline_thickness: int,
    car_alpha: float,
) -> np.ndarray:
    """
    Строит финальный кадр с визуализацией зон и машин.

    Возвращает:
        np.ndarray: Кадр BGR с разметкой.
    """
    visualization_frame_bgr = base_frame_bgr.copy()

    # Зоны
    for zone_index, curved_polygon in enumerate(curved_zone_polygons):
        zone_color_bgr = zone_colors_bgr[zone_index]
        draw_polygon_outline(
            visualization_frame_bgr,
            curved_polygon,
            color_bgr=zone_color_bgr,
            thickness=zone_outline_thickness
        )

        polygon_center_xy = np.mean(curved_polygon, axis=0)
        zone_label_text = f"Zone {zone_statistics[zone_index]['id']}: {zone_statistics[zone_index]['occupied']}"
        text_x = int(round(polygon_center_xy[0]))
        text_y = int(round(polygon_center_xy[1]))
        put_text_outline(
            visualization_frame_bgr,
            zone_label_text,
            (max(0, text_x - 40), max(12, text_y)),
            zone_color_bgr
        )

    # Машины
    for detection_index, (bounding_box_xyxy, detection_score, detection_class_id) in enumerate(
        zip(bounding_boxes_xyxy, detection_scores, detection_class_ids)
    ):
        if 0 <= int(detection_class_id) < len(class_names):
            detection_class_name = class_names[int(detection_class_id)]
        else:
            detection_class_name = str(int(detection_class_id))

        detection_score_percent = int(round(float(detection_score) * 100))
        detection_label_text = str(detection_score_percent)

        assigned_zone_index = car_assigned_zone_indices[detection_index]

        if assigned_zone_index >= 0:
            box_edge_color_bgr = zone_colors_bgr[assigned_zone_index]
            box_fill_color_bgr = zone_colors_bgr[assigned_zone_index]
            draw_box_with_alpha(
                visualization_frame_bgr,
                bounding_box_xyxy,
                detection_label_text,
                edge_color_bgr=box_edge_color_bgr,
                fill_color_bgr=box_fill_color_bgr,
                alpha=car_alpha,
                thickness=1
            )
        else:
            draw_box_with_alpha(
                visualization_frame_bgr,
                bounding_box_xyxy,
                detection_label_text,
                edge_color_bgr=(0, 255, 0),
                fill_color_bgr=None,
                alpha=0.0,
                thickness=2
            )

        box_center_x = int(round((bounding_box_xyxy[0] + bounding_box_xyxy[2]) * 0.5))
        box_center_y = int(round((bounding_box_xyxy[1] + bounding_box_xyxy[3]) * 0.5))
        cv2.circle(visualization_frame_bgr, (box_center_x, box_center_y), 3, (0, 0, 0), -1)
        cv2.circle(visualization_frame_bgr, (box_center_x, box_center_y), 2, (255, 255, 255), -1)

    legend_entries = [
        f"Zone {zone_info['id']}: {zone_info['occupied']}"
        for zone_info in zone_statistics
    ]
    draw_legend_bottom_left(
        visualization_frame_bgr,
        legend_entries,
        zone_colors_bgr,
        margin_pixels=10,
        padding_pixels=8,
        line_height_pixels=22,
        background_alpha=0.5
    )

    return visualization_frame_bgr


def build_result_payload(
    camera_id: int,
    video_source_url: str,
    frame_width: int,
    frame_height: int,
    samples_per_edge: int,
    base_api_url: str,
    zone_statistics: List[Dict[str, Any]],
    bounding_boxes_xyxy: np.ndarray,
    observed_at_iso: str,
) -> Dict[str, Any]:
    """
    Собирает JSON-пейлоад результата, аналогичный тому, что раньше печатался в main.

    Возвращает:
        dict: Готовый JSON-словарь результата.
    """
    result_payload = {
        "timestamp": observed_at_iso,
        "camera_id": camera_id,
        "source": str(video_source_url),
        "zones": zone_statistics,
        "totals": {
            "cars_detected": int(bounding_boxes_xyxy.shape[0]),
            "cars_in_zones": int(sum(zone_info["occupied"] for zone_info in zone_statistics)),
        },
        "meta": {
            "frame_width": int(frame_width),
            "frame_height": int(frame_height),
            "samples_per_edge": int(max(8, samples_per_edge)),
            "base_api_url": base_api_url,
        },
    }
    return result_payload


def push_zone_updates_to_api(
    http_session: requests.Session,
    base_api_url: str,
    zone_statistics: List[Dict[str, Any]],
    observed_at_iso: str,
    snapshots: Dict[str, Dict[str, str]],
    detected_cars_count: int,
) -> None:
    """
    Проходит по всем зонам и публикует результаты детекции в API двумя шагами:
      1) PUT  /zones/{zone_id} — обновляет текущее состояние зоны.
      2) POST /occupancy/new — пишет это же состояние в историю occupancy.

    Важно: запись истории создаётся только после успешного PUT, чтобы не получить
    ситуацию, когда в истории есть наблюдение, а текущая зона не обновилась.
    """
    for zone_info in zone_statistics:
        zone_id = int(zone_info["id"])
        occupied_count = int(zone_info["occupied"])
        zone_confidence = float(zone_info["confidence"])

        try:
            updated_zone = update_zone_occupancy(
                http_session,
                base_api_url,
                zone_id=zone_id,
                occupied_count=occupied_count,
                zone_confidence=zone_confidence,
            )
        except Exception as exception:
            print(
                f"[WARN] failed to update zone {zone_id}; "
                f"occupancy history row will not be created: {exception}",
                file=sys.stderr
            )
            continue

        capacity = None
        if isinstance(updated_zone, dict) and updated_zone.get("capacity") is not None:
            capacity = int(updated_zone["capacity"])
        elif zone_info.get("capacity") is not None:
            capacity = int(zone_info["capacity"])

        try:
            create_occupancy_observation(
                http_session,
                base_api_url,
                zone_id=zone_id,
                occupied_count=occupied_count,
                zone_confidence=zone_confidence,
                observed_at_iso=observed_at_iso,
                source_type="camera_cv",
                source_ref=f"zone:{zone_id}:detected_at:{observed_at_iso}",
                capacity=capacity,
                metadata={
                    "writer": "detection_pipeline",
                    "action": "zone_occupancy_update",
                    "detected_cars_count": detected_cars_count,
                    "snapshots": snapshots,
                },
            )
        except Exception as exception:
            print(
                f"[WARN] zone {zone_id} was updated, "
                f"but failed to create occupancy history row: {exception}",
                file=sys.stderr
            )


# ---------- Высокоуровневый единый шаг ----------

def run_single_frame_pipeline(args):
    """
    Запускает полный пайплайн "один кадр" от API до результата.

    Аргументы:
        args (argparse.Namespace): Аргументы командной строки.

    Возвращает:
        tuple:
            result_payload (dict): JSON-словарь результата.
            visualization_frame_bgr (np.ndarray): Визуализированный кадр.
            source_frame: Оригинальный кадр с камеры (без визуализаций)
    """
    base_api_url = args.base_api_url

    # Проверяем S3-конфигурацию до получения кадров и запуска инференса.
    snapshot_storage = S3SnapshotStorage(SnapshotStorageConfig.from_environment())

    # 1. HTTP-сессия
    http_session = setup_http_session(args.api_token)

    # 2. Камера + калибровка
    (
        camera_id,
        video_source_url,
        calibration_image_width,
        calibration_image_height,
        camera_matrix,
        distortion_coefficients,
        rectified_camera_matrix_opt,
        crop_x,
        crop_y,
        crop_width,
        crop_height,
        camera_confidence_threshold,
    ) = fetch_camera_and_calibration(http_session, base_api_url)

    print(f"Starting with camera {camera_id}")

    # Порог уверенности: из calib камеры если задан, иначе из CLI --conf.
    effective_confidence_threshold = (
        camera_confidence_threshold
        if camera_confidence_threshold is not None
        else args.conf
    )
    print(
        f"[cam {camera_id}] confidence threshold = {effective_confidence_threshold} "
        f"({'из calib' if camera_confidence_threshold is not None else 'из --conf'})",
        flush=True,
    )

    # 3. Три кадра с интервалом примерно 10 секунд из одного потока
    targets = [0.0, 10.0, 20.0]
    snapshot_captured_at = datetime.now(timezone.utc)
    frames_bgr = grab_frames_any(video_source_url, targets, headers=None)
    first_frame_bgr = frames_bgr[0]
    frame_height, frame_width = first_frame_bgr.shape[:2]

    # Для обычной камеры калибровка может отсутствовать. В локальном режиме
    # используем приближённую pinhole-матрицу и нулевую fisheye-дисторсию. Это
    # сохраняет зоны пригодными для тестовой обработки без обязательной
    # предварительной калибровки объектива.
    if camera_matrix is None or distortion_coefficients is None:
        focal_length = float(max(frame_width, frame_height))
        camera_matrix = np.array(
            [
                [focal_length, 0.0, frame_width / 2.0],
                [0.0, focal_length, frame_height / 2.0],
                [0.0, 0.0, 1.0],
            ],
            dtype=np.float64,
        )
        distortion_coefficients = np.zeros((4, 1), dtype=np.float64)
        calibration_image_width = frame_width
        calibration_image_height = frame_height
        rectified_camera_matrix_opt = camera_matrix.copy()

    # 3a. Обрезка кадров по параметрам из calib (если они заданы)
    use_crop = (
            crop_x is not None
            and crop_y is not None
            and crop_width is not None
            and crop_height is not None
    )

    if use_crop:
        detection_frames_bgr = [
            frame[crop_y:crop_y + crop_height, crop_x:crop_x + crop_width].copy()
            for frame in frames_bgr
        ]
    else:
        detection_frames_bgr = frames_bgr

    # 4. Подгонка матрицы камеры
    camera_matrix = adjust_camera_matrix_to_frame_size(
        camera_matrix,
        calibration_image_width,
        calibration_image_height,
        frame_width,
        frame_height,
    )

    # 5. rectified_camera_matrix
    if rectified_camera_matrix_opt is not None:
        rectified_camera_matrix = rectified_camera_matrix_opt
    else:
        rectified_camera_matrix = compute_fullview_rectified_camera_matrix(
            frame_width,
            frame_height,
            camera_matrix,
            distortion_coefficients
        )

    # 6. Зоны + изогнутые полигоны
    curved_zone_polygons, zone_identifiers = build_curved_zones_from_api(
        http_session,
        base_api_url,
        camera_id,
        camera_matrix,
        distortion_coefficients,
        rectified_camera_matrix,
        samples_per_edge=args.samples_per_edge,
    )
    zone_colors_bgr = vivid_palette(len(curved_zone_polygons))

    # 7. Инференс на трёх кадрах
    model_xml_path = Path(args.model).expanduser().resolve()

    all_boxes_full: List[np.ndarray] = []
    all_scores: List[np.ndarray] = []
    all_class_ids: List[np.ndarray] = []
    class_names = None

    for det_frame_bgr in detection_frames_bgr:
        (
            boxes,
            scores,
            class_ids,
            class_names_local,
            resize_ratio,
            padding_width,
            padding_height,
        ) = run_openvino_inference_on_frame(
            det_frame_bgr,
            model_xml_path=model_xml_path,
            device=args.device,
            img_size=args.imgsz,
            confidence_threshold=effective_confidence_threshold,
            car_only=args.car_only,
        )

        det_h, det_w = det_frame_bgr.shape[:2]
        boxes = restore_boxes_to_original_frame(
            boxes,
            resize_ratio=resize_ratio,
            padding_width=padding_width,
            padding_height=padding_height,
            frame_width=det_w,
            frame_height=det_h,
        )

        # Если кадр был обрезан по ROI, возвращаемся в координаты полного кадра
        if use_crop:
            boxes[:, [0, 2]] += crop_x
            boxes[:, [1, 3]] += crop_y

        all_boxes_full.append(boxes)
        all_scores.append(scores)
        all_class_ids.append(class_ids)

        if class_names is None:
            class_names = class_names_local

    # 7b. Агрегация: берём боксы, которые попали на 2 или 3 кадра
    (
        bounding_boxes_xyxy,
        detection_scores,
        detection_class_ids,
    ) = aggregate_detections_across_frames(
        all_boxes_full,
        all_scores,
        all_class_ids,
        iou_threshold=0.5,
        min_appearances=2,
    )

    # 9. Назначение машин зонам
    zone_statistics, car_assigned_zone_indices = assign_detections_to_zones(
        bounding_boxes_xyxy,
        detection_scores,
        detection_class_ids,
        curved_zone_polygons,
        zone_identifiers,
        frame_width,
        frame_height,
    )

    # 10. confidence по зонам
    compute_zone_confidences(zone_statistics)

    # 11. Визуализация
    visualization_frame_bgr = render_visualization_frame(
        first_frame_bgr,
        curved_zone_polygons,
        zone_statistics,
        car_assigned_zone_indices,
        bounding_boxes_xyxy,
        detection_scores,
        detection_class_ids,
        class_names,
        zone_colors_bgr,
        zone_outline_thickness=args.zone_thickness,
        car_alpha=args.car_alpha,
    )

    # 12. JSON-результат
    observed_at_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    result_payload = build_result_payload(
        camera_id,
        video_source_url,
        frame_width,
        frame_height,
        samples_per_edge=args.samples_per_edge,
        base_api_url=base_api_url,
        zone_statistics=zone_statistics,
        bounding_boxes_xyxy=bounding_boxes_xyxy,
        observed_at_iso=observed_at_iso,
    )

    # Оригинал, визуализация и YOLO-разметка шифруются в памяти и только после
    # этого загружаются в S3. Незашифрованные файлы на диск не пишутся.
    labels_yolo = encode_yolo_labels(
        bounding_boxes_xyxy,
        detection_class_ids,
        image_width=frame_width,
        image_height=frame_height,
    )
    stored_snapshots = snapshot_storage.store_pair(
        camera_id=camera_id,
        captured_at=snapshot_captured_at,
        raw_frame_bgr=first_frame_bgr,
        annotated_frame_bgr=visualization_frame_bgr,
        labels_yolo=labels_yolo,
    )
    result_payload["snapshots"] = {
        variant: snapshot.as_dict()
        for variant, snapshot in stored_snapshots.items()
    }

    # 13. Отправка в API: наблюдение в /occupancy/new + апдейт зоны в /zones/{id}
    push_zone_updates_to_api(
        http_session,
        base_api_url,
        zone_statistics,
        observed_at_iso=observed_at_iso,
        snapshots=result_payload["snapshots"],
        detected_cars_count=result_payload["totals"]["cars_detected"],
    )

    return result_payload, visualization_frame_bgr
