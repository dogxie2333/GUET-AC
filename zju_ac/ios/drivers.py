"""Explicit, elevated Apple-component bootstrap using signed official packages."""

import base64
import ctypes
import hashlib
import os
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

APPLE_URL = "https://www.apple.com/itunes/download/win64"
USB_URL = (
    "https://catalog.s.download.windowsupdate.com/d/msdownload/update/driver/drvs/2023/10/"
    "5fb262ea-d52d-46a7-9361-f3260ba57a1a_3e8075a4dded0a795131f82285e2f1a06525ebc2.cab"
)
USB_SHA1 = "3e8075a4dded0a795131f82285e2f1a06525ebc2"


def components_present() -> bool:
    if os.name != "nt":
        return False
    import winreg

    base = "SYSTEM\\CurrentControlSet\\Services\\"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base + "Apple Mobile Device Service"):
            pass
        for name in ("AppleKmdfFilter", "usbaapl64"):
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base + name):
                    return True
            except OSError:
                continue
    except OSError:
        pass
    return False


def run(args: list, accepted=(0,), timeout=180):
    result = subprocess.run(
        [str(arg) for arg in args],
        capture_output=True,
        timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode not in accepted:
        raise RuntimeError(f"驱动组件操作失败（退出码 {result.returncode}）。")
    return result.returncode


def verify_signature(path: Path, publisher: str):
    encoded = base64.b64encode(str(path).encode("utf-8")).decode("ascii")
    script = (
        f"$p=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded}'));"
        "$s=Get-AuthenticodeSignature -LiteralPath $p;"
        f"if($s.Status -ne 'Valid' -or $s.SignerCertificate.Subject -notlike '*{publisher}*')"
        "{exit 1};exit 0"
    )
    code = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    run(
        [
            str(Path(os.environ["WINDIR"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"),
            "-NoProfile",
            "-NonInteractive",
            "-EncodedCommand",
            code,
        ],
        timeout=60,
    )


def download(url: str, target: Path):
    # Direct first, then the configured proxy. Neither path disables TLS validation.
    for direct in (True, False):
        opener = (
            urllib.request.build_opener(urllib.request.ProxyHandler({}))
            if direct
            else urllib.request.build_opener()
        )
        try:
            with opener.open(url, timeout=45) as response, target.open("wb") as output:
                size = 0
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > 512 * 1024 * 1024:
                        raise RuntimeError("驱动下载超出预期大小。")
                    output.write(chunk)
            return
        except OSError:
            target.unlink(missing_ok=True)
    raise RuntimeError("驱动下载失败，请检查网络后重试。")


def install(directory: Path) -> int:
    directory.mkdir(parents=True, exist_ok=True)
    system = Path(os.environ["WINDIR"]) / "System32"
    if components_present():
        run([system / "sc.exe", "start", "Apple Mobile Device Service"], accepted=(0, 1056))
        return 0
    restart = False
    with tempfile.TemporaryDirectory(prefix="apple-", dir=directory) as temporary:
        folder = Path(temporary)
        installer = folder / "iTunes64Setup.exe"
        download(APPLE_URL, installer)
        verify_signature(installer, "O=Apple Inc.")
        run([system / "tar.exe", "-xf", installer, "-C", folder, "AppleMobileDeviceSupport64.msi"])
        msi = folder / "AppleMobileDeviceSupport64.msi"
        verify_signature(msi, "O=Apple Inc.")
        restart = (
            run(
                [
                    system / "msiexec.exe",
                    "/i",
                    msi,
                    "/qn",
                    "/norestart",
                    "/L*v",
                    directory / "apple-msi.log",
                ],
                accepted=(0, 3010),
            )
            == 3010
        )
        cab = folder / "AppleUSB.cab"
        download(USB_URL, cab)
        if hashlib.sha1(cab.read_bytes()).hexdigest() != USB_SHA1:
            raise RuntimeError("Apple USB 驱动校验失败，已停止安装。")
        extracted = folder / "usb"
        extracted.mkdir()
        run([system / "expand.exe", "-F:*", cab, extracted])
        verify_signature(
            extracted / "appleusb.cat", "Microsoft Windows Hardware Compatibility Publisher"
        )
        restart = (
            run(
                [system / "pnputil.exe", "/add-driver", extracted / "AppleUsb.inf", "/install"],
                accepted=(0, 3010),
            )
            == 3010
        ) or restart
        run([system / "sc.exe", "start", "Apple Mobile Device Service"], accepted=(0, 1056))
    return 3010 if restart else 0


def main() -> int:
    from ..desktop.storage import default_directory
    from ..diagnostics import record, setup

    setup(default_directory() / "logs")
    record("ios_started", operation="drivers")
    try:
        if os.name != "nt" or not ctypes.windll.shell32.IsUserAnAdmin():
            raise RuntimeError("Apple 驱动安装需要 Windows 管理员授权。")
        if getattr(sys, "frozen", False):
            # This helper runs before Qt. Do not pass PyInstaller's DLL search
            # directory to Windows system installers and signature tools.
            ctypes.windll.kernel32.SetDllDirectoryW(None)
        result = install(default_directory() / "ios/driver-logs")
        message = "Apple USB 组件已准备完成，请重新检测设备。"
        if result == 3010:
            message = "Apple USB 组件已安装，请重启 Windows 后检测设备。"
    except Exception as exc:
        record("ios_error", operation="drivers", error=exc)
        result, message = (
            1,
            "Apple USB 组件安装未完成。请检查网络、管理员授权及 data/ios/driver-logs。",
        )
    if os.name == "nt":
        ctypes.windll.user32.MessageBoxW(
            None, message, "ZJU-AC · Apple USB 组件", 0x10 if result == 1 else 0x40
        )
    record("ios_finished", operation="drivers", outcome="failure" if result == 1 else "success")
    return result


def launch_installer() -> int:
    """Return a process handle; Windows itself presents the administrator prompt."""
    if os.name != "nt":
        raise RuntimeError("驱动安装目前仅支持 Windows。")
    from ctypes import wintypes as w

    class ExecuteInfo(ctypes.Structure):
        _fields_ = [
            ("cbSize", w.DWORD),
            ("fMask", w.ULONG),
            ("hwnd", w.HWND),
            ("lpVerb", w.LPCWSTR),
            ("lpFile", w.LPCWSTR),
            ("lpParameters", w.LPCWSTR),
            ("lpDirectory", w.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", w.HINSTANCE),
            ("lpIDList", w.LPVOID),
            ("lpClass", w.LPCWSTR),
            ("hkeyClass", w.HKEY),
            ("dwHotKey", w.DWORD),
            ("hIcon", w.HANDLE),
            ("hProcess", w.HANDLE),
        ]

    args = ["--install-apple-drivers"]
    folder = Path(sys.executable).resolve().parent
    if not getattr(sys, "frozen", False):
        args = ["-m", "zju_ac.desktop", *args]
        folder = Path(__file__).resolve().parents[2]
    info = ExecuteInfo()
    info.cbSize, info.fMask = ctypes.sizeof(info), 0x40
    info.lpVerb, info.lpFile = "runas", sys.executable
    info.lpParameters, info.lpDirectory, info.nShow = subprocess.list2cmdline(args), str(folder), 0
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    shell.ShellExecuteExW.argtypes = [ctypes.POINTER(ExecuteInfo)]
    shell.ShellExecuteExW.restype = w.BOOL
    if not shell.ShellExecuteExW(ctypes.byref(info)):
        raise RuntimeError("未启动驱动安装，请确认 Windows 管理员授权。")
    if not info.hProcess:
        raise RuntimeError("未取得驱动安装进程，请稍后重新检测组件。")
    return info.hProcess


def installation_result(handle: int) -> int | None:
    from ctypes import wintypes as w

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetExitCodeProcess.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
    kernel.GetExitCodeProcess.restype = w.BOOL
    code = w.DWORD()
    if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
        raise RuntimeError("无法读取驱动安装进度。")
    return None if code.value == 259 else code.value


def close_handle(handle: int):
    from ctypes import wintypes as w

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.CloseHandle.restype = w.BOOL
    kernel.CloseHandle(handle)
