import sys
from pathlib import Path
import json
import traceback
import cv2

from cli import parse_arguments
from pipeline import run_single_frame_pipeline


def main():
    """
    Точка входа: парсит аргументы, запускает пайплайн, выводит JSON
    и, при необходимости, показывает кадр.
    """
    args = parse_arguments()

    try:
        result_payload, visualization_frame_bgr = run_single_frame_pipeline(args)
    except Exception as exception:
        print(f"[ERR] pipeline failed: {type(exception).__name__}: {exception}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)

    # Печатаем JSON в stdout
    print(json.dumps(result_payload, ensure_ascii=False))

    # Показ окна (если нужно)
    if args.show:
        cv2.imshow(
            "Curved zones on distorted image (single frame, API)",
            visualization_frame_bgr
        )
        cv2.waitKey(1500)
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
