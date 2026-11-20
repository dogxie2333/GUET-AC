"""Decode a locally supplied image without opening the QR's target URL."""

import argparse
import json
import struct
import subprocess
from pathlib import Path
from time import monotonic

import zxingcpp
from PIL import Image, ImageOps


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
            timeout=2.5,
            check=True,
        )
        results = json.loads(completed.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    if not isinstance(results, list) or not all(isinstance(item, str) for item in results):
        return []
    return results


def zxing(image):
    return [
        item.text for item in zxingcpp.read_barcodes(
            image, formats=zxingcpp.BarcodeFormat.QRCode,
            try_invert=True,
        )
    ]


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
    results = set()
    for box in dict.fromkeys(boxes):
        if monotonic() >= deadline:
            break
        region = ImageOps.autocontrast(gray.crop(box), cutoff=0.3)
        scale = min(2.5, 1600 / max(region.size))
        if scale > 1:
            region = region.resize(tuple(max(1, round(n * scale)) for n in region.size),
                                   Image.Resampling.LANCZOS)
        results.update(zxing(region))
        if len(results) > 1:
            break
    return list(results)


def decode(path):
    deadline = monotonic() + 4.2
    if path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError("图片超过 8 MiB。")
    with Image.open(path, formats=("PNG", "JPEG", "WEBP")) as image:
        if image.width * image.height > 16_000_000:
            raise ValueError("图片像素超过限制。")
        image = ImageOps.exif_transpose(image)
        results = wxscan(image, Path(__file__).resolve().parent / "wxscan")
        if not results:
            results = zxing(image.convert("L"))
        if not results:
            results = detail_pass(image, deadline)
    if len(results) != 1:
        raise ValueError("图片中应包含且仅包含一个二维码。")
    return results[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    args = parser.parse_args()
    try:
        decode(args.image)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"识别失败：{exc}\n")
    print("已识别一个二维码。")


if __name__ == "__main__":
    main()
