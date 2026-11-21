"""本轮新增行为：自动通道、去重恢复、GPS/数字码提交与 CLI JSON 契约。"""

import io
import json
import os
import re
import stat
import sys
from datetime import datetime, timedelta, timezone

import pytest
import requests
import responses

from guet_ac import cli, config, ledger, store
from guet_ac.auth import BASE, GuetError
from guet_ac.cli import (automatic_channels, config_stamp, main, refresh_config, watch)
from guet_ac.client import Client

HEX_EVENT = re.compile(r"[0-9a-f]{64}\.json")
PLACE = "osm-way-606411550"


def record(**kwargs):
    return {"id": "22", "course_id": "11", "is_radar": True,
            "rollcall_status": "in_progress", "status": "absent", **kwargs}


def write_config(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def event_files(directory):
    return [json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(directory.glob("*.json")) if HEX_EVENT.fullmatch(path.name)]


def read_state(directory):
    return json.loads((directory / "state.json").read_text(encoding="utf-8"))["activities"]


# ---------------------------------------------------------------- 自动 GPS / 数字码


@responses.activate
def test_gps_submit_confirmed_and_deduped_across_restart(tmp_path, capsys):
    path = write_config(tmp_path, f'default_location="{PLACE}"\n')
    event_dir = tmp_path / "events"
    event_dir.mkdir()
    responses.get(BASE + "/api/radar/rollcalls", json={"rollcalls": [record()]})
    responses.get(BASE + "/api/radar/rollcalls", json={"rollcalls": [record()]})
    responses.put(BASE + "/api/rollcall/22/answer", json={})
    responses.get(BASE + "/api/radar/rollcalls",
                  json={"rollcalls": [record(status="on_call")]})
    responses.get(BASE + "/api/radar/rollcalls", json={"rollcalls": [record()]})
    client = Client(requests.Session(), "device")
    watch(client, config.load(path), once=True, auto_gps=True, event_dir=event_dir)
    puts = [call for call in responses.calls if call.request.method == "PUT"]
    assert len(puts) == 1
    assert puts[0].request.url.startswith(BASE + "/api/rollcall/22/answer")
    assert read_state(event_dir)["22"]["stage"] == "confirmed"
    stages = [event["stage"] for event in event_files(event_dir)]
    assert "confirmed" in stages and "started" in stages
    # 模拟进程重启：同一 event-dir 再次运行不得再次提交。
    watch(Client(requests.Session(), "device"), config.load(path), once=True,
          auto_gps=True, event_dir=event_dir)
    assert len([call for call in responses.calls if call.request.method == "PUT"]) == 1
    assert len(event_files(event_dir)) == len(stages)


@responses.activate
def test_gps_without_place_reports_action_required(tmp_path):
    path = write_config(tmp_path, 'default_location=""\n')
    event_dir = tmp_path / "events"
    event_dir.mkdir()
    responses.get(BASE + "/api/radar/rollcalls", json={"rollcalls": [record()]})
    watch(Client(requests.Session()), config.load(path), once=True,
          auto_gps=True, event_dir=event_dir)
    assert all(call.request.method == "GET" for call in responses.calls)
    events = event_files(event_dir)
    assert [event["stage"] for event in events].count("action_required") == 1
    assert "未绑定签到地点" in next(e for e in events
                                   if e["stage"] == "action_required")["message"]
    assert read_state(event_dir)["22"]["stage"] == "action_required"


@responses.activate
def test_number_code_confirmed(tmp_path):
    path = write_config(tmp_path, "")
    event_dir = tmp_path / "events"
    event_dir.mkdir()
    rec = record(is_radar=False, is_number=True)
    responses.get(BASE + "/api/radar/rollcalls", json={"rollcalls": [rec]})
    responses.get(BASE + "/api/radar/rollcalls", json={"rollcalls": [rec]})
    responses.get(BASE + "/api/rollcall/22/student_rollcalls", json={"number_code": 123})
    responses.put(BASE + "/api/rollcall/22/answer_number_rollcall", json={})
    responses.get(BASE + "/api/radar/rollcalls",
                  json={"rollcalls": [{**rec, "status": "on_call"}]})
    watch(Client(requests.Session()), config.load(path), once=True,
          auto_number=True, event_dir=event_dir)
    put = [call for call in responses.calls if call.request.method == "PUT"][0]
    assert json.loads(put.request.body)["numberCode"] == "0123"
    assert read_state(event_dir)["22"]["stage"] == "confirmed"
    assert "confirmed" in [event["stage"] for event in event_files(event_dir)]


@responses.activate
def test_number_code_unconfirmed_when_verify_absent(tmp_path):
    path = write_config(tmp_path, "")
    event_dir = tmp_path / "events"
    event_dir.mkdir()
    rec = record(is_radar=False, is_number=True)
    responses.get(BASE + "/api/radar/rollcalls", json={"rollcalls": [rec]})
    responses.get(BASE + "/api/radar/rollcalls", json={"rollcalls": [rec]})
    responses.get(BASE + "/api/rollcall/22/student_rollcalls",
                  json={"number_code": "0123"})
    responses.put(BASE + "/api/rollcall/22/answer_number_rollcall", json={})
    responses.get(BASE + "/api/radar/rollcalls", json={"rollcalls": [rec]})
    watch(Client(requests.Session()), config.load(path), once=True,
          auto_number=True, event_dir=event_dir)
    assert read_state(event_dir)["22"]["stage"] == "unconfirmed"
    assert "unconfirmed" in [event["stage"] for event in event_files(event_dir)]


@responses.activate
def test_dry_run_never_writes_or_submits(tmp_path):
    path = write_config(tmp_path, f'default_location="{PLACE}"\n')
    event_dir = tmp_path / "events"
    responses.get(BASE + "/api/radar/rollcalls", json={"rollcalls": [record()]})
    watch(Client(requests.Session()), config.load(path), once=True,
          dry_run=True, auto_gps=True, event_dir=event_dir)
    assert all(call.request.method == "GET" for call in responses.calls)
    assert not (event_dir / "state.json").exists()


@responses.activate
def test_restart_recovery_marks_unconfirmed_without_put(tmp_path):
    path = write_config(tmp_path, f'default_location="{PLACE}"\n')
    event_dir = tmp_path / "events"
    event_dir.mkdir()
    (event_dir / "state.json").write_text(json.dumps({
        "version": 1,
        "activities": {"22": {"course_id": "11", "channel": "GPS", "stage": "submitting",
                              "updated_at": datetime.now(timezone.utc).isoformat()}},
    }), encoding="utf-8")
    responses.get(BASE + "/api/radar/rollcalls", json={"rollcalls": [record()]})
    watch(Client(requests.Session()), config.load(path), once=True,
          auto_gps=True, event_dir=event_dir)
    assert all(call.request.method == "GET" for call in responses.calls)
    events = event_files(event_dir)
    unconfirmed = [event for event in events if event["stage"] == "unconfirmed"]
    assert len(unconfirmed) == 1
    assert "结果不明" in unconfirmed[0]["message"]
    assert read_state(event_dir)["22"]["stage"] == "unconfirmed"


# ------------------------------------------------------------------- qr-submit


def qr_payload_json():
    return json.dumps({"courseId": "11", "rollcallId": "22", "data": "test-token"})


def invoke(capsys, argv, stdin_text):
    old = sys.stdin
    sys.stdin = io.StringIO(stdin_text)
    try:
        return main(argv)
    finally:
        sys.stdin = old


@responses.activate
def test_qr_submit_parse_stage(capsys):
    with pytest.raises(SystemExit) as info:
        invoke(capsys, ["qr-submit"], "{}")
    assert info.value.code == 2
    stream = capsys.readouterr()
    payload = json.loads(stream.err.strip())
    assert payload["ok"] is False and payload["stage"] == "parse" and payload["error"]
    assert stream.out == ""


def test_qr_submit_session_stage(capsys, monkeypatch):
    def missing(account):
        raise GuetError("此账号尚未登录，请先运行 login。")
    monkeypatch.setattr(store, "load", missing)
    with pytest.raises(SystemExit) as info:
        invoke(capsys, ["qr-submit"], qr_payload_json())
    assert info.value.code == 2
    payload = json.loads(capsys.readouterr().err.strip())
    assert payload["stage"] == "session"


@responses.activate
def test_qr_submit_network_stage(capsys, monkeypatch):
    monkeypatch.setattr(store, "load", lambda account: (requests.Session(), "dev"))
    responses.get(BASE + "/api/radar/rollcalls", body=requests.ConnectionError("boom"))
    with pytest.raises(SystemExit) as info:
        invoke(capsys, ["qr-submit"], qr_payload_json())
    assert info.value.code == 2
    payload = json.loads(capsys.readouterr().err.strip())
    assert payload["stage"] == "network"
    assert "test-token" not in json.dumps(payload)


@responses.activate
def test_qr_submit_rejected_stage(capsys, monkeypatch):
    monkeypatch.setattr(store, "load", lambda account: (requests.Session(), "dev"))
    rec = record(is_radar=False, type="qr_rollcall")
    responses.get(BASE + "/api/radar/rollcalls", json={"rollcalls": [rec]})
    responses.put(BASE + "/api/rollcall/22/answer_qr_rollcall", status=403)
    with pytest.raises(SystemExit) as info:
        invoke(capsys, ["qr-submit"], qr_payload_json())
    assert info.value.code == 2
    payload = json.loads(capsys.readouterr().err.strip())
    assert payload["stage"] == "submit"


@responses.activate
def test_qr_submit_verify_stage(capsys, monkeypatch):
    monkeypatch.setattr(store, "load", lambda account: (requests.Session(), "dev"))
    rec = record(is_radar=False, type="qr_rollcall")
    responses.get(BASE + "/api/radar/rollcalls", json={"rollcalls": [rec]})
    responses.put(BASE + "/api/rollcall/22/answer_qr_rollcall", json={})
    responses.get(BASE + "/api/radar/rollcalls", json={"rollcalls": "broken"})
    with pytest.raises(SystemExit) as info:
        invoke(capsys, ["qr-submit"], qr_payload_json())
    assert info.value.code == 2
    payload = json.loads(capsys.readouterr().err.strip())
    assert payload["stage"] == "verify"


@responses.activate
def test_qr_submit_confirmed_and_unconfirmed(capsys, monkeypatch):
    monkeypatch.setattr(store, "load", lambda account: (requests.Session(), "dev"))
    rec = record(is_radar=False, type="qr_rollcall")
    responses.get(BASE + "/api/radar/rollcalls", json={"rollcalls": [rec]})
    responses.put(BASE + "/api/rollcall/22/answer_qr_rollcall", json={})
    responses.get(BASE + "/api/radar/rollcalls",
                  json={"rollcalls": [{**rec, "status": "on_call"}]})
    invoke(capsys, ["qr-submit"], qr_payload_json())
    stream = capsys.readouterr()
    payload = json.loads(stream.out.strip())
    assert payload == {"ok": True, "confirmed": True, "message": "已确认签到。"}

    responses.get(BASE + "/api/radar/rollcalls", json={"rollcalls": [rec]})
    responses.put(BASE + "/api/rollcall/22/answer_qr_rollcall", json={})
    responses.get(BASE + "/api/radar/rollcalls", json={"rollcalls": [rec]})
    with pytest.raises(SystemExit) as info:
        invoke(capsys, ["qr-submit"], qr_payload_json())
    assert info.value.code == 3
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is True and payload["confirmed"] is False


@responses.activate
def test_qr_submit_accepts_text_payload(capsys, monkeypatch):
    monkeypatch.setattr(store, "load", lambda account: (requests.Session(), "dev"))
    rec = record(is_radar=False, type="qr_rollcall")
    responses.get(BASE + "/api/radar/rollcalls", json={"rollcalls": [rec]})
    responses.put(BASE + "/api/rollcall/22/answer_qr_rollcall", json={})
    responses.get(BASE + "/api/radar/rollcalls",
                  json={"rollcalls": [{**rec, "status": "on_call"}]})
    compact = "0~\x10b!4~\x10m!3~test-token"
    invoke(capsys, ["qr-submit"], json.dumps({"text": compact}))
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload == {"ok": True, "confirmed": True, "message": "已确认签到。"}
    put = [call for call in responses.calls if call.request.method == "PUT"][0]
    assert json.loads(put.request.body)["data"] == "test-token"


# -------------------------------------------------------------- config 子命令


def test_config_json_defaults_and_no_secrets(capsys, tmp_path):
    path = write_config(tmp_path, "")
    main(["--config", str(path), "config-json"])
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload == {
        "default_location": "",
        "course_locations": {},
        "auto_number": False,
        "auto_gps": False,
        "auto_qr": False,
        "poll_seconds": 15,
        "accuracy": 35.0,
        "submit_coordinate_system": "gcj02",
    }
    assert "cookie" not in json.dumps(payload).lower()


def test_config_check_valid_and_invalid(capsys, tmp_path):
    good = write_config(tmp_path, 'default_location="osm-way-606411550"\n')
    main(["--config", str(good), "config-check"])
    assert capsys.readouterr().out == ""
    bad = tmp_path / "bad.toml"
    bad.write_text('default_location="nope"\n', encoding="utf-8")
    with pytest.raises(SystemExit) as info:
        main(["--config", str(bad), "config-check"])
    assert info.value.code == 2
    assert "error" in json.loads(capsys.readouterr().err.strip())


def test_config_update_backup_and_roundtrip(capsys, tmp_path):
    path = write_config(tmp_path, 'default_location=""\n')
    original = path.read_bytes()
    update = {"auto_number": True, "auto_gps": True,
              "course_locations": {"11": PLACE}}
    old = sys.stdin
    sys.stdin = io.StringIO(json.dumps(update))
    try:
        main(["--config", str(path), "config-update"])
    finally:
        sys.stdin = old
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["auto_number"] is True and payload["auto_gps"] is True
    assert payload["course_locations"] == {"11": PLACE}
    assert config.load(path)["auto_number"] is True
    backups = sorted(tmp_path.glob("config.toml.before-web-*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == original
    if os.name == "posix":
        assert stat.S_IMODE(os.stat(backups[0]).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


@pytest.mark.parametrize("update", [
    {"default_location": "unknown-place"},
    {"course_locations": {"x": "osm-way-606411550"}},
    {"auto_number": "yes"},
    {"auto_gps": 1},
    {"poll_seconds": 1},
    {"unknown_key": True},
])
def test_config_update_rejects_invalid_without_touching_file(capsys, tmp_path, update):
    path = write_config(tmp_path, 'default_location=""\n')
    before = path.read_bytes()
    old = sys.stdin
    sys.stdin = io.StringIO(json.dumps(update))
    try:
        with pytest.raises(SystemExit) as info:
            main(["--config", str(path), "config-update"])
    finally:
        sys.stdin = old
    assert info.value.code == 2
    payload = json.loads(capsys.readouterr().err.strip())
    assert payload["error"]
    assert path.read_bytes() == before


# ------------------------------------------------------------- status/test-event


@responses.activate
def test_status_json_has_no_credentials(capsys, monkeypatch, tmp_path):
    session = requests.Session()
    session.cookies.set("session", "secret-cookie", domain="courses.guet.edu.cn")
    monkeypatch.setattr(store, "load", lambda account: (session, "dev"))
    responses.get(BASE + "/api/profile", json={"id": 123, "name": "张三"})
    responses.get(BASE + "/api/radar/rollcalls", json={"rollcalls": [
        record(is_radar=True, is_number=False, type=""),
        record(id="23", course_id="12", is_radar=False, is_number=False,
               type="qr_rollcall", rollcall_status="finished"),
    ]})
    main(["--config", str(tmp_path / "missing.toml"), "status", "--json"])
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    assert payload["profile"] == {"id": "123", "name": "张三"}
    assert payload["rollcalls"][0]["kind"] == "GPS"
    assert payload["rollcalls"][0]["active"] is True
    assert payload["rollcalls"][1]["kind"] == "二维码"
    assert payload["rollcalls"][1]["active"] is False
    assert datetime.fromisoformat(payload["at"]).tzinfo is not None
    lowered = out.lower()
    for secret in ("secret-cookie", "cookie", "session", "set-cookie"):
        assert secret not in lowered


@responses.activate
def test_status_json_failure_exit_2(capsys, monkeypatch, tmp_path):
    monkeypatch.setattr(store, "load", lambda account: (requests.Session(), "dev"))
    responses.get(BASE + "/api/profile", status=401)
    with pytest.raises(SystemExit) as info:
        main(["--config", str(tmp_path / "missing.toml"), "status", "--json"])
    assert info.value.code == 2
    assert "error" in json.loads(capsys.readouterr().err.strip())


def test_test_event_file_format(capsys, tmp_path):
    event_dir = tmp_path / "events"
    main(["test-event", "--event-dir", str(event_dir),
          "--channel", "GPS", "--stage", "action_required"])
    printed = capsys.readouterr().out.strip()
    path = next(event_dir.glob("*.json"))
    assert path == type(path)(printed)
    assert len(printed.splitlines()) == 1
    assert HEX_EVENT.fullmatch(path.name)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["stage"] == "action_required"
    assert payload["channel"] == "GPS"
    assert payload["test"] is True
    assert payload["message"].startswith("【测试】")
    assert len(payload["message"]) <= 200
    assert not (event_dir / "state.json").exists()
    # 两次测试事件互不覆盖。
    main(["test-event", "--event-dir", str(event_dir)])
    assert len(list(event_dir.glob("*.json"))) == 2


@responses.activate
def test_watch_auto_follows_config_switches(tmp_path):
    path = write_config(tmp_path, f'default_location="{PLACE}"\n'
                                  "auto_number=false\nauto_gps=true\n")
    event_dir = tmp_path / "events"
    gps = record()
    number = record(id="23", is_radar=False, is_number=True)
    responses.get(BASE + "/api/radar/rollcalls", json={"rollcalls": [gps, number]})
    responses.get(BASE + "/api/radar/rollcalls", json={"rollcalls": [gps, number]})
    responses.put(BASE + "/api/rollcall/22/answer", json={})
    responses.get(BASE + "/api/radar/rollcalls",
                  json={"rollcalls": [{**gps, "status": "on_call"}, number]})
    watch(Client(requests.Session()), config.load(path), once=True,
          auto=True, event_dir=event_dir)
    puts = [call.request.url for call in responses.calls if call.request.method == "PUT"]
    assert len(puts) == 1 and "answer_number_rollcall" not in puts[0]
    # auto_number=false：数字码只记录开始，不读取数字码。
    assert not any("student_rollcalls" in call.request.url for call in responses.calls)


@responses.activate
def test_state_file_override(tmp_path):
    path = write_config(tmp_path, f'default_location="{PLACE}"\n')
    event_dir = tmp_path / "events"
    state_file = tmp_path / "custom-state.json"
    responses.get(BASE + "/api/radar/rollcalls", json={"rollcalls": [record()]})
    responses.get(BASE + "/api/radar/rollcalls", json={"rollcalls": [record()]})
    responses.put(BASE + "/api/rollcall/22/answer", json={})
    responses.get(BASE + "/api/radar/rollcalls", json={"rollcalls": [record(status="on_call")]})
    watch(Client(requests.Session()), config.load(path), once=True,
          auto_gps=True, event_dir=event_dir, state_file=state_file)
    assert json.loads(state_file.read_text(encoding="utf-8"))["activities"]["22"]["stage"] \
        == "confirmed"
    assert not (event_dir / "state.json").exists()


@responses.activate
def test_qr_token_failure_emits_action_required(tmp_path):
    path = write_config(tmp_path, "auto_qr=true\n")
    event_dir = tmp_path / "events"
    event_dir.mkdir()
    rec = record(is_radar=False, type="qr_rollcall")
    responses.get(BASE + "/api/radar/rollcalls", json={"rollcalls": [rec]})
    responses.get(BASE + "/api/course/11/rollcall/22/qr_code", status=403)
    watch(Client(requests.Session()), config.load(path), once=True,
          auto=True, event_dir=event_dir)
    assert all(call.request.method == "GET" for call in responses.calls)
    assert "action_required" in [event["stage"] for event in event_files(event_dir)]


def test_locations_json_and_text(capsys):
    main(["locations", "--json"])
    payload = json.loads(capsys.readouterr().out.strip())
    places = payload["places"]
    assert len(places) >= 50
    assert {"id", "name", "latitude", "longitude", "source"} <= set(places[0])
    main(["locations"])
    lines = [line for line in capsys.readouterr().out.splitlines() if line]
    assert len(lines) == len(places)
    assert "\t" in lines[0] and "(WGS84)" in lines[0]


@responses.activate
def test_watch_hot_reloads_config_in_same_process(tmp_path, monkeypatch):
    path = write_config(tmp_path, 'default_location=""\nauto_gps=false\n')
    event_dir = tmp_path / "events"
    event_dir.mkdir()
    rec = record()
    responses.get(BASE + "/api/radar/rollcalls", json={"rollcalls": [rec]})  # 第 1 轮
    responses.get(BASE + "/api/radar/rollcalls", json={"rollcalls": [rec]})  # 第 2 轮
    responses.get(BASE + "/api/radar/rollcalls", json={"rollcalls": [rec]})  # fresh
    responses.put(BASE + "/api/rollcall/22/answer", json={})
    responses.get(BASE + "/api/radar/rollcalls",
                  json={"rollcalls": [{**rec, "status": "on_call"}]})  # verify
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 1:
            write_config(tmp_path, f'default_location="{PLACE}"\n'
                                   "auto_gps=true\npoll_seconds=60\n")
        else:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", fake_sleep)
    with pytest.raises(KeyboardInterrupt):
        watch(Client(requests.Session()), config.load(path), auto=True,
              event_dir=event_dir, config_path=path)
    # 第一轮按旧配置只提醒；改文件后第二轮重载并提交，poll_seconds 也生效。
    assert sleeps == [15, 60]
    puts = [call for call in responses.calls if call.request.method == "PUT"]
    assert len(puts) == 1
    assert read_state(event_dir)["22"]["stage"] == "confirmed"


def test_refresh_config_updates_enabled_and_keeps_old_on_invalid(tmp_path, capsys):
    path = write_config(tmp_path, 'default_location=""\nauto_gps=false\n')
    cfg = config.load(path)
    stamp = config_stamp(path)
    assert automatic_channels(cfg, auto=True) == set()
    path.write_text(f'default_location="{PLACE}"\nauto_gps=true\npoll_seconds=60\n',
                    encoding="utf-8")
    cfg2, stamp2, changed = refresh_config(cfg, path, stamp)
    assert changed is True and cfg2["poll_seconds"] == 60
    assert automatic_channels(cfg2, auto=True) == {"GPS"}
    assert stamp2 == config_stamp(path)
    # 非法配置：不抛异常、保留旧 cfg、只告警一行。
    path.write_text('auto_gps="yes"\n', encoding="utf-8")
    cfg3, stamp3, changed = refresh_config(cfg2, path, stamp2)
    assert changed is False and cfg3 is cfg2 and cfg3["auto_gps"] is True
    errors = capsys.readouterr().err.strip().splitlines()
    assert len(errors) == 1 and "警告" in errors[0]
    # stamp 未变化时不再重复重载。
    cfg4, _, changed = refresh_config(cfg3, path, stamp3)
    assert changed is False and cfg4 is cfg3


@responses.activate
def test_watch_keeps_running_when_reload_invalid(tmp_path, monkeypatch, capsys):
    path = write_config(tmp_path, f'default_location="{PLACE}"\nauto_gps=false\n')
    event_dir = tmp_path / "events"
    event_dir.mkdir()
    rec = record()
    responses.get(BASE + "/api/radar/rollcalls", json={"rollcalls": [rec]})
    responses.get(BASE + "/api/radar/rollcalls", json={"rollcalls": [rec]})
    rounds = []

    def fake_sleep(seconds):
        rounds.append(seconds)
        if len(rounds) == 1:
            write_config(tmp_path, 'auto_gps="yes"\n')
        else:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", fake_sleep)
    with pytest.raises(KeyboardInterrupt):
        watch(Client(requests.Session()), config.load(path), auto=True,
              event_dir=event_dir, config_path=path)
    assert len(rounds) == 2
    assert all(call.request.method == "GET" for call in responses.calls)
    errors = capsys.readouterr().err.strip().splitlines()
    assert len(errors) == 1 and "警告" in errors[0]


def test_global_config_flag_before_subcommand(capsys, tmp_path):
    path = write_config(tmp_path, 'default_location=""\n')
    main(["--config", str(path), "config-json"])
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["auto_gps"] is False
    old = sys.stdin
    sys.stdin = io.StringIO(json.dumps({"auto_number": True}))
    try:
        main(["--config", str(path), "config-update"])
    finally:
        sys.stdin = old
    assert json.loads(capsys.readouterr().out.strip())["auto_number"] is True
    # 契约要求全局参数在子命令之前；放到后面应被 argparse 拒绝。
    with pytest.raises(SystemExit) as info:
        main(["config-check", "--config", str(path)])
    assert info.value.code == 2


# --------------------------------------------------------------------- ledger


def test_ledger_tolerates_corrupt_file(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")
    book = ledger.Ledger(path)
    assert book.load() == {}
    assert book.claim("22", course_id="11", channel="GPS") is True
    assert json.loads(path.read_text(encoding="utf-8"))["activities"]["22"]["stage"] == "submitting"
    assert not list(tmp_path.glob(".state-*"))


def test_ledger_dedupes_and_blocks_submitting(tmp_path):
    book = ledger.Ledger(tmp_path / "state.json")
    assert book.claim("22") is True
    assert book.claim("22") is False
    book.mark("22", "confirmed")
    assert book.claim("22") is False
    assert book.get("22")["stage"] == "confirmed"


def test_ledger_prunes_expired_and_caps_entries(tmp_path):
    path = tmp_path / "state.json"
    old = (datetime.now(timezone.utc) - timedelta(days=15)).isoformat()
    fresh = datetime.now(timezone.utc).isoformat()
    activities = {"old": {"stage": "confirmed", "updated_at": old}}
    for index in range(505):
        activities[f"r{index:03d}"] = {"stage": "confirmed", "updated_at": fresh}
    path.write_text(json.dumps({"version": 1, "activities": activities}), encoding="utf-8")
    loaded = ledger.Ledger(path).load()
    assert "old" not in loaded
    assert len(loaded) == 500
    # 写入时把清理结果落盘。
    ledger.Ledger(path).mark("new", "confirmed")
    stored = json.loads(path.read_text(encoding="utf-8"))["activities"]
    assert "old" not in stored and len(stored) <= 500
    if os.name == "posix":
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
