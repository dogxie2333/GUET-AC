"""User-owned longitude/latitude presets and explicit coordinate conversion."""

import copy
import hashlib
import math
import re
import shutil
import sys
from pathlib import Path

from ..errors import ProtocolError
from ..gps import Coordinates


def coordinates(longitude: str, latitude: str, system: str = "gcj02") -> Coordinates:
    try:
        point = Coordinates(float(latitude), float(longitude))
    except (ValueError, TypeError, ProtocolError) as exc:
        raise ValueError("请输入有效经纬度：经度 -180～180，纬度 -90～90。") from exc
    if system == "wgs84":
        return point
    if system != "gcj02":
        raise ValueError("请选择 WGS84 或高德 GCJ-02 坐标系。")
    # Inverse approximation of the standard GCJ-02 model for the selected point.
    lon, lat = point.longitude, point.latitude
    if not (72.004 <= lon <= 137.8347 and 0.8293 <= lat <= 55.8271):
        return point
    x, y = lon - 105, lat - 35
    dlat = -100 + 2 * x + 3 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    dlon = 300 + x + 2 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    dlat += (20 * math.sin(6 * x * math.pi) + 20 * math.sin(2 * x * math.pi)) * 2 / 3
    dlon += (20 * math.sin(6 * x * math.pi) + 20 * math.sin(2 * x * math.pi)) * 2 / 3
    dlat += (20 * math.sin(y * math.pi) + 40 * math.sin(y * math.pi / 3)) * 2 / 3
    dlat += (160 * math.sin(y * math.pi / 12) + 320 * math.sin(y * math.pi / 30)) * 2 / 3
    dlon += (20 * math.sin(x * math.pi) + 40 * math.sin(x * math.pi / 3)) * 2 / 3
    dlon += (150 * math.sin(x * math.pi / 12) + 300 * math.sin(x * math.pi / 30)) * 2 / 3
    rad = lat * math.pi / 180
    magic = 1 - 0.00669342162296594323 * math.sin(rad) ** 2
    dlat = dlat * 180 / ((6378245 * (1 - 0.00669342162296594323)) / (magic**1.5) * math.pi)
    dlon = dlon * 180 / (6378245 / math.sqrt(magic) * math.cos(rad) * math.pi)
    return Coordinates(lat - dlat, lon - dlon)


def load_locations(path: Path) -> list[dict]:
    if path.stat().st_size > 256 * 1024:
        raise ValueError("location.md 过大，请保留地点与坐标内容。")
    group, results = "", []
    for line_no, raw in enumerate(path.read_text("utf-8-sig").splitlines(), 1):
        line = raw.strip().lstrip("# ")
        if not line or line == "---":
            continue
        if line.endswith(("：", ":")):
            group = line[:-1].strip()
            continue
        match = re.fullmatch(r"([^:：]{1,80})[:：]\s*([^,，]+)[,，]\s*(.+)", line)
        if not match:
            raise ValueError(f"location.md 第 {line_no} 行格式无效，应为 地点：经度,纬度。")
        name, lon, lat = match.groups()
        coordinates(lon, lat, "wgs84")
        results.append(
            {
                "name": f"{group} · {name.strip()}" if group else name.strip(),
                "longitude": lon.strip(),
                "latitude": lat.strip(),
            }
        )
    return results


def locations_file(directory: Path) -> Path:
    if not getattr(sys, "frozen", False):
        return Path(__file__).resolve().parents[2] / "location.md"
    path = directory / "ios/location.md"
    if not path.exists():
        seed = Path(sys._MEIPASS) / "ios-resources/location.md"
        if seed.is_file():
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(seed, path)
    return path


def merge_presets(data: dict, path: Path) -> dict:
    """Add GCJ-02 presets as WGS84 choices; never pick a location for the user."""
    if not path.is_file():
        return data
    presets = load_locations(path)
    result = copy.deepcopy(data)
    gps = result.setdefault("gps", {})
    if not isinstance(gps, dict):
        return data
    locations = gps.setdefault("locations", {})
    if not isinstance(locations, dict):
        return data
    for key in list(locations):
        if key.startswith("md_"):
            del locations[key]
    for item in presets:
        key = "md_" + hashlib.sha256(item["name"].encode("utf-8")).hexdigest()[:16]
        point = coordinates(item["longitude"], item["latitude"], "gcj02")
        locations[key] = {
            "name": item["name"],
            "latitude": point.latitude,
            "longitude": point.longitude,
        }
    selected = gps.get("location", "")
    if isinstance(selected, str) and selected.startswith("md_") and selected not in locations:
        gps["location"] = ""
    return result
