import json
import math
from importlib.resources import files
from pathlib import Path

from .auth import GuetError

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


def locations():
    resource = files("guet_ac").joinpath("data/huajiang.json")
    return {item["id"]: item for item in json.loads(resource.read_text(encoding="utf-8"))["places"]}


def load(path):
    data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    data.setdefault("default_location", "")
    data.setdefault("course_locations", {})
    data.setdefault("poll_seconds", 15)
    data.setdefault("auto_qr", False)
    data.setdefault("accuracy", 35.0)
    data.setdefault("submit_coordinate_system", "gcj02")
    if data["submit_coordinate_system"] not in ("wgs84", "gcj02"):
        raise GuetError("submit_coordinate_system 必须为 wgs84 或 gcj02。")
    if type(data["poll_seconds"]) is not int or data["poll_seconds"] < 5:
        raise GuetError("poll_seconds 必须是至少 5 的整数。")
    if not isinstance(data["auto_qr"], bool):
        raise GuetError("auto_qr 必须是布尔值。")
    if (type(data["accuracy"]) not in (int, float) or not math.isfinite(data["accuracy"])
            or not 0 < data["accuracy"] <= 10000):
        raise GuetError("accuracy 必须大于 0 且不超过 10000。")
    presets = locations()
    if not isinstance(data["course_locations"], dict):
        raise GuetError("course_locations 必须为课号到地点的配置表。")
    for place in [data["default_location"], *data["course_locations"].values()]:
        if not isinstance(place, str) or (place and place not in presets):
            raise GuetError(f"未知地点：{place}，请运行 locations 查看。")
    for course in data["course_locations"]:
        if not course.isascii() or not course.isdigit():
            raise GuetError("课程绑定键必须是畅课 course_id。")
    return data


def location_for(config, course_id):
    key = config["course_locations"].get(str(course_id), config["default_location"])
    return locations().get(key)
