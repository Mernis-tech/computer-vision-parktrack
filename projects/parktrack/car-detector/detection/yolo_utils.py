from pathlib import Path

import cv2
import numpy as np
import yaml


def load_class_names(model_directory: Path):
    """
    Загружает имена классов из файла metadata.yaml рядом с моделью.

    Аргументы:
        model_directory (Path): Путь к директории, в которой лежит модель
                                и файл metadata.yaml.

    Возвращает:
        list[str]: Список имён классов. Если не удалось прочитать,
                   возвращается ["car"] по умолчанию.
    """
    metadata_path = model_directory / "metadata.yaml"
    if metadata_path.exists():
        try:
            metadata_content = metadata_path.read_text(encoding="utf-8")
            metadata = yaml.safe_load(metadata_content)
            class_names = metadata.get("names") or metadata.get("classes") or None

            if isinstance(class_names, dict):
                # Преобразуем dict {0: "car", 1: "person", ...} в список по возрастанию ключей
                sorted_keys = sorted(class_names.keys(), key=lambda key: int(key))
                class_names = [class_names[key] for key in sorted_keys]

            if isinstance(class_names, list) and len(class_names) > 0:
                return class_names
        except Exception:
            pass
    return ["car"]


def letterbox(input_image, target_shape=640, border_color=(114, 114, 114)):
    """
    Масштабирует изображение под квадрат/прямоугольник target_shape без искажения пропорций,
    добавляя рамки нужного цвета.

    Аргументы:
        input_image (np.ndarray): Входное изображение (H, W, 3).
        target_shape (int | tuple[int, int]): Целевой размер. Если int — используется (size, size).
        border_color (tuple[int, int, int]): Цвет рамок (B, G, R).

    Возвращает:
        tuple:
            output_image (np.ndarray): Изображение после масштабирования и паддинга.
            resize_ratio (float): Коэффициент масштабирования по меньшей стороне.
            padding_offsets (tuple[float, float]): Отступы (dw, dh) по x и y.
    """
    original_height, original_width = input_image.shape[:2]

    if isinstance(target_shape, int):
        target_height, target_width = target_shape, target_shape
    else:
        target_height, target_width = target_shape

    resize_ratio = min(target_height / original_height, target_width / original_width)
    resized_width = int(round(original_width * resize_ratio))
    resized_height = int(round(original_height * resize_ratio))

    padding_width = target_width - resized_width
    padding_height = target_height - resized_height

    padding_width /= 2.0
    padding_height /= 2.0

    if (original_width, original_height) != (resized_width, resized_height):
        input_image = cv2.resize(
            input_image,
            (resized_width, resized_height),
            interpolation=cv2.INTER_LINEAR
        )

    top_padding = int(round(padding_height - 0.1))
    bottom_padding = int(round(padding_height + 0.1))
    left_padding = int(round(padding_width - 0.1))
    right_padding = int(round(padding_width + 0.1))

    output_image = cv2.copyMakeBorder(
        input_image,
        top_padding,
        bottom_padding,
        left_padding,
        right_padding,
        cv2.BORDER_CONSTANT,
        value=border_color
    )

    return output_image, resize_ratio, (padding_width, padding_height)


def xywh_to_xyxy(boxes_xywh):
    """
    Конвертирует боксы из формата (center_x, center_y, width, height)
    в формат (x_min, y_min, x_max, y_max).

    Аргументы:
        boxes_xywh (np.ndarray): Массив формы (N, 4) в формате (cx, cy, w, h).

    Возвращает:
        np.ndarray: Массив формы (N, 4) в формате (x1, y1, x2, y2).
    """
    boxes_xyxy = np.empty_like(boxes_xywh)
    boxes_xyxy[:, 0] = boxes_xywh[:, 0] - boxes_xywh[:, 2] / 2.0  # x_min
    boxes_xyxy[:, 1] = boxes_xywh[:, 1] - boxes_xywh[:, 3] / 2.0  # y_min
    boxes_xyxy[:, 2] = boxes_xywh[:, 0] + boxes_xywh[:, 2] / 2.0  # x_max
    boxes_xyxy[:, 3] = boxes_xywh[:, 1] + boxes_xywh[:, 3] / 2.0  # y_max
    return boxes_xyxy


def non_maximum_suppression(bounding_boxes_xyxy, scores, iou_threshold=0.5):
    """
    Реализует Non-Maximum Suppression (NMS).
    Убирает сильно пересекающиеся боксы, оставляя только наиболее уверенные.

    Аргументы:
        bounding_boxes_xyxy (np.ndarray): Массив формы (N, 4) с боксами (x1, y1, x2, y2).
        scores (np.ndarray): Вектор длины N с confidence-score каждой детекции.
        iou_threshold (float): Порог IoU, выше которого бокс считается дубликатом.

    Возвращает:
        list[int]: Список индексов боксов, которые следует оставить.
    """
    if bounding_boxes_xyxy is None or len(bounding_boxes_xyxy) == 0:
        return []

    bounding_boxes_xyxy = np.asarray(bounding_boxes_xyxy, dtype=np.float32)
    scores = np.asarray(scores, dtype=np.float32)

    x_min = bounding_boxes_xyxy[:, 0]
    y_min = bounding_boxes_xyxy[:, 1]
    x_max = bounding_boxes_xyxy[:, 2]
    y_max = bounding_boxes_xyxy[:, 3]

    width = np.maximum(0.0, x_max - x_min)
    height = np.maximum(0.0, y_max - y_min)
    areas = width * height

    sorted_indices = scores.argsort()[::-1]
    kept_indices = []

    while sorted_indices.size > 0:
        current_index = sorted_indices[0]
        kept_indices.append(int(current_index))

        if sorted_indices.size == 1:
            break

        remaining_indices = sorted_indices[1:]

        intersection_x_min = np.maximum(x_min[current_index], x_min[remaining_indices])
        intersection_y_min = np.maximum(y_min[current_index], y_min[remaining_indices])
        intersection_x_max = np.minimum(x_max[current_index], x_max[remaining_indices])
        intersection_y_max = np.minimum(y_max[current_index], y_max[remaining_indices])

        intersection_width = np.maximum(0.0, intersection_x_max - intersection_x_min)
        intersection_height = np.maximum(0.0, intersection_y_max - intersection_y_min)
        intersection_area = intersection_width * intersection_height

        union_area = areas[current_index] + areas[remaining_indices] - intersection_area
        iou_values = intersection_area / (union_area + 1e-9)

        remaining_after_nms = np.where(iou_values <= iou_threshold)[0]
        sorted_indices = remaining_indices[remaining_after_nms]

    return kept_indices


def parse_with_embedded_nms(output_tensors, confidence_threshold):
    """
    Пытается распарсить выход модели, если она уже сделала NMS внутри себя.
    Ожидается формат тензора (1, N, 6): [x1, y1, x2, y2, score, class_id].

    Аргументы:
        output_tensors (list[Tensor | np.ndarray]): Список выходных тензоров модели.
        confidence_threshold (float): Порог уверенности.

    Возвращает:
        tuple[np.ndarray, np.ndarray, np.ndarray] | None:
            (boxes_xyxy, scores, class_ids) либо None,
            если формат не похож на embedded-NMS.
    """
    for tensor in output_tensors:
        tensor_data = tensor.data if hasattr(tensor, "data") else np.asarray(tensor)
        tensor_array = np.array(tensor_data)

        if tensor_array.ndim == 3 and tensor_array.shape[-1] == 6:
            detections = tensor_array[0]
            if detections.size == 0:
                return np.empty((0, 4)), np.empty((0,)), np.empty((0,))

            boxes_xyxy = detections[:, :4]
            scores = detections[:, 4]
            class_ids = detections[:, 5]

            confidence_mask = scores >= confidence_threshold
            return (
                boxes_xyxy[confidence_mask],
                scores[confidence_mask],
                class_ids[confidence_mask],
            )

    return None


def parse_raw_yolo_outputs(
    output_tensors,
    confidence_threshold,
    car_only=False,
    car_class_id=0,
    nms_iou_threshold=0.5
):
    """
    Универсальный парсер для "сырых" выходов YOLO-подобных моделей без встроенного NMS.

    Аргументы:
        output_tensors (list[Tensor | np.ndarray]): Выходные тензоры модели.
        confidence_threshold (float): Порог уверенности.
        car_only (bool): Оставлять только детекции класса car.
        car_class_id (int): ID класса "car" в модели.
        nms_iou_threshold (float): Порог IoU для NMS.

    Возвращает:
        tuple[np.ndarray, np.ndarray, np.ndarray]:
            boxes_xyxy, scores, class_ids.
            Если детекций нет, возвращаются пустые массивы.
    """
    output_arrays = []
    for tensor in output_tensors:
        tensor_data = tensor.data if hasattr(tensor, "data") else np.asarray(tensor)
        output_arrays.append(np.array(tensor_data))

    largest_output = max(output_arrays, key=lambda array: array.size)

    # Приводим к виду (num_detections, 4 + num_classes)
    if largest_output.ndim == 3:
        if largest_output.shape[1] >= 5 and largest_output.shape[0] == 1:
            largest_output = np.transpose(largest_output[0], (1, 0))
        elif largest_output.shape[2] >= 5 and largest_output.shape[0] == 1:
            largest_output = largest_output[0]
        elif largest_output.shape[0] == 1 and largest_output.shape[1] == 1:
            largest_output = largest_output[0, 0]
    elif largest_output.ndim != 2:
        raise RuntimeError("Неизвестный формат выхода модели (ожидался 2D или [1, *]).")

    if largest_output.shape[1] < 5:
        raise RuntimeError("Выход модели не похож на YOLO-предсказания (столбцов < 5).")

    boxes_xywh = largest_output[:, :4]
    class_scores_matrix = largest_output[:, 4:]

    class_ids = np.argmax(class_scores_matrix, axis=1)
    scores = class_scores_matrix[np.arange(class_scores_matrix.shape[0]), class_ids]

    confidence_mask = scores >= confidence_threshold
    boxes_xywh = boxes_xywh[confidence_mask]
    scores = scores[confidence_mask]
    class_ids = class_ids[confidence_mask]

    if car_only:
        car_mask = (class_ids == car_class_id)
        boxes_xywh = boxes_xywh[car_mask]
        scores = scores[car_mask]
        class_ids = class_ids[car_mask]

    boxes_xyxy = xywh_to_xyxy(boxes_xywh)
    kept_indices = non_maximum_suppression(
        boxes_xyxy,
        scores,
        iou_threshold=nms_iou_threshold
    )

    if len(kept_indices) == 0:
        return np.empty((0, 4)), np.empty((0,)), np.empty((0,))

    kept_indices = np.array(kept_indices, dtype=int)
    return boxes_xyxy[kept_indices], scores[kept_indices], class_ids[kept_indices]
