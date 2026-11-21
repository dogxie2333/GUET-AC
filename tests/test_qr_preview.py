"""Tests for scripts/guet_qr_preview.py and its JSON contract (docs/interfaces.md §2)."""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from time import monotonic

import pytest
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "guet_qr_preview.py"
BENCH = ROOT / "scripts" / "guet_qr_bench.py"
DECODERS = {"wxscan", "zxing", "detail", "aruco"}
STAGES = ["wxscan", "zxing", "detail", "aruco"]
# Decoder label -> function name in scripts/guet_qr_preview.py.
STAGE_FUNCTIONS = {"wxscan": "wxscan", "zxing": "zxing",
                   "detail": "detail_pass", "aruco": "aruco_pass"}

pytest.importorskip("qrcode")
import qrcode  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _env(extra_paths=()):
    paths = [str(path) for path in extra_paths] + [str(ROOT)]
    if os.environ.get("PYTHONPATH"):
        paths.append(os.environ["PYTHONPATH"])
    return {**os.environ, "PYTHONPATH": os.pathsep.join(paths),
            "PYTHONUTF8": "1"}


def run_preview(image, *args, extra_paths=(), timeout=120):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args, str(image)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, env=_env(extra_paths),
    )


def run_json(image, *args, **kwargs):
    return run_preview(image, "--json", *args, **kwargs)


def load_preview():
    spec = importlib.util.spec_from_file_location("guet_qr_preview_tests", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def preview():
    return load_preview()


@pytest.fixture(scope="module")
def payload_text():
    return json.dumps({"courseId": "11", "rollcallId": "22",
                       "data": "token-abc"}, separators=(",", ":"))


def qr_image(text, box_size=8, border=4):
    code = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=box_size,
        border=border,
    )
    code.add_data(text)
    code.make(fit=True)
    return code.make_image(fill_color="black", back_color="white").convert("RGB")


def save_qr(path, text, box_size=8):
    image = qr_image(text, box_size)
    image.save(path)
    return path


def save_photo_without_qr(path):
    image = Image.new("RGB", (640, 480), (30, 32, 36))
    draw = ImageDraw.Draw(image)
    draw.rectangle((40, 40, 300, 210), fill=(62, 64, 72))
    draw.rectangle((360, 60, 600, 260), fill=(44, 46, 52))
    for x in range(0, 640, 32):
        draw.line((x, 320, x, 460), fill=(92, 92, 98), width=2)
    image.save(path)
    return path


def save_two_codes(path, first_text, second_text):
    first = qr_image(first_text)
    second = qr_image(second_text)
    canvas = Image.new(
        "RGB", (first.width + second.width + 80,
                max(first.height, second.height) + 80),
        "white",
    )
    canvas.paste(first, (20, 20))
    canvas.paste(second, (first.width + 60, 20))
    canvas.save(path)
    return path


def json_line(result):
    lines = result.stdout.strip().splitlines()
    assert len(lines) == 1, f"expected one stdout line, got {lines!r}"
    return json.loads(lines[0])


# ---------------------------------------------------------------------------
# Success paths
# ---------------------------------------------------------------------------

def test_json_success_contract(tmp_path, payload_text):
    result = run_json(save_qr(tmp_path / "ok.png", payload_text))
    assert result.returncode == 0
    assert result.stderr == ""
    data = json_line(result)
    assert data["ok"] is True
    assert set(data) == {"ok", "decoder", "payload"}
    assert data["decoder"] in DECODERS
    assert data["payload"]["courseId"] == "11"
    assert data["payload"]["rollcallId"] == "22"
    assert data["payload"]["data"] == "token-abc"


def test_json_compact_payload_keeps_control_bytes(tmp_path):
    """Real TronClass codes use \\x10/\\x1f/\\x1e; ZXing's display text mangles them."""
    text = "0~\x10b!4~\x10m!3~token\x1fpart\x1emore"
    result = run_json(save_qr(tmp_path / "compact.png", text))
    assert result.returncode == 0, result.stdout + result.stderr
    data = json_line(result)
    assert data["payload"]["courseId"] == "11"
    assert data["payload"]["rollcallId"] == "22"
    assert data["payload"]["data"] == "token~part!more"


def test_accept_rejects_escaped_wxscan_text_and_uses_zxing(tmp_path, preview,
                                                           monkeypatch):
    """A wxscan display-escaped compact code must not block the zxing rescue."""
    compact = "0~\x10b!4~\x10m!3~token\x1fpart\x1emore"
    escaped = compact.replace("\x10", "<DLE>").replace("\x1f", "<US>").replace("\x1e", "<RS>")
    path = save_qr(tmp_path / "compact.png", compact)
    monkeypatch.setattr(preview, "wxscan", lambda *args, **kwargs: [escaped])
    monkeypatch.setattr(preview, "zxing", lambda *args, **kwargs: [compact])
    seen = []

    def accept(text):
        seen.append(text)
        preview.parse_payload(text)

    text, decoder = preview.decode(path, budget=10, accept=accept)
    assert decoder == "zxing"
    assert text == compact
    # The rejected escaped candidate must not leak through the error path.
    assert seen == [escaped, compact]


def test_accept_keeps_parse_failure_when_no_stage_validates(tmp_path, preview,
                                                           monkeypatch):
    path = save_qr(tmp_path / "bad.png", "not-a-rollcall")
    for name in ("wxscan", "zxing", "detail_pass", "aruco_pass"):
        monkeypatch.setattr(preview, name, lambda *args, **kwargs: ["not-a-rollcall"])

    def accept(text):
        preview.parse_payload(text)

    with pytest.raises(preview.Failure) as info:
        preview.decode(path, budget=10, accept=accept)
    assert (info.value.stage, info.value.error) == ("parse", "invalid_payload")


def test_plain_output_stays_compatible(tmp_path, payload_text):
    result = run_preview(save_qr(tmp_path / "plain.png", payload_text))
    assert result.returncode == 0
    assert result.stdout.strip() == "已识别一个二维码。"
    # The legacy text mode must never print the payload.
    assert "token-abc" not in result.stdout + result.stderr


# ---------------------------------------------------------------------------
# Decode failures
# ---------------------------------------------------------------------------

def test_json_no_qr(tmp_path):
    result = run_json(save_photo_without_qr(tmp_path / "photo.png"))
    assert result.returncode == 2
    data = json_line(result)
    assert data["ok"] is False
    assert data["stage"] == "decode"
    assert data["error"] == "no_qr"
    assert isinstance(data["message"], str) and data["message"]


def test_json_multiple_qr(tmp_path, payload_text):
    path = save_two_codes(tmp_path / "two.png", payload_text, json.dumps(
        {"courseId": "12", "rollcallId": "23", "data": "token-two"},
        separators=(",", ":")))
    result = run_json(path)
    assert result.returncode == 2
    data = json_line(result)
    assert data["stage"] == "decode"
    assert data["error"] == "multiple_qr"
    assert "token" not in result.stdout


def test_json_invalid_payload(tmp_path):
    path = save_qr(tmp_path / "unrelated.png",
                   "https://example.com/not-a-rollcall?q=1")
    result = run_json(path)
    assert result.returncode == 2
    data = json_line(result)
    assert data["stage"] == "parse"
    assert data["error"] == "invalid_payload"
    # Neither the payload nor its URL may leak through stdout/stderr.
    assert "example.com" not in result.stdout + result.stderr


def test_plain_failure_exit_code_stays_compatible(tmp_path):
    result = run_preview(save_photo_without_qr(tmp_path / "photo.png"))
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr.strip().startswith("识别失败：")


# ---------------------------------------------------------------------------
# Image failures
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["corrupt", "missing"])
def test_json_unreadable_image(tmp_path, kind):
    if kind == "corrupt":
        path = tmp_path / "broken.png"
        path.write_bytes(b"this is not a PNG file")
    else:
        path = tmp_path / "absent.png"
    result = run_json(path)
    assert result.returncode == 2
    data = json_line(result)
    assert data["stage"] == "image"
    assert data["error"] == "unreadable"


def test_json_file_too_large(tmp_path):
    path = tmp_path / "huge.png"
    path.write_bytes(b"\x00" * (8 * 1024 * 1024 + 1))
    result = run_json(path)
    assert result.returncode == 2
    data = json_line(result)
    assert data["stage"] == "image"
    assert data["error"] == "too_large"


def test_json_pixels_too_large(tmp_path):
    path = tmp_path / "wide.png"
    Image.new("RGB", (5000, 4000), "white").save(path)  # 20M pixels, small file
    result = run_json(path)
    assert result.returncode == 2
    data = json_line(result)
    assert data["stage"] == "image"
    assert data["error"] == "pixels_too_large"


# ---------------------------------------------------------------------------
# Aruco fallback (cv2 optional)
# ---------------------------------------------------------------------------

def test_missing_cv2_skips_aruco(tmp_path, payload_text):
    fake = tmp_path / "fake"
    fake.mkdir()
    (fake / "cv2.py").write_text(
        "raise ImportError('cv2 disabled for this test')\n", encoding="utf-8")
    result = run_json(save_qr(tmp_path / "ok.png", payload_text),
                      extra_paths=[fake])
    assert result.returncode == 0
    data = json_line(result)
    assert data["ok"] is True
    assert data["payload"]["data"] == "token-abc"


def test_aruco_fallback_actually_decodes(tmp_path, payload_text, preview,
                                         monkeypatch):
    cv2 = pytest.importorskip("cv2")
    if not hasattr(cv2, "QRCodeDetectorAruco"):
        pytest.skip("cv2.QRCodeDetectorAruco is unavailable")
    path = save_qr(tmp_path / "aruco.png", payload_text)
    image = preview._load(path)
    decoded = preview.aruco_pass(image, monotonic() + 4.2)
    assert payload_text in decoded
    # Expired deadline must return [] rather than blocking the chain.
    assert preview.aruco_pass(image, monotonic() - 1) == []
    # Force the chain past wxscan/zxing/detail to prove the fallback routing.
    for name in ("wxscan", "zxing", "detail_pass"):
        monkeypatch.setattr(preview, name, lambda *args, **kwargs: [])
    text, decoder = preview.decode(path, budget=10)
    assert decoder == "aruco"
    assert text == payload_text


def test_aruco_detector_errors_are_swallowed(tmp_path, payload_text, preview,
                                             monkeypatch):
    class Boom:
        def detect(self, *args, **kwargs):
            raise RuntimeError("simulated detector failure")

    fake_cv2 = type(sys)("cv2")
    fake_cv2.QRCodeDetectorAruco = Boom
    monkeypatch.setitem(sys.modules, "cv2", fake_cv2)
    path = save_qr(tmp_path / "boom.png", payload_text)
    image = preview._load(path)
    assert preview.aruco_pass(image, monotonic() + 4.2) == []
    text, decoder = preview.decode(path, budget=10)
    assert decoder in {"wxscan", "zxing", "detail"}
    assert text == payload_text


def test_scoped_aruco_worker_standalone(tmp_path, payload_text):
    pytest.importorskip("cv2")
    path = save_qr(tmp_path / "worker.png", payload_text)
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "guet_aruco_worker.py"),
         str(path), "4"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=60, env=_env(),
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout.strip().splitlines()[-1])
    assert data["ok"] is True
    assert payload_text in data["texts"]


def test_scoped_aruco_uses_systemd_run_and_never_logs_payload(preview, monkeypatch):
    calls = []

    class Completed:
        stdout = b'{"ok":true,"texts":["candidate"]}'

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return Completed()

    monkeypatch.setattr(preview.shutil, "which", lambda name: "/usr/bin/systemd-run")
    monkeypatch.setattr(preview.subprocess, "run", fake_run)
    assert preview._scoped_aruco(Path("/tmp/image.png"), monotonic() + 3) == ["candidate"]
    command, kwargs = calls[0]
    assert command[0] == "/usr/bin/systemd-run"
    assert "--user" in command and "--scope" in command
    assert kwargs.get("stderr") == preview.subprocess.DEVNULL
    # Non-ok / malformed worker output must degrade to an empty fallback.
    Completed.stdout = b'{"ok":false}'
    assert preview._scoped_aruco(Path("/tmp/image.png"), monotonic() + 3) == []
    Completed.stdout = b"not json"
    assert preview._scoped_aruco(Path("/tmp/image.png"), monotonic() + 3) == []


def test_aruco_scope_is_opted_in_by_environment(preview, monkeypatch):
    calls = []
    monkeypatch.setenv("GUET_AC_ARUCO_SCOPE", "1")
    monkeypatch.setattr(preview, "_scoped_aruco",
                        lambda path, deadline: calls.append(path) or ["scoped"])
    image = Image.new("L", (10, 10), 255)
    assert preview.aruco_pass(image, monotonic() + 3, image_path=Path("/tmp/x.png")) == ["scoped"]
    assert calls == [Path("/tmp/x.png")]
    # Desktop/tests without the env var keep the in-process path.
    monkeypatch.delenv("GUET_AC_ARUCO_SCOPE")
    assert preview.aruco_pass(Image.new("L", (10, 10), 255), monotonic() + 3) == []


def test_deadline_exhausted_is_timeout(tmp_path, payload_text, preview):
    path = save_qr(tmp_path / "late.png", payload_text)
    with pytest.raises(preview.Failure) as info:
        preview.decode(path, budget=-1)
    assert info.value.stage == "decode"
    assert info.value.error == "timeout"


# ---------------------------------------------------------------------------
# Decoder order / attribution / error vocabulary
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("target", STAGES)
def test_decoder_order_and_attribution(tmp_path, payload_text, preview,
                                       monkeypatch, target):
    path = save_qr(tmp_path / f"{target}.png", payload_text)
    calls = []
    for name in STAGES:
        result = [payload_text] if name == target else []

        def stage(*args, _name=name, _result=result, **kwargs):
            calls.append(_name)
            return list(_result)

        monkeypatch.setattr(preview, STAGE_FUNCTIONS[name], stage)
    text, decoder = preview.decode(path, budget=10)
    assert decoder == target
    assert text == payload_text
    # Only the winning stage and its predecessors may run.
    assert calls == STAGES[:STAGES.index(target) + 1]


def test_all_stages_fail_reports_no_qr(tmp_path, payload_text, preview,
                                       monkeypatch):
    path = save_qr(tmp_path / "chain.png", payload_text)
    calls = []

    def empty(name):
        def stage(*args, **kwargs):
            calls.append(name)
            return []

        return stage

    for name in STAGES:
        monkeypatch.setattr(preview, STAGE_FUNCTIONS[name], empty(name))
    with pytest.raises(preview.Failure) as info:
        preview.decode(path, budget=10)
    assert calls == STAGES
    assert (info.value.stage, info.value.error) == ("decode", "no_qr")


def test_error_vocabulary_is_fixed(preview):
    assert preview.ERRORS == {
        "unreadable", "too_large", "pixels_too_large", "no_qr", "multiple_qr",
        "invalid_payload", "timeout", "internal",
    }


def test_json_failure_shape(tmp_path):
    result = run_json(save_photo_without_qr(tmp_path / "photo.png"))
    data = json_line(result)
    assert set(data) == {"ok", "stage", "error", "message"}
    assert data["stage"] in {"image", "decode", "parse"}


# ---------------------------------------------------------------------------
# Bench / synthetic fixtures
# ---------------------------------------------------------------------------

def test_bench_make_synthetic_without_payload_leak(tmp_path):
    output = tmp_path / "fixtures"
    result = subprocess.run(
        [sys.executable, str(BENCH), "--make-synthetic", str(output)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=180, env=_env(),
    )
    assert result.returncode == 0, result.stderr
    created = sorted(path.name for path in output.glob("*.png"))
    assert len(created) == 6
    assert "synthetic-clean.png" in created
    assert "synthetic-no-qr.png" in created
    assert "generated 6 synthetic images" in result.stdout
    assert "total 6 images" in result.stdout
    # The benchmark reports counts only; payload tokens must never appear.
    assert "guet-qr-bench-token" not in result.stdout + result.stderr
    assert "token" not in result.stdout
