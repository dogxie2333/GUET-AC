"""登录会话存放在操作系统凭据库；不保存学校密码。"""

import json
import uuid

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


def save(account, session, device_id=None):
    secure_backend()
    cookies = [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path,
                "secure": c.secure, "expires": c.expires} for c in session.cookies
               if c.domain.lstrip(".") == "courses.guet.edu.cn"]
    payload = {"cookies": cookies, "device_id": device_id or str(uuid.uuid4())}
    keyring.set_password(SERVICE, account, json.dumps(payload))


def load(account):
    secure_backend()
    raw = keyring.get_password(SERVICE, account)
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
    secure_backend()
    if keyring.get_password(SERVICE, account):
        keyring.delete_password(SERVICE, account)

