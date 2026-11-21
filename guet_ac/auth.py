"""桂电 CAS authserver 协议：动态 salt、AES-CBC、Cookie 跳转。"""

import base64
import secrets
import string
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

BASE = "https://courses.guet.edu.cn"
CAS = "https://cas.guet.edu.cn"
HOSTS = {"courses.guet.edu.cn", "identity.guet.edu.cn", "cas.guet.edu.cn"}


class GuetError(Exception):
    """用户可读错误。stage 由调用方按 parse/session/network/submit/verify 标注。"""

    stage = None


def checked_url(url):
    parts = urlsplit(url)
    if (parts.scheme != "https" or parts.hostname not in HOSTS
            or parts.port not in (None, 443) or parts.username or parts.password):
        raise GuetError("认证跳转地址不属于桂电认证站点。")
    return url


def follow(session, method, url, **kwargs):
    """逐跳校验目标，避免把凭据带到非认证站点。"""
    for _ in range(15):
        try:
            response = session.request(method, checked_url(url), timeout=20,
                                       allow_redirects=False, **kwargs)
        except requests.RequestException as exc:
            raise GuetError("桂电认证连接失败，请检查网络。") from exc
        if response.status_code not in (301, 302, 303, 307, 308):
            if response.status_code == 401:
                response.close()
                raise GuetError("桂电认证未通过，请检查账号、密码和验证码。")
            if not 200 <= response.status_code < 300:
                status = response.status_code
                response.close()
                raise GuetError(f"桂电认证返回 HTTP {status}。")
            return response
        target = checked_url(urljoin(response.url, response.headers["Location"]))
        status = response.status_code
        response.close()
        if status in (307, 308) and method != "GET":
            raise GuetError("认证服务要求重发登录表单，请重新登录。")
        method, url, kwargs = "GET", target, {}
    raise GuetError("认证跳转次数过多。")


def encrypt_password(password, salt, *, prefix=None, iv=None):
    """匹配桂电 encrypt.js：随机 64 字符前缀 + 密码，AES-CBC/PKCS7。"""
    key = salt.strip().encode()
    if len(key) not in (16, 24, 32):
        raise GuetError("桂电认证页面的密码加密 salt 无效。")
    prefix = prefix if prefix is not None else "".join(
        secrets.choice(string.ascii_letters + string.digits) for _ in range(64))
    iv = iv if iv is not None else "".join(
        secrets.choice(string.ascii_letters + string.digits) for _ in range(16)).encode()
    padder = padding.PKCS7(128).padder()
    plaintext = padder.update((prefix + password).encode()) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return base64.b64encode(encryptor.update(plaintext) + encryptor.finalize()).decode()


def password_form(html):
    soup = BeautifulSoup(html, "html.parser")
    form = soup.select_one("form#pwdFromId")
    if form is None or form.select_one("#pwdEncryptSalt") is None:
        raise GuetError("桂电认证页面未包含账号密码登录表单。")
    if str(form.get("method", "")).lower() != "post":
        raise GuetError("桂电认证表单不是 POST。")
    return form


def login(username, password, captcha_reader=None, session=None):
    session = session or requests.Session()
    session.headers.update({"User-Agent": "GUET-AC/0.1", "Accept-Language": "zh-CN"})
    response = follow(session, "GET", BASE + "/user/index")
    try:
        form = password_form(response.text)
        if urlsplit(response.url).hostname != "cas.guet.edu.cn":
            raise GuetError("密码表单未来自桂电 CAS。")
        target = checked_url(urljoin(response.url, form.get("action", "")))
        if urlsplit(target).hostname != "cas.guet.edu.cn":
            raise GuetError("密码表单提交地址未指向桂电 CAS。")
        data = {i["name"]: i.get("value", "") for i in form.select('input[type="hidden"][name]')}
        salt = form.select_one("#pwdEncryptSalt").get("value", "")
        referer = response.url
        service = parse_qs(urlsplit(referer).query).get("service")
        if service:
            parts = urlsplit(target)
            query = parse_qs(parts.query)
            query["service"] = service
            target = urlunsplit(parts._replace(query=urlencode(query, doseq=True)))
    finally:
        response.close()
    with session.get(CAS + "/authserver/bfp/info",
                     params={"bfp": secrets.token_hex(16).upper()}, timeout=20) as fingerprint:
        fingerprint.raise_for_status()
    with session.get(CAS + "/authserver/checkNeedCaptcha.htl",
                     params={"username": username}, timeout=20) as check:
        check.raise_for_status()
        need = check.json().get("isNeed")
        if not isinstance(need, bool):
            raise GuetError("验证码检查响应格式改变。")
    captcha = ""
    if need:
        if captcha_reader is None:
            raise GuetError("该账号需要图形验证码，请使用交互式登录。")
        with session.get(CAS + "/authserver/getCaptcha.htl", timeout=20) as image:
            image.raise_for_status()
            captcha = captcha_reader(image.content).strip()
        if not captcha:
            raise GuetError("验证码为空，已取消登录。")
    data.update(username=username, password=encrypt_password(password, salt),
                captcha=captcha, cllt="userNameLogin", dllt="generalLogin", _eventId="submit")
    data.pop("passwordText", None)
    response = follow(session, "POST", target, data=data, headers={"Referer": referer})
    response.close()
    # 必须以业务 API 验证登录，不能把认证页面 HTTP 200 当作成功。
    from .client import Client
    Client(session).profile()
    return session
