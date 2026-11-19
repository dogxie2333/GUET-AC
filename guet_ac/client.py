import re
import uuid
from datetime import datetime, timezone

import requests
from coord_convert.transform import wgs2gcj

from .auth import BASE, GuetError

SIGNED = {"on_call", "on_call_fine", "on_personal_leave"}
ACTIVE = {"in_progress", "on_going", "active", "started"}


def identifier(value):
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]+", str(value)):
        raise GuetError("课程或签到编号无效。")
    return str(value)


def kind(record):
    if record.get("is_radar"):
        return "GPS"
    if record.get("is_number"):
        return "数字码"
    return "二维码" if record.get("type") == "qr_rollcall" else "其他"


def active(record):
    if (record.get("is_expired") or record.get("status") in SIGNED
            or record.get("rollcall_status") not in ACTIVE):
        return False
    if record.get("end_time"):
        try:
            end = datetime.fromisoformat(record["end_time"].replace("Z", "+00:00"))
            if end.tzinfo is None or end <= datetime.now(timezone.utc):
                return False
        except (ValueError, TypeError):
            return False
    return True


class Client:
    def __init__(self, session, device_id=None):
        self.session = session
        self.device_id = device_id or str(uuid.uuid4())
        self.student_id = None

    def request(self, method, path, **kwargs):
        if not re.fullmatch(r"/api/[A-Za-z0-9_/-]+", path):
            raise GuetError("API 路径无效。")
        try:
            with self.session.request(method, BASE + path, timeout=20, allow_redirects=False,
                                      headers={"Accept": "application/json"}, **kwargs) as response:
                if response.status_code in (301, 302, 303, 307, 308, 401):
                    raise GuetError("会话已过期，请重新 login。")
                if response.status_code == 403:
                    raise GuetError("此账号没有接口访问权限（403）。")
                if not 200 <= response.status_code < 300:
                    raise GuetError(f"接口返回 HTTP {response.status_code}。")
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise GuetError("接口未返回 JSON，请重新登录或检查站点状态。") from exc
                if isinstance(payload, dict) and (payload.get("error_code")
                                                 or payload.get("success") is False):
                    raise GuetError("服务器拒绝请求：" + str(payload.get("error_code", "业务错误")))
                return payload
        except requests.RequestException as exc:
            raise GuetError("请求中断，请检查网络；提交结果请在畅课内查看。") from exc

    def profile(self):
        data = self.request("GET", "/api/profile")
        if not isinstance(data, dict) or not data.get("id"):
            raise GuetError("未取得桂电畅课登录会话。")
        self.student_id = identifier(data["id"])
        return data

    def rollcalls(self):
        data = self.request("GET", "/api/radar/rollcalls")
        records = data.get("rollcalls") if isinstance(data, dict) else None
        if not isinstance(records, list) or not all(isinstance(r, dict) for r in records):
            raise GuetError("签到列表响应结构改变。")
        return records

    def fresh(self, record):
        rid, cid = identifier(record["id"]), identifier(record["course_id"])
        for item in self.rollcalls():
            if (str(item.get("rollcall_id", item.get("id"))) == rid
                    and str(item.get("course_id")) == cid and active(item)):
                return {**item, "id": rid}
        raise GuetError("签到活动已结束、已签到或不再可用。")

    def number_code(self, record):
        rid = identifier(record["id"])
        data = self.request("GET", f"/api/rollcall/{rid}/student_rollcalls")
        code = data.get("number_code") if isinstance(data, dict) else None
        if type(code) is int and code >= 0:
            code = str(code).zfill(4)
        if not isinstance(code, str) or not re.fullmatch(r"[0-9]{4,8}", code):
            raise GuetError("接口没有返回数字码，可使用 number 命令输入课堂数字码。")
        return code

    def submit(self, record, channel, *, place=None, code=None, token=None, accuracy=35,
               coordinate_system="gcj02"):
        fresh = self.fresh(record)
        rid = identifier(fresh["id"])
        if kind(fresh) != channel:
            raise GuetError("活动类型与提交方式不匹配。")
        body = {"deviceId": self.device_id}
        params = {}
        if channel == "GPS":
            if place is None:
                raise GuetError("该课程尚未选择签到地点。")
            latitude, longitude = place["latitude"], place["longitude"]
            if coordinate_system == "gcj02":
                longitude, latitude = wgs2gcj(longitude, latitude)
            elif coordinate_system != "wgs84":
                raise GuetError("未知坐标系。")
            body.update(latitude=latitude, longitude=longitude,
                        accuracy=accuracy, altitude=None, altitudeAccuracy=None,
                        heading=None, speed=None)
            suffix, params = "answer", {"api_version": "1.1.2"}
        elif channel == "数字码":
            if not isinstance(code, str) or not re.fullmatch(r"[0-9]{4,8}", code):
                raise GuetError("数字码格式无效。")
            body["numberCode"], suffix = code, "answer_number_rollcall"
        elif channel == "二维码":
            if not isinstance(token, str) or not token:
                raise GuetError("二维码 data 为空。")
            body["data"], suffix = token, "answer_qr_rollcall"
        else:
            raise GuetError("未支持的签到类型。")
        self.request("PUT", f"/api/rollcall/{rid}/{suffix}", json=body, params=params)
        return self.verify(record)

    def qr_token(self, record):
        cid, rid = identifier(record["course_id"]), identifier(record["id"])
        data = self.request("GET", f"/api/course/{cid}/rollcall/{rid}/qr_code")
        token = data.get("data") if isinstance(data, dict) else None
        if not isinstance(token, str) or not re.fullmatch(r"[0-9]{10}[0-9a-f]{32}", token):
            raise GuetError("取码接口未返回有效二维码 data。")
        age = datetime.now(timezone.utc).timestamp() - int(token[:10])
        if not -2 <= age <= 3:
            raise GuetError("二维码 data 已过期或本机时钟不正确。")
        return token

    def verify(self, record):
        for item in self.rollcalls():
            if (str(item.get("rollcall_id", item.get("id"))) == str(record["id"])
                    and str(item.get("course_id")) == str(record["course_id"])):
                return item.get("status") in SIGNED
        if self.student_id is None:
            self.profile()
        cid = identifier(record["course_id"])
        data = self.request("GET", f"/api/course/{cid}/student/{self.student_id}/rollcalls")
        records = data.get("rollcalls", []) if isinstance(data, dict) else data
        if not isinstance(records, list):
            return False
        return any(isinstance(item, dict) and str(item.get("rollcall_id", item.get("id")))
                   == str(record["id"]) and item.get("status") in SIGNED for item in records)

