"""TronClass 二维码载荷解析。协议参考见 docs/protocol.md。"""

import json
from urllib.parse import unquote, urlsplit

from .auth import GuetError

FIELDS = ("courseId", "activityId", "activityType", "data", "rollcallId", "groupSetId",
          "accessCode", "action", "enableGroupRollcall", "createUser", "joinCourse")


def parse(text):
    if len(text) > 16384:
        raise GuetError("二维码内容过长。")
    raw = text.strip()
    if "?" in raw:
        query = urlsplit(raw).query
        for piece in query.split("&"):
            key, _, value = piece.partition("=")
            if key in ("p", "_p", "_wg_p"):
                raw = unquote(value)
                break
    if raw.startswith("{"):
        try:
            result = json.loads(raw)
        except ValueError as exc:
            raise GuetError("二维码 JSON 无效。") from exc
    else:
        result = {}
        for pair in raw.split("!"):
            key, sep, value = pair.partition("~")
            if not sep:
                continue
            if key in "0123456789a" and len(key) == 1:
                key = FIELDS[int(key, 36)]
            if value.startswith("\x10"):
                try:
                    value = int(value[1:], 36)
                except ValueError as exc:
                    raise GuetError("二维码编号编码无效。") from exc
            else:
                value = value.replace("\x1f", "~").replace("\x1e", "!")
            result[key] = value
    if not isinstance(result, dict):
        raise GuetError("二维码内容不是对象。")
    for name in ("rollcallId", "courseId"):
        value = result.get(name)
        if isinstance(value, bool) or not str(value).isascii() or not str(value).isdigit():
            raise GuetError(f"二维码缺少有效的 {name}。")
        result[name] = str(value)
    if not isinstance(result.get("data"), str) or not result["data"]:
        raise GuetError("二维码缺少有效 data。")
    return result
