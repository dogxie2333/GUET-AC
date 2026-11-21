"""Decode a locally supplied image without opening the QR's target URL."""

import argparse
import json
import os
import shutil
import struct
import subprocess
import sys
import uuid
from pathlib import Path
from time import monotonic

import zxingcpp
from PIL import Image, ImageOps

MAX_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 16_000_000
DECODE_BUDGET = 4.2
WXSCAN_TIMEOUT = 2.5
IMAGE_FORMATS = ("PNG", "JPEG", "WEBP")

# Fixed error vocabulary shared with the QQ bridge (docs/interfaces.md §2).
ERRORS = frozenset({
    "unreadable", "too_large", "pixels_too_large", "no_qr", "multiple_qr",
    "invalid_payload", "timeout", "internal",
})

# Stages that failed to produce a text are reported as image/decode failures.
_FAILURE_MESSAGES = {
    "unreadable": "图片无法读取。",
    "too_large": "图片超过 8 MiB。",
    "pixels_too_large": "图片像素超过限制。",
    "no_qr": "图片中应包含且仅包含一个二维码。",
    "multiple_qr": "图片中应包含且仅包含一个二维码。",
    "timeout": "识别超时。",
    "internal": "识别内部错误。",
}


class Failure(Exception):
    """A classified failure: stage=image|decode|parse, error from ERRORS."""

    def __init__(self, stage, error, message):
        super().__init__(message)
        self.stage = stage
        self.error = error
        self.message = message


def _unique(texts):
    found = []
    for text in texts:
        if isinstance(text, str) and text and text not in found:
            found.append(text)
    return found


def wxscan(image, directory):
    node = Path.home() / ".local/node24/bin/node"
    entry = directory / "guet_wxscan.mjs"
    if not (node.is_file() and entry.is_file()):
        return []
    gray = image.convert("L")
    frame = struct.pack("<II", *gray.size) + gray.tobytes()
    try:
        completed = subprocess.run(
            [node, entry, directory],
            input=frame,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=WXSCAN_TIMEOUT,
            check=True,
        )
        results = json.loads(completed.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    if not isinstance(results, list) or not all(isinstance(item, str) for item in results):
        return []
    return _unique(results)


def _result_text(item):
    """Return the QR payload text, preserving control bytes.

    zxing-cpp escapes non-printable characters in ``item.text`` (e.g. ``\\x10``
    becomes ``<DLE>``), which breaks TronClass compact codes. The raw payload
    bytes are exact, so decode them as UTF-8 and only fall back to the display
    text for legacy bindings without ``bytes`` or non-UTF-8 content.
    """
    try:
        raw = item.bytes
    except AttributeError:
        return item.text
    if raw:
        try:
            return bytes(raw).decode("utf-8")
        except (UnicodeDecodeError, TypeError, ValueError):
            pass
    return item.text


def _read_qr(image):
    """Core ZXing decode used by the zxing stage and by the Aruco fallback."""
    return _unique(
        _result_text(item) for item in zxingcpp.read_barcodes(
            image, formats=zxingcpp.BarcodeFormat.QRCode,
            try_invert=True,
        )
    )


def zxing(image):
    return _read_qr(image)


def detail_pass(image, deadline):
    """Try overlapping local regions so small codes retain their detail."""
    gray = image.convert("L")
    width, height = gray.size
    boxes = [(0, 0, width, height)]
    if max(width, height) >= 320:
        tile_w = max(1, (width * 2 + 2) // 3)
        tile_h = max(1, (height * 2 + 2) // 3)
        for top in (0, height - tile_h):
            for left in (0, width - tile_w):
                boxes.append((left, top, left + tile_w, top + tile_h))
        boxes.insert(1, (width // 4, height // 4, width * 3 // 4, height * 3 // 4))
    results = []
    for box in dict.fromkeys(boxes):
        if monotonic() >= deadline:
            break
        region = ImageOps.autocontrast(gray.crop(box), cutoff=0.3)
        scale = min(2.5, 1600 / max(region.size))
        if scale > 1:
            region = region.resize(tuple(max(1, round(n * scale)) for n in region.size),
                                   Image.Resampling.LANCZOS)
        results.extend(zxing(region))
        if len(_unique(results)) > 1:
            break
    return _unique(results)


def _cv2():
    try:
        import cv2
    except Exception:
        return None
    if not hasattr(cv2, "QRCodeDetectorAruco"):
        return None
    return cv2


def _order_quad(points):
    """Return a 4x2 float32 quad ordered TL, TR, BR, BL."""
    import numpy as np

    pts = np.asarray(points, dtype="float32").reshape(-1, 2)[:4]
    if pts.shape[0] != 4:
        return None
    total = pts.sum(axis=1)
    diff = np.diff(pts, axis=1).reshape(-1)
    return np.array(
        [pts[int(np.argmin(total))], pts[int(np.argmin(diff))],
         pts[int(np.argmax(total))], pts[int(np.argmax(diff))]],
        dtype="float32",
    )


def aruco_quads(image):
    """Locate QR candidates with OpenCV's Aruco-based detector.

    Returns a list of ordered quads, or [] when cv2 is unavailable or fails.
    """
    cv2 = _cv2()
    if cv2 is None:
        return []
    try:
        import numpy as np

        source = np.asarray(image.convert("L"))
        detector = cv2.QRCodeDetectorAruco()
        result = detector.detect(source)
        points = result[1] if isinstance(result, tuple) and len(result) >= 2 else result
        if points is None:
            return []
        quads = []
        for group in np.asarray(points, dtype="float32").reshape(-1, 4, 2):
            quad = _order_quad(group)
            if quad is not None:
                quads.append(quad)
        return quads
    except Exception:
        return []


def _warp_quad(cv2, np, source, quad, deadline):
    """Perspective-rectify one quad to a square and decode it with ZXing."""
    edges = [
        float(np.linalg.norm(quad[(index + 1) % 4] - quad[index]))
        for index in range(4)
    ]
    side = max(edges)
    if side < 1:
        return []
    target = int(round(side * 4)) if side < 240 else int(round(side))
    target = max(240, min(1600, target))
    destination = np.array(
        [[0, 0], [target - 1, 0], [target - 1, target - 1], [0, target - 1]],
        dtype="float32",
    )
    try:
        matrix = cv2.getPerspectiveTransform(quad, destination)
        warped = cv2.warpPerspective(
            source, matrix, (target, target),
            flags=cv2.INTER_LINEAR, borderValue=255,
        )
    except Exception:
        return []
    margin = max(8, target // 8)
    padded = cv2.copyMakeBorder(
        warped, margin, margin, margin, margin, cv2.BORDER_CONSTANT, value=255,
    )
    if monotonic() >= deadline:
        return []
    return _read_qr(Image.fromarray(padded))


def _scoped_aruco(image_path, deadline):
    """Run the Aruco fallback through systemd-run --user --scope.

    The QQ bridge service has a small MemoryMax; importing OpenCV inside its
    cgroup can trigger the OOM killer. A user scope keeps the heavy import in
    the user slice. Any failure returns [] instead of falling back in-process.
    """
    if image_path is None:
        return []
    runner = shutil.which("systemd-run")
    worker = Path(__file__).resolve().with_name("guet_aruco_worker.py")
    if not (runner and worker.is_file()):
        return []
    remaining = max(0.5, min(5.0, deadline - monotonic() + 0.5))
    unit = f"guet-aruco-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    command = [
        runner, "--user", "--scope", "--quiet", f"--unit={unit}",
        sys.executable, str(worker), str(image_path), f"{remaining:.2f}",
    ]
    try:
        completed = subprocess.run(
            command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=remaining + 2, check=False,
        )
        payload = json.loads(completed.stdout.decode("utf-8", "replace"))
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        return []
    texts = payload.get("texts")
    if not isinstance(texts, list) or not all(isinstance(item, str) for item in texts):
        return []
    return _unique(texts)


def aruco_pass(image, deadline, image_path=None, use_scope=None):
    """Aruco fallback: locate quads, rectify, then decode with ZXing.

    Always best-effort: missing cv2, unexpected errors or an exhausted deadline
    return [] instead of breaking the decode chain. On the QQ bridge the
    fallback runs in a separate user scope to avoid the service memory limit.
    """
    if monotonic() >= deadline:
        return []
    if use_scope is None:
        use_scope = os.environ.get("GUET_AC_ARUCO_SCOPE") == "1" and image_path is not None
    if use_scope:
        return _scoped_aruco(image_path, deadline)
    try:
        quads = aruco_quads(image)
        if not quads:
            return []
        cv2 = _cv2()
        if cv2 is None:
            return []
        import numpy as np

        source = np.asarray(image.convert("L"))
        results = []
        for quad in quads:
            if monotonic() >= deadline:
                break
            results.extend(_warp_quad(cv2, np, source, quad, deadline))
            if len(_unique(results)) > 1:
                break
        return _unique(results)
    except Exception:
        return []


def _load(path):
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise Failure("image", "unreadable", _FAILURE_MESSAGES["unreadable"]) from exc
    if size > MAX_BYTES:
        raise Failure("image", "too_large", _FAILURE_MESSAGES["too_large"])
    try:
        with Image.open(path, formats=IMAGE_FORMATS) as image:
            if image.width * image.height > MAX_PIXELS:
                raise Failure("image", "pixels_too_large",
                              _FAILURE_MESSAGES["pixels_too_large"])
            image = ImageOps.exif_transpose(image)
            image.load()
            return image.copy()
    except Failure:
        raise
    except Exception as exc:
        raise Failure("image", "unreadable", _FAILURE_MESSAGES["unreadable"]) from exc


def decode(path, budget=DECODE_BUDGET, accept=None):
    """Return (text, decoder) for the single QR code in *path*.

    When *accept* is given, a candidate text must pass it before being returned;
    this lets a later decoder rescue a payload that an earlier decoder returned
    with display-escaped control bytes (e.g. ``<DLE>`` instead of ``\\x10``).

    Raises Failure on image/decode errors; never logs or returns the payload in
    the error message.
    """
    image = _load(path)
    deadline = monotonic() + budget
    directory = Path(__file__).resolve().parent
    stages = (
        ("wxscan", lambda: wxscan(image, directory / "wxscan")),
        ("zxing", lambda: zxing(image.convert("L"))),
        ("detail", lambda: detail_pass(image, deadline)),
        ("aruco", lambda: aruco_pass(image, deadline, image_path=path)),
    )
    rejected = None
    for name, run in stages:
        if monotonic() >= deadline:
            break
        try:
            results = _unique(run() or [])
        except Exception:
            results = []
        if len(results) > 1:
            raise Failure("decode", "multiple_qr", _FAILURE_MESSAGES["multiple_qr"])
        if len(results) == 1:
            if accept is None:
                return results[0], name
            try:
                accept(results[0])
                return results[0], name
            except Failure as failure:
                rejected = failure
                continue
    if rejected is not None:
        raise rejected
    if monotonic() >= deadline:
        raise Failure("decode", "timeout", _FAILURE_MESSAGES["timeout"])
    raise Failure("decode", "no_qr", _FAILURE_MESSAGES["no_qr"])


def parse_payload(text):
    """Parse a decoded QR text with the installed guet-ac package."""
    try:
        from guet_ac import qr
    except Exception as exc:
        raise Failure("parse", "internal", "无法加载畅课二维码解析模块。") from exc
    try:
        return qr.parse(text)
    except Exception as exc:
        message = str(exc) or "二维码内容不是有效的畅课签到码。"
        raise Failure("parse", "invalid_payload", message) from exc


def _json(payload):
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _emit_failure(failure, parser, as_json):
    if as_json:
        print(_json({
            "ok": False,
            "stage": failure.stage,
            "error": failure.error,
            "message": failure.message,
        }))
        return 2
    parser.exit(1, f"识别失败：{failure.message}\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true",
                        help="stdout 输出单行 JSON，失败时退出码 2")
    parser.add_argument("image", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.json:
            parsed = {}

            def accept(text):
                parsed["payload"] = parse_payload(text)

            _text, decoder = decode(args.image, accept=accept)
            print(_json({"ok": True, "decoder": decoder, "payload": parsed["payload"]}))
        else:
            decode(args.image)
            print("已识别一个二维码。")
    except Failure as failure:
        return _emit_failure(failure, parser, args.json)
    except Exception:
        return _emit_failure(
            Failure("decode", "internal", _FAILURE_MESSAGES["internal"]),
            parser, args.json,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
