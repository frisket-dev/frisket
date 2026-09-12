"""One-shot YuNet face extraction worker."""

from __future__ import annotations

import json
import math
import sys


def main() -> None:
    payload = json.load(sys.stdin)
    try:
        import cv2
    except ImportError:
        print(json.dumps({"error": "opencv not installed"}))
        return
    # Rows already run concurrently; avoid per-worker native pools exhausting
    # the sandbox address-space limit.
    cv2.setNumThreads(1)
    image = cv2.imread(payload["path"])
    if image is None:
        print(json.dumps({"error": "unreadable image"}))
        return
    height, width = image.shape[:2]
    detector = cv2.FaceDetectorYN.create(
        payload["model_path"],
        "",
        (320, 320),
        0.9,
        0.3,
        5000,
        cv2.dnn.DNN_BACKEND_OPENCV,
        cv2.dnn.DNN_TARGET_CPU,
    )
    detector.setInputSize((width, height))
    _, found = detector.detect(image)
    faces = []
    for found_face in found if found is not None else ():
        x, y, face_width, face_height = (float(value) for value in found_face[:4])
        if (
            not all(math.isfinite(value) for value in (x, y, face_width, face_height))
            or face_width <= 0
            or face_height <= 0
        ):
            continue
        x0 = max(0, min(width - 1, round(x)))
        y0 = max(0, min(height - 1, round(y)))
        x1 = max(x0 + 1, min(width, round(x + face_width)))
        y1 = max(y0 + 1, min(height, round(y + face_height)))
        face_width, face_height = x1 - x0, y1 - y0
        pad = int(0.15 * face_width)
        crop_x0, crop_y0 = max(0, x0 - pad), max(0, y0 - pad)
        crop_x1, crop_y1 = min(width, x1 + pad), min(height, y1 + pad)
        crop_path = payload["out_dir"] + f"/face{len(faces)}.jpg"
        if not cv2.imwrite(crop_path, image[crop_y0:crop_y1, crop_x0:crop_x1]):
            print(json.dumps({"error": "could not write face crop"}))
            return
        faces.append(
            {"x": x0, "y": y0, "w": face_width, "h": face_height, "crop": crop_path}
        )
    print(json.dumps({"faces": faces}))


if __name__ == "__main__":
    main()
