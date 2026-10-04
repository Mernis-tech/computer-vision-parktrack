import numpy as np
import cv2


def undistort_pixels_to_rectified(
    distorted_pixel_points,
    camera_matrix,
    distortion_coefficients,
    rectified_camera_matrix
):
    """
    Переводит точки из координат искажённого (fisheye) изображения
    в координаты "выпрямленного" пространства.
    """
    distorted_pixel_points = np.asarray(distorted_pixel_points, dtype=np.float64)
    distorted_pixel_points = distorted_pixel_points.reshape(-1, 1, 2)

    rectified_points = cv2.fisheye.undistortPoints(
        distorted_pixel_points,
        camera_matrix,
        distortion_coefficients,
        P=rectified_camera_matrix
    ).reshape(-1, 2)

    return rectified_points


def rectified_to_distorted_pixels(
    rectified_points,
    camera_matrix,
    distortion_coefficients,
    rectified_camera_matrix
):
    """
    Переводит точки из координат выпрямленного пространства обратно
    в пиксели искажённого (fisheye) изображения.
    """
    rectified_points = np.asarray(rectified_points, dtype=np.float64).reshape(-1, 2)
    ones_column = np.ones((rectified_points.shape[0], 1), dtype=np.float64)

    homogeneous_points = np.hstack([rectified_points, ones_column])
    inverse_rectified_camera_matrix = np.linalg.inv(rectified_camera_matrix)

    normalized_points = (inverse_rectified_camera_matrix @ homogeneous_points.T).T[:, :2]
    normalized_points = normalized_points.reshape(-1, 1, 2)

    distorted_points = cv2.fisheye.distortPoints(
        normalized_points,
        camera_matrix,
        distortion_coefficients
    ).reshape(-1, 2)

    return distorted_points


def densify_edge(start_point, end_point, number_of_samples):
    """
    Разбивает прямой отрезок между двумя точками на множество промежуточных точек.
    """
    interpolation_parameters = np.linspace(0.0, 1.0, number_of_samples, dtype=np.float64)
    interpolation_parameters = interpolation_parameters.reshape(-1, 1)

    start_point = np.asarray(start_point, dtype=np.float64)
    end_point = np.asarray(end_point, dtype=np.float64)

    sampled_points = (1.0 - interpolation_parameters) * start_point + interpolation_parameters * end_point
    return sampled_points


def build_curved_polygon_from_anchors(
    distorted_anchors_pixels,
    camera_matrix,
    distortion_coefficients,
    rectified_camera_matrix,
    samples_per_edge=50
):
    """
    Строит "изогнутый" полигон зоны по якорным точкам на искажённом кадре.
    """
    distorted_anchors_pixels = np.asarray(distorted_anchors_pixels, dtype=np.float64)
    rectified_anchor_points = undistort_pixels_to_rectified(
        distorted_anchors_pixels,
        camera_matrix,
        distortion_coefficients,
        rectified_camera_matrix
    )

    curved_polygon_points = []

    number_of_vertices = len(rectified_anchor_points)
    for vertex_index in range(number_of_vertices):
        current_point = rectified_anchor_points[vertex_index]
        next_point = rectified_anchor_points[(vertex_index + 1) % number_of_vertices]

        rectified_segment_points = densify_edge(
            current_point,
            next_point,
            number_of_samples=samples_per_edge
        )
        distorted_segment_points = rectified_to_distorted_pixels(
            rectified_segment_points,
            camera_matrix,
            distortion_coefficients,
            rectified_camera_matrix
        )
        curved_polygon_points.append(distorted_segment_points)

    polygon_points = np.vstack(curved_polygon_points).astype(np.float32)
    return polygon_points


def signed_depth_to_polygon(point_xy, polygon_points_float32):
    """
    Вычисляет "глубину" точки внутри полигона как расстояние (в пикселях)
    до границы полигона. Вне полигона глубина считается 0.
    """
    contour = polygon_points_float32.reshape((-1, 1, 2)).astype(np.float32)
    distance_to_border = cv2.pointPolygonTest(
        contour,
        (float(point_xy[0]), float(point_xy[1])),
        True
    )
    return max(0.0, float(distance_to_border))


def overlap_ratio_box_in_polygon(
    bounding_box_xyxy,
    polygon_points_float32,
    image_height,
    image_width
):
    """
    Вычисляет долю площади прямоугольного бокса, попавшую внутрь полигона (от 0 до 1).
    """
    x_min, y_min, x_max, y_max = bounding_box_xyxy

    roi_x_min = int(np.floor(max(0, min(x_min, x_max))))
    roi_x_max = int(np.ceil(min(image_width - 1, max(x_min, x_max))))
    roi_y_min = int(np.floor(max(0, min(y_min, y_max))))
    roi_y_max = int(np.ceil(min(image_height - 1, max(y_min, y_max))))

    if roi_x_max <= roi_x_min or roi_y_max <= roi_y_min:
        return 0.0

    roi_width = roi_x_max - roi_x_min + 1
    roi_height = roi_y_max - roi_y_min + 1

    polygon_points_roi = polygon_points_float32.copy()
    polygon_points_roi[:, 0] -= roi_x_min
    polygon_points_roi[:, 1] -= roi_y_min

    polygon_mask = np.zeros((roi_height, roi_width), dtype=np.uint8)
    cv2.fillPoly(
        polygon_mask,
        [polygon_points_roi.astype(np.int32)],
        255
    )

    bounding_box_mask = np.zeros((roi_height, roi_width), dtype=np.uint8)
    top_left_x = int(round(x_min - roi_x_min))
    top_left_y = int(round(y_min - roi_y_min))
    bottom_right_x = int(round(x_max - roi_x_min))
    bottom_right_y = int(round(y_max - roi_y_min))

    cv2.rectangle(
        bounding_box_mask,
        (top_left_x, top_left_y),
        (bottom_right_x, bottom_right_y),
        255,
        -1
    )

    intersection_mask = cv2.bitwise_and(polygon_mask, bounding_box_mask)
    intersection_area = cv2.countNonZero(intersection_mask)
    bounding_box_area = cv2.countNonZero(bounding_box_mask)

    if bounding_box_area <= 0:
        return 0.0

    overlap_ratio = float(intersection_area) / float(bounding_box_area)
    return overlap_ratio


def normalize_depth_for_box(depth_pixels, bounding_box_xyxy):
    """
    Нормирует глубину точки относительно диагонали бокса,
    чтобы значения были сопоставимы с overlap.
    """
    x_min, y_min, x_max, y_max = bounding_box_xyxy

    box_width = max(1.0, float(x_max - x_min))
    box_height = max(1.0, float(y_max - y_min))
    box_diagonal = (box_width * box_width + box_height * box_height) ** 0.5

    return float(depth_pixels) / box_diagonal
