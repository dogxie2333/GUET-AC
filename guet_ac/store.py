"""登录会话存放在操作系统凭据库；不保存学校密码。"""

import hashlib
import json
import os
import stat
import tempfile
import uuid
from pathlib import Path

import keyring
import requests

from .auth import GuetError

SERVICE = "GUET-AC/session/v1"


def secure_backend():
    backend = keyring.get_keyring()
    module = type(backend).__module__
    if not module.startswith(("keyring.backends.Windows", "keyring.backends.macOS",
                              "keyring.backends.SecretService", "keyring.backends.kwallet")):
        raise GuetError("未找到支持的系统凭据库，请配置 Windows 凭据管理器或系统钥匙串。")


def session_file(account):
    directory = os.environ.get("GUET_AC_SESSION_DIR")
    if not directory:
        return None
    if os.name != "posix":
        raise GuetError("目录会话仅支持 Linux 等 POSIX 系统。")
    path = Path(directory).expanduser()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise GuetError("会话目录必须由当前用户拥有且权限为 0700。")
    return path / (hashlib.sha256(account.encode()).hexdigest() + ".json")


def read_session(path):
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "r", encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise GuetError("会话文件必须由当前用户拥有且权限为 0600。")
        return stream.read()


def save(account, session, device_id=None):
    path = session_file(account)
    cookies = [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path,
                "secure": c.secure, "expires": c.expires} for c in session.cookies
               if c.domain.lstrip(".") == "courses.guet.edu.cn"]
    payload = {"cookies": cookies, "device_id": device_id or str(uuid.uuid4())}
    if path is None:
        secure_backend()
        keyring.set_password(SERVICE, account, json.dumps(payload))
        return
    fd, name = tempfile.mkstemp(dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def load(account):
    path = session_file(account)
    if path is None:
        secure_backend()
        raw = keyring.get_password(SERVICE, account)
    else:
        try:
            raw = read_session(path)
        except FileNotFoundError:
            raw = None
    if not raw:
        raise GuetError("此账号尚未登录，请先运行 login。")
    data = json.loads(raw)
    session = requests.Session()
    for cookie in data["cookies"]:
        if cookie["domain"].lstrip(".") != "courses.guet.edu.cn":
            raise GuetError("会话中包含其他站点的 Cookie。")
        session.cookies.set_cookie(requests.cookies.create_cookie(**cookie))
    return session, data["device_id"]


def remove(account):
    path = session_file(account)
    if path is not None:
        path.unlink(missing_ok=True)
        return
    secure_backend()
    if keyring.get_password(SERVICE, account):
        keyring.delete_password(SERVICE, account)
