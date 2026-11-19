"""CAS/Keycloak form authentication over HTTPS, with no browser or stored credentials."""

import re
import time
from contextlib import contextmanager
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup, Tag

from . import __version__
from .client import CoursesClient
from .errors import ProtocolError, TemporaryError, ZjuError
from .network import configure_direct_session, create_direct_session

AUTH_HOSTS = {"courses.guet.edu.cn", "cas.guet.edu.cn", "identity.guet.edu.cn"}
REDIRECTS = {301, 302, 303, 307, 308}


def trusted_login_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return (
            parsed.scheme == "https"
            and parsed.hostname in AUTH_HOSTS
            and parsed.port in {None, 443}
            and not parsed.username
            and not parsed.password
        )
    except ValueError:
        return False


def checked_url(base: str, target: str) -> str:
    url = urljoin(base, target)
    if not trusted_login_url(url):
        raise ProtocolError("认证跳转或表单地址不可信，已停止登录。")
    return url


def encrypt_cas_password(password: str, key: dict) -> str:
    """Implement the legacy CAS frontend's raw RSA protocol (not PKCS#1)."""
    try:
        modulus = int(key["modulus"], 16)
        exponent = int(key["exponent"], 16)
        value = int.from_bytes(password.encode("utf-8"), "big")
        if modulus <= value or exponent <= 1 or modulus.bit_length() < 512:
            raise ValueError("Invalid key or oversized password")
    except (KeyError, TypeError, ValueError) as exc:
        raise ProtocolError("统一认证返回的 RSA 公钥无效，或密码长度超出协议限制。") from exc
    width = (modulus.bit_length() + 7) // 8 * 2
    return format(pow(value, exponent, modulus), f"0{width}x")


def form_values(form: Tag) -> dict[str, str]:
    """Preserve server hidden fields without executing page JavaScript."""
    return {
        str(field["name"]): str(field.get("value", ""))
        for field in form.select("input[name]")
        if field.get("type", "text").lower() == "hidden" and not field.has_attr("disabled")
    }


class HttpAuthenticator:
    def __init__(self, session: requests.Session, timeout: float, otp_reader=None):
        self.session = configure_direct_session(session)
        self.deadline = time.monotonic() + timeout
        self.otp_reader = otp_reader

    def request(self, method: str, url: str, **kwargs) -> requests.Response:
        checked_url(url, url)
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ZjuError("登录超时，请检查网络后重试。")
        try:
            response = self.session.request(
                method,
                url,
                timeout=min(15, remaining),
                allow_redirects=False,
                **kwargs,
            )
        except requests.RequestException as exc:
            raise TemporaryError("认证直连请求失败，请检查网络或校园网连接。") from exc
        if response.status_code >= 400:
            status = response.status_code
            response.close()
            if status == 429 or status >= 500:
                raise TemporaryError(f"认证服务暂时不可用（HTTP {status}）。")
            raise ZjuError(f"认证服务拒绝请求（HTTP {status}）。")
        return response

    def follow(self, response: requests.Response, posted: bool = False) -> requests.Response:
        for _ in range(20):
            try:
                if response.status_code in REDIRECTS:
                    if posted and response.status_code in {307, 308}:
                        raise ProtocolError("认证要求重新发送密码，已停止以避免重复提交。")
                    location = response.headers.get("location")
                    if not location:
                        raise ProtocolError("认证跳转缺少目标地址。")
                else:
                    soup = BeautifulSoup(response.text, "html.parser")
                    refresh = soup.find("meta", attrs={"http-equiv": re.compile("^refresh$", re.I)})
                    if refresh is None:
                        return response
                    match = re.search(r"url\s*=\s*(.+)$", str(refresh.get("content", "")), re.I)
                    if not match:
                        raise ProtocolError("无法解析认证页面跳转。")
                    location = match[1].strip().strip("\"'")
                target = checked_url(response.url, location)
            except BaseException:
                response.close()
                raise
            response.close()
            response = self.request("GET", target)
            posted = False
        response.close()
        raise ProtocolError("认证跳转次数过多，已停止登录。")

    def authenticate(self, student_id: str, password: str) -> None:
        from .guet_auth import login as guet_login
        guet_login(student_id, password, captcha_reader=read_captcha, session=self.session)


@contextmanager
def login(student_id: str, password: str, login_timeout: int = 180, *, otp_reader=None):
    session = create_direct_session()
    session.headers.update(
        {
            "User-Agent": f"ZJU-AC/{__version__}",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
    )
    try:
        print("正在通过 HTTPS 直连登录统一身份认证（忽略环境及系统代理）……", flush=True)
        if otp_reader is None:
            authenticator = HttpAuthenticator(session, login_timeout)
        else:
            authenticator = HttpAuthenticator(session, login_timeout, otp_reader)
        authenticator.authenticate(student_id, password)
        password = ""
        print(
            "登录成功，开始监控签到。Ctrl+C 退出。" if otp_reader is None else "登录成功。",
            flush=True,
        )
        yield CoursesClient(session)
    finally:
        session.cookies.clear()
        session.close()


def read_captcha(image):
    from PySide6.QtGui import QPixmap
    from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QLabel, QLineEdit, QVBoxLayout

    application = QApplication.instance() or QApplication([])
    dialog = QDialog()
    dialog.setWindowTitle("桂电统一身份认证")
    layout = QVBoxLayout(dialog)
    picture = QLabel()
    pixmap = QPixmap()
    pixmap.loadFromData(image)
    picture.setPixmap(pixmap)
    layout.addWidget(picture)
    field = QLineEdit()
    field.setPlaceholderText("请输入图形验证码")
    layout.addWidget(field)
    buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    field.setFocus()
    if dialog.exec() != QDialog.Accepted:
        raise ZjuError("已取消桂电验证码登录。")
    application.processEvents()
    return field.text().strip()
