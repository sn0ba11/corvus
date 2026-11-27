import hashlib
import os
import urllib.request
from pathlib import Path

OPENCV_ZOO_COMMIT = "47534e27c9851bb1128ccc0102f1145e27f23f98"
MEDIA_BASE = f"https://media.githubusercontent.com/media/opencv/opencv_zoo/{OPENCV_ZOO_COMMIT}"
RAW_BASE = f"https://raw.githubusercontent.com/opencv/opencv_zoo/{OPENCV_ZOO_COMMIT}"
DOWNLOAD_SPECS = {
    "face_detection_yunet_2023mar.onnx": (
        f"{MEDIA_BASE}/models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
        "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
    ),
    "face_recognition_sface_2021dec.onnx": (
        f"{MEDIA_BASE}/models/face_recognition_sface/face_recognition_sface_2021dec.onnx",
        "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79",
    ),
    "LICENSE-yunet.txt": (
        f"{RAW_BASE}/models/face_detection_yunet/LICENSE",
        "c83b8120c50ccbd4c4f96edf53141bdd566ebb8f8e9227e415326aa1b1aba958",
    ),
    "LICENSE-sface.txt": (
        f"{RAW_BASE}/models/face_recognition_sface/LICENSE",
        "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30",
    ),
}


def download_models(destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for filename, (url, expected_sha256) in DOWNLOAD_SPECS.items():
        target = destination / filename
        if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == expected_sha256:
            continue
        request = urllib.request.Request(url, headers={"User-Agent": "OSINT-Workbench-Build/1"})
        temporary = target.with_suffix(f"{target.suffix}.{os.getpid()}.tmp")
        digest = hashlib.sha256()
        with urllib.request.urlopen(request, timeout=120) as response, temporary.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                digest.update(chunk)
                output.write(chunk)
        if digest.hexdigest() != expected_sha256:
            temporary.unlink(missing_ok=True)
            raise RuntimeError(f"checksum mismatch for {filename}")
        temporary.replace(target)


if __name__ == "__main__":
    download_models(Path(os.environ.get("FACE_MODEL_DIR", "/app/models")))
