# Bundled YuNet face detector

`face_detection_yunet_2023mar.onnx` comes from OpenCV Zoo at commit `47534e27c9851bb1128ccc0102f1145e27f23f98`.

- Source: https://github.com/opencv/opencv_zoo/blob/47534e27c9851bb1128ccc0102f1145e27f23f98/models/face_detection_yunet/face_detection_yunet_2023mar.onnx
- SHA-256: `8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4` (verified against the upstream Git LFS object ID)
- Size: 232589 bytes
- License: MIT; see LICENSE alongside the model.

This 2023mar model is compatible with Frisket's OpenCV 4.x dependency. The newer 2026may export targets OpenCV 5's ONNX Runtime engine. The model is bundled so face detection needs no network access or runtime download.
