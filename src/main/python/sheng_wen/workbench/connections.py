from __future__ import annotations
import ctypes, json, os, threading
from pathlib import Path
from http.cookies import SimpleCookie
from datetime import datetime, timezone

ALLOWED = {
    "SESSDATA",
    "bili_jct",
    "DedeUserID",
    "DedeUserID__ckMd5",
    "buvid3",
    "buvid4",
    "ac_time_value",
}


def parse_cookies(text: str) -> dict[str, str]:
    text = text.strip()
    if not text:
        raise ValueError("请填写登录凭据")
    result = {}
    if "\t" in text:
        for line in text.splitlines():
            line = line.removeprefix("#HttpOnly_")
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) == 7 and (
                parts[0].lstrip(".") == "bilibili.com"
                or parts[0].endswith(".bilibili.com")
            ):
                if parts[5] in ALLOWED:
                    result[parts[5]] = parts[6]
    elif "=" in text:
        jar = SimpleCookie()
        jar.load(text)
        result = {k: v.value for k, v in jar.items() if k in ALLOWED}
    else:
        result = {"SESSDATA": text}
    if not result.get("SESSDATA"):
        raise ValueError("没有找到 B 站 SESSDATA；请检查导入内容")
    if any(any(c in v for c in "\r\n\t") for v in result.values()):
        raise ValueError("凭据包含非法换行")
    return result


class Blob(ctypes.Structure):
    _fields_ = [("size", ctypes.c_ulong), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def protect(data: bytes, decrypt=False) -> bytes:
    if os.name != "nt":
        raise RuntimeError(
            "此版本的持久化凭据存储使用 Windows DPAPI；其他系统请通过环境变量提供凭据"
        )
    buf = ctypes.create_string_buffer(data)
    src = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    dst = Blob()
    fn = (
        ctypes.windll.crypt32.CryptUnprotectData
        if decrypt
        else ctypes.windll.crypt32.CryptProtectData
    )
    if not fn(ctypes.byref(src), None, None, None, None, 1, ctypes.byref(dst)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(dst.data, dst.size)
    finally:
        ctypes.windll.kernel32.LocalFree(dst.data)


class Connection:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        self.data = {"cookies": {}, "status": "disconnected", "use_env": True}
        if path.exists():
            self.data = json.loads(protect(path.read_bytes(), True))

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_bytes(protect(json.dumps(self.data).encode()))
        temp.replace(self.path)

    def cookies(self, override: str = ""):
        if override:
            return parse_cookies(override)
        with self.lock:
            if self.data.get("cookies"):
                return dict(self.data["cookies"])
            if self.data.get("use_env", True):
                value = os.getenv("BILIBILI_SESSDATA") or os.getenv("SESSDATA")
                if value:
                    return {"SESSDATA": value}
        return {}

    def status(self):
        with self.lock:
            cookies = self.cookies()
            value = cookies.get("SESSDATA", "")
            return {
                "status": self.data.get("status", "unverified")
                if self.data.get("cookies")
                else ("unverified" if value else "disconnected"),
                "source": self.data.get("source", "saved")
                if self.data.get("cookies")
                else ("env" if value else "none"),
                "masked": (value[:3] + "…" + value[-3:])
                if len(value) > 8
                else ("***" if value else ""),
                "checked_at": self.data.get("checked_at"),
                "account": self.data.get("account"),
                "message": self.data.get("message", ""),
                "use_env": self.data.get("use_env", True),
                "has_cookie": bool(value),
            }

    def import_value(self, text, source="manual"):
        cookies = parse_cookies(text)
        with self.lock:
            self.data.update(
                cookies=cookies,
                status="unverified",
                source=source,
                account=None,
                checked_at=None,
                message="已保存，尚未验证",
            )
            self._save()
        return self.status()

    def disconnect(self):
        with self.lock:
            self.data = {
                "cookies": {},
                "status": "disconnected",
                "use_env": False,
                "message": "已移除连接，环境变量回退也已停用",
            }
            self._save()
        return self.status()

    async def verify(self):
        import asyncio
        from bilibili_api import Credential, user

        cookies = self.cookies()
        if not cookies:
            return self.status()
        try:
            cred = Credential.from_cookies(cookies)
            valid = await asyncio.wait_for(cred.check_valid(), 20)
            status = "connected" if valid else "expired"
            account = None
            if valid:
                try:
                    info = await asyncio.wait_for(user.get_self_info(cred), 10)
                    account = {
                        "name": info.get("name", ""),
                        "uid": str(info.get("mid", "")),
                    }
                except Exception:
                    pass
            message = (
                "登录验证通过；具体视频的字幕和访问权限需单独检查"
                if valid
                else "登录已失效，请重新导入"
            )
        except Exception:
            status = "network_error"
            account = None
            message = "暂时无法验证，可能是网络或平台限制；已保留凭据"
        with self.lock:
            # Do not apply an old request to newly imported credentials.
            if cookies == self.cookies():
                self.data.update(
                    status=status,
                    account=account,
                    checked_at=datetime.now(timezone.utc).isoformat(),
                    message=message,
                )
                self._save()
        return self.status()

    def browser_import(self, browser="edge", profile=None):
        if browser not in ("edge", "chrome", "firefox", "brave"):
            raise ValueError("不支持的浏览器")
        import browser_cookie3

        try:
            kw = {"domain_name": "bilibili.com"}
            if profile:
                kw["cookie_file"] = profile
            jar = getattr(browser_cookie3, browser)(**kw)
            values = {c.name: c.value for c in jar if c.name in ALLOWED}
            return self.import_value(
                "; ".join(f"{k}={v}" for k, v in values.items()), browser
            )
        except PermissionError:
            raise ValueError(
                "浏览器 Cookie 文件无法读取：请关闭对应浏览器并检查权限"
            ) from None
        except ValueError:
            raise
        except Exception:
            raise ValueError(
                "浏览器读取或解密失败，请改用手动导入；没有修改现有连接"
            ) from None


_connection = None


def get_connection():
    global _connection
    if _connection is None:
        from ..utils.project_root import get_project_root

        _connection = Connection(
            Path(os.getenv("SHENGWEN_DATA_DIR", str(get_project_root() / "data")))
            / "bilibili.dpapi"
        )
    return _connection


def attach_cookies(ydl, override=""):
    from http.cookiejar import Cookie

    for name, value in get_connection().cookies(override).items():
        ydl.cookiejar.set_cookie(
            Cookie(
                0,
                name,
                value,
                None,
                False,
                ".bilibili.com",
                True,
                True,
                "/",
                True,
                True,
                None,
                True,
                None,
                None,
                {},
                False,
            )
        )
