import cv2
import numpy as np


def vivid_palette(number_of_colors):
    """
    Возвращает список "ярких" цветов (BGR), циклически используя базовую палитру.
    """
    base_palette_bgr = [
        (0, 180, 255),
        (0, 200, 0),
        (255, 0, 0),
        (180, 0, 180),
        (0, 140, 255),
        (0, 255, 255),
        (255, 0, 180),
        (255, 255, 0),
        (0, 100, 255),
        (255, 100, 0),
    ]
    palette = [base_palette_bgr[index % len(base_palette_bgr)] for index in range(number_of_colors)]
    return palette


def draw_polygon_outline(frame_bgr, polygon_points, color_bgr, thickness=2):
    """
    Рисует контур полигона на изображении.
    """
    polygon_points_int32 = polygon_points.reshape((-1, 1, 2)).astype(np.int32)
    cv2.polylines(
        frame_bgr,
        [polygon_points_int32],
        isClosed=True,
        color=color_bgr,
        thickness=thickness
    )


def put_text_outline(frame_bgr, text, origin_xy, color_bgr, scale=0.6, thickness=1):
    """
    Рисует текст с чёрной обводкой (для лучшей читаемости).
    """
    cv2.putText(
        frame_bgr,
        text,
        origin_xy,
        cv2.FONT_HERSHEY_SIMPLEX,
        scale,
        (0, 0, 0),
        thickness + 2,
        cv2.LINE_AA
    )
    cv2.putText(
        frame_bgr,
        text,
        origin_xy,
        cv2.FONT_HERSHEY_SIMPLEX,
        scale,
        color_bgr,
        thickness,
        cv2.LINE_AA
    )


def draw_legend_bottom_left(
    frame_bgr,
    legend_entries,
    legend_colors_bgr,
    margin_pixels=10,
    padding_pixels=8,
    line_height_pixels=22,
    background_alpha=0.5
):
    """
    Рисует легенду (Zone N: occupied) в левой нижней части изображения.
    """
    image_height, image_width = frame_bgr.shape[:2]

    text_widths = []
    for entry_text in legend_entries:
        (text_width, _), _ = cv2.getTextSize(
            entry_text,
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            1
        )
        text_widths.append(text_width)

    if text_widths:
        legend_box_width = max(text_widths) + 2 * padding_pixels
    else:
        legend_box_width = 0

    legend_box_height = len(legend_entries) * line_height_pixels + 2 * padding_pixels

    if legend_box_width <= 0 or legend_box_height <= 0:
        return

    legend_box_x_min = margin_pixels
    legend_box_y_max = image_height - margin_pixels
    legend_box_y_min = legend_box_y_max - legend_box_height

    overlay_frame = frame_bgr.copy()
    cv2.rectangle(
        overlay_frame,
        (legend_box_x_min, legend_box_y_min),
        (legend_box_x_min + legend_box_width, legend_box_y_max),
        (0, 0, 0),
        -1
    )
    cv2.addWeighted(
        overlay_frame,
        background_alpha,
        frame_bgr,
        1 - background_alpha,
        0,
        dst=frame_bgr
    )

    text_y = legend_box_y_min + padding_pixels + int(line_height_pixels * 0.8)
    for index, entry_text in enumerate(legend_entries):
        text_x = legend_box_x_min + padding_pixels
        put_text_outline(
            frame_bgr,
            entry_text,
            (text_x, text_y),
            legend_colors_bgr[index],
            scale=0.6,
            thickness=1
        )
        text_y += line_height_pixels


def draw_box_with_alpha(
    frame_bgr,
    bounding_box_xyxy,
    label_text,
    edge_color_bgr,
    fill_color_bgr=None,
    alpha=0.25,
    thickness=2
):
    """
    Рисует прямоугольник по боксу с опциональной полупрозрачной заливкой и подписью.
    """
    x_min, y_min, x_max, y_max = [int(round(value)) for value in bounding_box_xyxy]

    x_min = max(0, x_min)
    y_min = max(0, y_min)
    x_max = min(frame_bgr.shape[1] - 1, x_max)
    y_max = min(frame_bgr.shape[0] - 1, y_max)

    if fill_color_bgr is not None and x_max > x_min and y_max > y_min:
        overlay_frame = frame_bgr.copy()
        cv2.rectangle(overlay_frame, (x_min, y_min), (x_max, y_max), fill_color_bgr, -1)
        cv2.addWeighted(overlay_frame, alpha, frame_bgr, 1 - alpha, 0, dst=frame_bgr)

    cv2.rectangle(frame_bgr, (x_min, y_min), (x_max, y_max), edge_color_bgr, thickness)

    if label_text:
        (text_width, text_height), _ = cv2.getTextSize(
            label_text,
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            2
        )
        text_position = (x_min + 3, y_min - 4)

        # Чёрная "тень" (обводка) текста
        cv2.putText(
            frame_bgr,
            label_text,
            text_position,
            cv2.FONT_HERSHEY_SIMPLEX,
            0.4,
            (0, 0, 0),
            2,
            cv2.LINE_AA
        )
        # Цветной текст
        cv2.putText(
            frame_bgr,
            label_text,
            text_position,
            cv2.FONT_HERSHEY_SIMPLEX,
            0.4,
            edge_color_bgr,
            1,
            cv2.LINE_AA
        )
