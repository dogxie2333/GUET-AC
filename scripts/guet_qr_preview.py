"""Decode a locally supplied image without opening the QR's target URL."""

import argparse
from pathlib import Path

import zxingcpp
from PIL import Image


def decode(path):
    if path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError("图片超过 8 MiB。")
    with Image.open(path, formats=("PNG", "JPEG", "WEBP")) as image:
        if image.width * image.height > 16_000_000:
            raise ValueError("图片像素超过限制。")
        results = zxingcpp.read_barcodes(
            image.convert("RGB"), formats=zxingcpp.BarcodeFormat.QRCode,
        )
    if len(results) != 1:
        raise ValueError("图片中应包含且仅包含一个二维码。")
    return results[0].text


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
