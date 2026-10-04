import numpy as np
import cv2


def load_calibration_from_dict(calibration_data):
    if not calibration_data:
        return None

    required_fields = ["image_width", "image_height", "K", "D"]

    if any(field not in calibration_data for field in required_fields):
        print(
            "[WARN] incomplete calibration data, undistortion will be skipped. "
            f"Received keys: {list(calibration_data.keys())}",
            flush=True,
        )
        return None

    image_width = int(calibration_data["image_width"])
    image_height = int(calibration_data["image_height"])

    camera_matrix = np.array(calibration_data["K"], dtype=np.float64).reshape(3, 3)
    distortion_coefficients = np.array(calibration_data["D"], dtype=np.float64).reshape(-1, 1)

    rectified_camera_matrix = None
    if calibration_data.get("newK") is not None:
        rectified_camera_matrix = np.array(calibration_data["newK"], dtype=np.float64).reshape(3, 3)

    balance = float(calibration_data.get("balance", 0.0))

    return (
        image_width,
        image_height,
        camera_matrix,
        distortion_coefficients,
        rectified_camera_matrix,
        balance,
    )

def compute_fullview_rectified_camera_matrix(
    image_width,
    image_height,
    camera_matrix,
    distortion_coefficients
):
    """
    Вычисляет "новую" матрицу камеры (rectified_camera_matrix), которая старается
    уместить максимум поля зрения после undistort (fisheye).
    """
    sampling_step = max(8, int(min(image_width, image_height) / 160))

    x_grid = np.arange(0, image_width, sampling_step, dtype=np.float64)
    y_grid = np.arange(0, image_height, sampling_step, dtype=np.float64)

    border_points = []
    for x_coordinate in x_grid:
        border_points += [[x_coordinate, 0.0], [x_coordinate, image_height - 1.0]]
    for y_coordinate in y_grid:
        border_points += [[0.0, y_coordinate], [image_width - 1.0, y_coordinate]]

    border_points = np.array(border_points, dtype=np.float64)

    undistorted_points = cv2.fisheye.undistortPoints(
        border_points.reshape(-1, 1, 2),
        camera_matrix,
        distortion_coefficients,
        P=None
    ).reshape(-1, 2)

    min_x = undistorted_points[:, 0].min()
    min_y = undistorted_points[:, 1].min()
    max_x = undistorted_points[:, 0].max()
    max_y = undistorted_points[:, 1].max()

    span_x = max(max_x - min_x, 1e-6)
    span_y = max(max_y - min_y, 1e-6)

    scale = min((image_width - 1) / span_x, (image_height - 1) / span_y)
    principal_point_x = -min_x * scale
    principal_point_y = -min_y * scale

    rectified_camera_matrix = np.array(
        [
            [scale, 0.0, principal_point_x],
            [0.0, scale, principal_point_y],
            [0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )
    return rectified_camera_matrix
