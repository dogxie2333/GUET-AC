import base64
import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, quote

import pytest
import requests
import responses
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from guet_ac import auth, config, qr, store
from guet_ac.auth import BASE, CAS, GuetError
from guet_ac.cli import watch
from guet_ac.client import Client, active

FORM = '''<form id="pwdFromId" method="post" action="/authserver/login">
<input name="username"><input name="passwordText" type="password">
<input name="password" type="hidden"><input id="pwdEncryptSalt" value="0123456789abcdef">
<input type="hidden" name="execution" value="e1s1">
<input type="hidden" name="lt" value=""></form>'''


def record(**kwargs):
    return {"id": "22", "course_id": "11", "is_radar": True,
            "rollcall_status": "in_progress", "status": "absent", **kwargs}


def decrypt_server(encoded, salt=b"0123456789abcdef"):
    # 服务端没有传回随机 IV，按固定 IV 解密后丢弃随机前缀。
    decryptor = Cipher(algorithms.AES(salt), modes.CBC(b"0" * 16)).decryptor()
    padded = decryptor.update(base64.b64decode(encoded)) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return (unpadder.update(padded) + unpadder.finalize()).decode()[64:]


def test_ascii_iv_password_encoding():
    # 连续随机化时服务端 UTF-8 解码应稳定，避免随机二进制 IV 的间歇密码错误。
    for _ in range(64):
        assert decrypt_server(auth.encrypt_password("示例Password!", "0123456789abcdef")) == "示例Password!"


@pytest.mark.parametrize("target", ["http://cas.guet.edu.cn/a", "https://cas.guet.edu.cn.evil/a",
                                    "https://evil.example/a", "https://x@cas.guet.edu.cn/a"])
def test_auth_redirect_host_allowlist(target):
    with pytest.raises(GuetError):
        auth.checked_url(target)


@responses.activate
@pytest.mark.parametrize("captcha", [False, True])
def test_guet_cas_chain(captcha):
    responses.get(BASE + "/user/index", status=302,
                  headers={"Location": "https://identity.guet.edu.cn/start",
                           "Set-Cookie": "session=test; Secure; Path=/"})
    responses.get("https://identity.guet.edu.cn/start", status=302,
                  headers={"Location": CAS + "/authserver/login?service=https%3A%2F%2Fidentity.guet.edu.cn%2Fdone"})
    responses.get(CAS + "/authserver/login", body=FORM)
    responses.get(CAS + "/authserver/bfp/info", json={})
    responses.get(CAS + "/authserver/checkNeedCaptcha.htl", json={"isNeed": captcha})
    if captcha:
        responses.get(CAS + "/authserver/getCaptcha.htl", body=b"image")

    def accept(request):
        data = parse_qs(request.body)
        assert data["username"] == ["student"]
        assert decrypt_server(data["password"][0]) == "test-password"
        assert "passwordText" not in data
        assert data["execution"] == ["e1s1"]
        assert data["cllt"] == ["userNameLogin"]
        assert "service=" in request.url
        if captcha:
            assert data["captcha"] == ["1234"]
        return 302, {"Location": BASE + "/user/index"}, ""

    responses.add_callback("POST", CAS + "/authserver/login", callback=accept)
    responses.get(BASE + "/user/index", body="业务首页")
    responses.get(BASE + "/api/profile", json={"id": 1})
    with auth.login("student", "test-password", lambda image: "1234") as session:
        assert session.cookies.get("session") == "test"


@responses.activate
def test_html_200_not_login_success():
    responses.get(BASE + "/api/profile", body="<html>登录</html>")
    with pytest.raises(GuetError, match="JSON"):
        Client(requests.Session()).profile()


@responses.activate
def test_offsite_redirect_not_followed():
    responses.get(BASE + "/user/index", status=302, headers={"Location": "https://evil.example"})
    with pytest.raises(GuetError):
        auth.follow(requests.Session(), "GET", BASE + "/user/index")
    assert len(responses.calls) == 1


@pytest.mark.parametrize("wrapper", [lambda s: s, lambda s: "/j?p=" + quote(s),
                                    lambda s: "https://mobile.guet.edu.cn/scanner-jumper?p=" + quote(s)])
def test_compact_qr(wrapper):
    parsed = qr.parse(wrapper("0~\x10b!4~\x10m!3~token\x1fpart\x1emore"))
    assert parsed == {"courseId": "11", "rollcallId": "22", "data": "token~part!more"}


def test_json_qr():
    payload = {"courseId": 11, "rollcallId": 22, "data": "token"}
    assert qr.parse("/scanner-jumper?_p=" + quote(json.dumps(payload)))["rollcallId"] == "22"


@pytest.mark.parametrize("text", ["https://evil.example", "[]", '{"rollcallId":true}',
                                  '{"courseId":11,"rollcallId":22,"data":""}'])
def test_bad_qr(text):
    with pytest.raises(GuetError):
        qr.parse(text)


def test_course_location_priority(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('default_location="osm-way-606411550"\n[course_locations]\n'
                    '"11"="osm-way-606478146"\n', encoding="utf-8")
    cfg = config.load(path)
    assert config.location_for(cfg, "11")["name"] == "第二教学楼"
    assert config.location_for(cfg, "12")["name"] == "第一教学楼"


def test_place_source_and_coordinates():
    presets = config.locations()
    assert len(presets) >= 50
    assert all(25.3057 < p["latitude"] < 25.3231 and 110.4022 < p["longitude"] < 110.4209
               and p["source"].startswith("https://www.openstreetmap.org/")
               for p in presets.values())


@pytest.mark.parametrize("changes", [{"is_expired": True}, {"status": "on_call"},
                                     {"rollcall_status": "finished"},
                                     {"end_time": "invalid"},
                                     {"end_time": (datetime.now(UTC) - timedelta(minutes=1)).isoformat()}])
def test_no_submit_for_inactive(changes):
    assert not active(record(**changes))


@responses.activate
@pytest.mark.parametrize("channel", ["GPS", "数字码", "二维码"])
def test_submit_protocol_and_verify(channel):
    rec = record(is_radar=channel == "GPS", is_number=channel == "数字码",
                 type="qr_rollcall" if channel == "二维码" else "")
    responses.get(BASE + "/api/radar/rollcalls", json={"rollcalls": [rec]})
    suffix = {"GPS": "answer", "数字码": "answer_number_rollcall",
              "二维码": "answer_qr_rollcall"}[channel]

    def accept(request):
        body = json.loads(request.body)
        assert body["deviceId"] == "test-device"
        if channel == "GPS":
            # GCJ 转换应有数百米偏移，避免经纬度写反/遗漏转换。
            assert 25.30 < body["latitude"] < 25.33
            assert 110.41 < body["longitude"] < 110.43
            assert abs(body["longitude"] - 110.4132312) > 0.001
        if channel == "数字码":
            assert body["numberCode"] == "0123"
        if channel == "二维码":
            assert body["data"] == "test-token"
        return 200, {"Content-Type": "application/json"}, '{}'

    responses.add_callback("PUT", BASE + f"/api/rollcall/22/{suffix}", callback=accept)
    responses.get(BASE + "/api/radar/rollcalls", json={"rollcalls": [{**rec, "status": "on_call"}]})
    client = Client(requests.Session(), "test-device")
    assert client.submit(rec, channel, place=config.locations()["osm-way-606411550"],
                         code="0123", token="test-token")


@responses.activate
def test_expired_before_submit_never_put():
    responses.get(BASE + "/api/radar/rollcalls", json={"rollcalls": [record(is_expired=True)]})
    with pytest.raises(GuetError):
        Client(requests.Session()).submit(record(), "GPS", place={})
    assert all(call.request.method == "GET" for call in responses.calls)


@responses.activate
def test_qr_403_never_fabricates_data(tmp_path):
    rec = record(is_radar=False, type="qr_rollcall")
    responses.get(BASE + "/api/radar/rollcalls", json={"rollcalls": [rec]})
    responses.get(BASE + "/api/course/11/rollcall/22/qr_code", status=403)
    path = tmp_path / "config.toml"
    path.write_text("auto_qr=true", encoding="utf-8")
    watch(Client(requests.Session()), config.load(path), once=True)
    assert len(responses.calls) == 2
    assert all(call.request.method == "GET" for call in responses.calls)


@responses.activate
def test_dry_run_has_no_writes(tmp_path):
    responses.get(BASE + "/api/radar/rollcalls", json={"rollcalls": [record()]})
    path = tmp_path / "config.toml"
    path.write_text('default_location="osm-way-606411550"', encoding="utf-8")
    watch(Client(requests.Session()), config.load(path), once=True, dry_run=True)
    assert len(responses.calls) == 1


def test_credential_storage_isolated_and_no_password(monkeypatch):
    storage = {}
    monkeypatch.setattr(store, "secure_backend", lambda: None)
    monkeypatch.setattr(store.keyring, "set_password", lambda service, account, value:
                        storage.__setitem__((service, account), value))
    monkeypatch.setattr(store.keyring, "get_password", lambda service, account:
                        storage.get((service, account)))
    session = requests.Session()
    session.cookies.set("session", "secret-a", domain="courses.guet.edu.cn", secure=True)
    session.cookies.set("CASTGC", "cas-secret", domain="cas.guet.edu.cn", secure=True)
    store.save("a", session, "device-a")
    saved, device = store.load("a")
    assert saved.cookies.get("session") == "secret-a"
    assert saved.cookies.get("CASTGC") is None
    assert device == "device-a"
    with pytest.raises(GuetError):
        store.load("b")
    assert "cas-secret" not in storage[(store.SERVICE, "a")]
