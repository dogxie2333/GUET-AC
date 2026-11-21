"""config.toml 读取、校验与 JSON/TOML 序列化。

字段与默认值见 docs/interfaces.md 第 1 节 config-json；校验入口由 load 与
config-update 共用，保证网页写回的配置与本地读取使用同一套规则。
"""

import json
import math
from importlib.resources import files
from pathlib import Path

from .auth import GuetError

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

FIELDS = ("default_location", "course_locations", "auto_number", "auto_gps", "auto_qr",
          "poll_seconds", "accuracy", "submit_coordinate_system")


def locations():
    resource = files("guet_ac").joinpath("data/huajiang.json")
    return {item["id"]: item for item in json.loads(resource.read_text(encoding="utf-8"))["places"]}


def validate(data):
    """补齐默认值并校验配置；返回同一个 dict，供 load 与 config-update 复用。"""
    if not isinstance(data, dict):
        raise GuetError("配置必须是 JSON/TOML 对象。")
    data.setdefault("default_location", "")
    data.setdefault("course_locations", {})
    data.setdefault("auto_number", False)
    data.setdefault("auto_gps", False)
    data.setdefault("auto_qr", False)
    data.setdefault("poll_seconds", 15)
    data.setdefault("accuracy", 35.0)
    data.setdefault("submit_coordinate_system", "gcj02")
    if data["submit_coordinate_system"] not in ("wgs84", "gcj02"):
        raise GuetError("submit_coordinate_system 必须为 wgs84 或 gcj02。")
    if type(data["poll_seconds"]) is not int or data["poll_seconds"] < 5:
        raise GuetError("poll_seconds 必须是至少 5 的整数。")
    for key in ("auto_number", "auto_gps", "auto_qr"):
        if not isinstance(data[key], bool):
            raise GuetError(f"{key} 必须是布尔值。")
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
        if not isinstance(course, str) or not course.isascii() or not course.isdigit():
            raise GuetError("课程绑定键必须是畅课 course_id。")
    return data


def load(path):
    return validate(tomllib.loads(Path(path).read_text(encoding="utf-8")))


def location_for(config, course_id):
    key = config["course_locations"].get(str(course_id), config["default_location"])
    return locations().get(key)


def public(config):
    """config-json / config-update 输出字段；不包含任何会话或凭据。"""
    return {
        "default_location": config["default_location"],
        "course_locations": dict(config["course_locations"]),
        "auto_number": config["auto_number"],
        "auto_gps": config["auto_gps"],
        "auto_qr": config["auto_qr"],
        "poll_seconds": config["poll_seconds"],
        "accuracy": float(config["accuracy"]),
        "submit_coordinate_system": config["submit_coordinate_system"],
    }


def dumps(config):
    """把配置写成规范化 TOML；config-update 先用备份保留原文件内容。"""
    def quoted(value):
        return json.dumps(value, ensure_ascii=False)

    lines = [
        "# GUET-AC 配置（UTF-8）。由 guet-ac config-update 写回，原文件已备份。",
        f'default_location = {quoted(config["default_location"])}',
        f'poll_seconds = {int(config["poll_seconds"])}',
        f'accuracy = {float(config["accuracy"])}',
        f'submit_coordinate_system = {quoted(config["submit_coordinate_system"])}',
        f'auto_number = {"true" if config["auto_number"] else "false"}',
        f'auto_gps = {"true" if config["auto_gps"] else "false"}',
        f'auto_qr = {"true" if config["auto_qr"] else "false"}',
        "",
        "[course_locations]",
    ]
    for course_id, place_id in config["course_locations"].items():
        lines.append(f"{quoted(str(course_id))} = {quoted(place_id)}")
    lines.append("")
    return "\n".join(lines)
