from __future__ import annotations
import os, sys, threading
from .credential_store import CredentialStore
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


class Connection:
    def __init__(self, path: Path, *, storage=None):
        self.path = path
        self.lock = threading.RLock()
        self.storage = storage if storage is not None else CredentialStore(path)
        loaded = self.storage.load()
        self.data = loaded or {"cookies": {}, "status": "disconnected", "use_env": True}
        self.persisted = bool(loaded)
        self._verified_cookies = None

    def _save(self):
        self.persisted = self.storage.save(self.data)
        return self.persisted

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
                "status": (
                    self.data.get("status", "unverified")
                    if self.data.get("cookies") or cookies == self._verified_cookies
                    else "unverified"
                )
                if value
                else "disconnected",
                "storage_mode": self.storage.mode,
                "storage_label": self.storage.label,
                "storage_persistent": self.persisted
                if self.data.get("cookies")
                else False,
                "storage_warning": self.storage.warning,
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
            self._verified_cookies = None
            self.data.update(
                user_disconnected=False,
                cookies=cookies,
                status="unverified",
                source=source,
                account=None,
                checked_at=None,
                message="已保存，尚未验证",
            )
            if not self._save():
                self.data["message"] = (
                    "凭据已在本次运行中生效，尚未持久保存；可以继续验证连接。"
                )
        return self.status()

    def disconnect(self):
        with self.lock:
            try:
                self.storage.disconnect()
            except OSError:
                raise ValueError(
                    "无法停用已保存的连接，请检查凭据目录权限；原连接未修改"
                ) from None
            self.data = {
                "cookies": {},
                "status": "disconnected",
                "use_env": False,
                "user_disconnected": True,
                "message": "已移除连接，环境变量回退也已停用",
            }
            self.persisted = True
            self._verified_cookies = None
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

        def finish_verification():
            with self.lock:
                # Native keychains may block; never run their I/O on the HTTP event loop.
                if cookies == self.cookies():
                    self.data.update(
                        status=status,
                        account=account,
                        checked_at=datetime.now(timezone.utc).isoformat(),
                        message=message,
                    )
                    self._verified_cookies = cookies
                    if self.data.get("cookies"):
                        self._save()
            return self.status()

        return await asyncio.to_thread(finish_verification)

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

        root = Path(os.getenv("SHENGWEN_DATA_DIR", str(get_project_root() / "data")))
        filename = (
            "bilibili.dpapi" if sys.platform == "win32" else "bilibili.credentials.json"
        )
        _connection = Connection(root / filename)
        try:
            copied_windows_file = (
                sys.platform != "win32"
                and not (root / filename).exists()
                and (root / "bilibili.dpapi").exists()
            )
        except OSError:
            copied_windows_file = False
        if copied_windows_file:
            _connection.storage.warning = (
                "检测到 Windows 凭据文件，已保留。请在当前系统重新连接 B 站账号。"
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
