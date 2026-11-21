"""Benchmark QR detection/decoding without ever printing the QR payload.

Usage:
    python guet_qr_bench.py IMAGE [IMAGE ...]
    python guet_qr_bench.py --make-synthetic DIR [IMAGE ...]

With ``--make-synthetic DIR`` a set of synthetic "projector photo" fixtures is
written to DIR (small projected codes, clean code, low contrast, inverted,
multiple codes and a photo without any code). When no positional paths are
given, the freshly generated files are benchmarked immediately.

Reported columns:
    bytes  - file size
    size   - pixel dimensions
    detect - QR candidate quadrilaterals found by OpenCV Aruco ("-" if cv2 is
             not installed)
    aruco  - 1 when the Aruco stage alone (locate + rectify + ZXing) decoded a
             payload, else 0 ("-" if cv2 is not installed)
    decode - 1 when the full decoder chain returned exactly one payload, else 0
    decoder- which stage decoded the code (wxscan/zxing/detail/aruco) or the
             failure error token (no_qr/multiple_qr/timeout/...)
    ms     - end-to-end decode time

Only counts, timings and decoder names are printed; payloads are never logged.
"""

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from time import monotonic

from PIL import Image, ImageDraw, ImageFilter, ImageOps

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
TRONCLASS_TOKEN = "guet-qr-bench-token"


def _load_preview():
    """Import scripts/guet_qr_preview.py so bench and CLI share one pipeline."""
    path = Path(__file__).resolve().parent / "guet_qr_preview.py"
    spec = importlib.util.spec_from_file_location("guet_qr_preview_bench", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Synthetic projector-style fixtures
# ---------------------------------------------------------------------------

def _payload(course="11", rollcall="22", token=TRONCLASS_TOKEN):
    return json.dumps(
        {"courseId": course, "rollcallId": rollcall, "data": token},
        separators=(",", ":"),
    )


def _render_qr(text, box_size=4, border=2):
    import qrcode

    code = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=box_size,
        border=border,
    )
    code.add_data(text)
    code.make(fit=True)
    return code.make_image(fill_color="black", back_color="white").convert("RGB")


def _scene(size, base=(14, 16, 20)):
    """A dark "classroom photo" background with a soft projector-light blob."""
    image = Image.new("RGB", size, base)
    draw = ImageDraw.Draw(image)
    width, height = size
    for step in range(24, 0, -1):
        ratio = step / 24
        colour = tuple(int(channel + 10 * (1 - ratio)) for channel in base)
        draw.ellipse(
            (width * (0.55 - 0.28 * ratio), height * (0.15 - 0.12 * ratio),
             width * (0.55 + 0.28 * ratio), height * (0.85 + 0.12 * ratio)),
            fill=colour,
        )
    draw.line((0, height * 0.82, width, height * 0.86), fill=(40, 42, 48), width=3)
    return image


def _no_qr_scene(size):
    """Photo-like frame that contains no QR code at all."""
    image = Image.new("RGB", size, (32, 34, 38))
    draw = ImageDraw.Draw(image)
    width, height = size
    draw.rectangle((0, int(height * 0.7), width, height), fill=(48, 46, 42))
    draw.rectangle((int(width * 0.08), int(height * 0.12), int(width * 0.55),
                    int(height * 0.62)), fill=(56, 58, 64))
    draw.rectangle((int(width * 0.62), int(height * 0.18), int(width * 0.9),
                    int(height * 0.5)), fill=(44, 46, 52))
    for index in range(12):
        x = int(width * (0.1 + 0.07 * index))
        draw.line((x, int(height * 0.75), x + 8, int(height * 0.75)),
                  fill=(88, 84, 76), width=2)
    return image


def _quad(cx, cy, size, skew=0.03):
    """A slightly skewed quadrilateral, points ordered TL, TR, BR, BL."""
    half = size / 2.0
    return [
        (cx - half * (1 + skew), cy - half * (1 - skew)),
        (cx + half * (1 - skew), cy - half * (1 + skew)),
        (cx + half * (1 + skew), cy + half * (1 - skew)),
        (cx - half * (1 - skew), cy + half * (1 + skew)),
    ]


def _perspective_coeffs(destination, source):
    """Coefficients mapping output pixels to source pixels for PIL.transform."""
    import numpy as np

    rows = []
    for (x, y), (u, v) in zip(destination, source):
        rows.append([x, y, 1, 0, 0, 0, -u * x, -u * y])
        rows.append([0, 0, 0, x, y, 1, -v * x, -v * y])
    matrix = np.array(rows, dtype="float64")
    target = np.array(source, dtype="float64").reshape(8)
    return np.linalg.solve(matrix, target).tolist()


def _project(scene, qr_image, quad):
    """Warp qr_image into quad and paste it onto the background scene."""
    width, height = scene.size
    qw, qh = qr_image.size
    coeffs = _perspective_coeffs(
        quad, [(0.0, 0.0), (float(qw), 0.0), (float(qw), float(qh)), (0.0, float(qh))],
    )
    warped = qr_image.transform((width, height), Image.PERSPECTIVE, coeffs,
                                Image.BICUBIC)
    mask = Image.new("L", (width, height), 0)
    ImageDraw.Draw(mask).polygon([tuple(point) for point in quad], fill=255)
    scene.paste(warped, (0, 0), mask)
    return scene


def _low_contrast(image, background=(96, 96, 96), alpha=0.4):
    washed = Image.blend(image, Image.new("RGB", image.size, background), 1 - alpha)
    return ImageOps.autocontrast(washed, cutoff=0)


def make_synthetic(directory):
    """Write the synthetic fixture set and return the created paths."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    created = []

    def save(name, image):
        path = directory / name
        image.save(path)
        created.append(path)
        return path

    qr = _render_qr(_payload())

    # 1. Clean projection on a small screen (control case).
    scene = _scene((1280, 960))
    save("synthetic-clean.png",
         _project(scene, qr, _quad(640, 430, 150)))

    # 2. Far shot: code occupies roughly 6% of the image width.
    scene = _scene((1600, 1200))
    scene = _project(scene, _render_qr(_payload(), box_size=5), _quad(1180, 320, 96))
    save("synthetic-far-small.png",
         scene.filter(ImageFilter.GaussianBlur(radius=1.0)))

    # 3. Low contrast + blur (washed-out projector in a bright room).
    scene = _scene((1600, 1200))
    scene = _project(scene, _render_qr(_payload(), box_size=5),
                     _quad(1180, 320, 110))
    scene = _low_contrast(scene)
    save("synthetic-low-contrast.png",
         scene.filter(ImageFilter.GaussianBlur(radius=1.2)))

    # 4. Inverted code (light modules on a dark field).
    scene = _scene((1600, 1200))
    inverted = ImageOps.invert(_render_qr(_payload(), box_size=5))
    scene = _project(scene, inverted, _quad(1180, 320, 100))
    save("synthetic-inverted.png",
         scene.filter(ImageFilter.GaussianBlur(radius=1.0)))

    # 5. Two codes in one frame (exercises multiple_qr classification).
    scene = _scene((1600, 1200))
    scene = _project(scene, _render_qr(_payload("11", "22"), box_size=5),
                     _quad(420, 350, 120))
    scene = _project(scene, _render_qr(_payload("12", "23"), box_size=5),
                     _quad(1180, 780, 120))
    save("synthetic-multiple.png",
         scene.filter(ImageFilter.GaussianBlur(radius=0.8)))

    # 6. Photo without any QR code.
    save("synthetic-no-qr.png", _no_qr_scene((1600, 1200)))
    return created


# ---------------------------------------------------------------------------
# Benchmark
# ---------------------------------------------------------------------------

def _images(paths):
    for path in paths:
        path = Path(path)
        if path.is_dir():
            yield from sorted(
                item for item in path.rglob("*")
                if item.is_file() and item.suffix.lower() in IMAGE_SUFFIXES
            )
        elif path.is_file():
            yield path


def bench_one(module, path):
    """Return one benchmark row; never returns the payload text."""
    row = {"file": path.name, "bytes": path.stat().st_size, "size": "-",
           "detect": None, "aruco": None, "decode": 0, "decoder": "-", "ms": 0.0}
    detect_fn = getattr(module, "aruco_quads", None)
    aruco_fn = getattr(module, "aruco_pass", None)
    try:
        with Image.open(path) as image:
            image = image.convert("RGB")
            row["size"] = f"{image.width}x{image.height}"
            if detect_fn is not None:
                row["detect"] = len(detect_fn(image))
            if aruco_fn is not None:
                row["aruco"] = 1 if aruco_fn(image, monotonic() + 2.0) else 0
    except Exception:
        row["decoder"] = "unreadable"
    started = monotonic()
    try:
        text, decoder = module.decode(path)
        row["decode"] = 1 if text else 0
        row["decoder"] = decoder
    except getattr(module, "Failure", Exception) as failure:
        row["decoder"] = getattr(failure, "error", "internal")
    except Exception:
        row["decoder"] = "internal"
    row["ms"] = round((monotonic() - started) * 1000, 1)
    return row


def format_rows(rows):
    lines = ["file  bytes  size  detect  aruco  decode  decoder  ms"]
    for row in rows:
        detect = "-" if row["detect"] is None else str(row["detect"])
        aruco = "-" if row["aruco"] is None else str(row["aruco"])
        lines.append(
            f"{row['file']}  {row['bytes']}  {row['size']}  {detect}  {aruco}  "
            f"{row['decode']}  {row['decoder']}  {row['ms']}"
        )
    return lines


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="*", type=Path,
                        help="图片文件或目录（目录递归扫描 png/jpg/webp）")
    parser.add_argument("--make-synthetic", type=Path, metavar="DIR",
                        help="生成合成投影对照图到 DIR；未给图片时直接基准这些图")
    args = parser.parse_args(argv)

    if not args.paths and not args.make_synthetic:
        parser.error("需要图片/目录，或使用 --make-synthetic DIR")

    module = _load_preview()
    if args.make_synthetic:
        created = make_synthetic(args.make_synthetic)
        print(f"generated {len(created)} synthetic images in {args.make_synthetic}")
        if not args.paths:
            args.paths = [args.make_synthetic]

    rows = [bench_one(module, path) for path in _images(args.paths)]
    if not rows:
        parser.error("no images found")
    for line in format_rows(rows):
        print(line)
    decoded = sum(row["decode"] for row in rows)
    print(f"total {len(rows)} images, decoded {decoded}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
