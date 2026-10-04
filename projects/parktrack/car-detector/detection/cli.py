import argparse


def build_argument_parser() -> argparse.ArgumentParser:
    """
    Создаёт и настраивает парсер аргументов командной строки
    для приложения детекции машин и зон.

    Возвращает:
        argparse.ArgumentParser: Готовый парсер.
    """
    argument_parser = argparse.ArgumentParser(
        description="YOLO + Curved parking zones on distorted image (one frame, API-based)"
    )

    argument_parser.add_argument(
        "--model",
        required=True,
        help="Путь к OpenVINO model.xml"
    )
    argument_parser.add_argument(
        "--base-api-url",
        required=True,
        help="BASE_API_URL, напр. http://localhost:8080"
    )
    argument_parser.add_argument(
        "--imgsz",
        type=int,
        default=640,
        help="Размер входного изображения для модели (одна сторона)"
    )
    argument_parser.add_argument(
        "--device",
        type=str,
        default="AUTO",
        help="Устройство OpenVINO (AUTO, CPU, GPU, ...)"
    )
    argument_parser.add_argument(
        "--conf",
        type=float,
        default=0.15,
        help=(
            "Порог уверенности детектирования объектов. "
            "Используется как fallback, если у камеры в calib не задано "
            "поле confidence (calib.confidence имеет приоритет)."
        )
    )
    argument_parser.add_argument(
        "--car_only",
        action="store_true",
        help="Оставлять только детекции класса 'car'"
    )
    argument_parser.add_argument(
        "--samples-per-edge",
        type=int,
        default=60,
        help="Плотность дискретизации сторон зон при построении изогнутого полигона"
    )
    argument_parser.add_argument(
        "--zone_thickness",
        type=int,
        default=2,
        help="Толщина линий при отрисовке контуров зон"
    )
    argument_parser.add_argument(
        "--car_alpha",
        type=float,
        default=0.25,
        help="Прозрачность заливки прямоугольников машин"
    )
    argument_parser.add_argument(
        "--show",
        action="store_true",
        help="Показать окно с визуализацией кадра"
    )
    argument_parser.add_argument(
        "--assign",
        choices=["center", "depth", "hybrid"],
        default="hybrid",
        help="(Исторический параметр: сейчас используется центр+overlap)"
    )
    argument_parser.add_argument(
        "--w_center",
        type=float,
        default=1.0,
        help="(Исторический параметр для hybrid; сейчас не используется)"
    )
    argument_parser.add_argument(
        "--w_overlap",
        type=float,
        default=2.0,
        help="(Исторический параметр для hybrid; сейчас не используется)"
    )
    argument_parser.add_argument(
        "--api-token",
        required=True,
        help="Bearer token для авторизации в API"
    )

    return argument_parser


def parse_arguments():
    """
    Обёртка вокруг build_argument_parser, сразу парсит аргументы.

    Возвращает:
        argparse.Namespace: Объект с полями, соответствующими аргументам CLI.
    """
    parser = build_argument_parser()
    return parser.parse_args()
