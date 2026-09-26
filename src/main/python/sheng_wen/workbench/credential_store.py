"""Platform-specific secret storage with an explicit, non-plaintext fallback."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path


def dpapi(data: bytes, decrypt: bool = False) -> bytes:
    if sys.platform != "win32":
        raise ValueError("Windows 凭据无法在当前系统解密")

    class Blob(ctypes.Structure):
        _fields_ = [("size", ctypes.c_ulong), ("data", ctypes.POINTER(ctypes.c_ubyte))]

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


def native_keyring(platform: str, environ):
    # Instantiate only OS-provided secure backends, never plaintext third-party fallbacks.
    if platform == "darwin":
        from keyring.backends.macOS import Keyring
    elif platform.startswith("linux") and environ.get("DBUS_SESSION_BUS_ADDRESS"):
        from keyring.backends.SecretService import Keyring
    else:
        return None
    backend = Keyring()
    return backend if backend.priority > 0 else None


def atomic_write(path: Path, content: bytes):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class CredentialStore:
    SERVICE = "ShengWen.Bilibili"

    def __init__(
        self, path: Path, *, platform=None, environ=None, keyring_factory=native_keyring
    ):
        self.path = Path(path)
        self.platform = sys.platform if platform is None else platform
        self.environ = os.environ if environ is None else environ
        self.mode = "session"
        self.warning = ""
        self.read_error = False
        self.backend = None
        self.cipher = None
        self.account = hashlib.sha256(str(self.path.resolve()).encode()).hexdigest()
        key = self.environ.get("SHENGWEN_SECRET_KEY")
        if key:
            try:
                from cryptography.fernet import Fernet

                self.cipher = Fernet(key.encode())
                self.mode = "encrypted_file"
            except Exception:
                self.warning = "SHENGWEN_SECRET_KEY 无效或加密依赖缺失；当前凭据仅在本次运行中使用。"
        elif self.platform == "win32":
            self.mode = "windows_dpapi"
        else:
            try:
                self.backend = keyring_factory(self.platform, self.environ)
                if self.backend:
                    self.mode = (
                        "macos_keychain"
                        if self.platform == "darwin"
                        else "linux_secret_service"
                    )
            except Exception:
                self.backend = None
            if self.backend is None:
                self.warning = "系统密钥环不可用；凭据仅在本次运行中使用。无桌面环境可配置 SHENGWEN_SECRET_KEY 以加密保存。"

    @property
    def label(self):
        return {
            "windows_dpapi": "Windows DPAPI",
            "macos_keychain": "macOS Keychain",
            "linux_secret_service": "Linux Secret Service",
            "encrypted_file": "独立密钥加密文件",
            "session": "仅本次运行（不写入凭据文件）",
        }[self.mode]

    def load(self):
        try:
            if not self.path.exists():
                return None
            raw = self.path.read_bytes()
            if raw.lstrip().startswith(b"{"):
                envelope = json.loads(raw)
                kind = envelope.get("backend")
                if kind == "disconnected":
                    return {
                        "cookies": {},
                        "status": "disconnected",
                        "use_env": False,
                        "user_disconnected": True,
                    }
                if kind == "fernet" and self.cipher is not None:
                    decoded = self.cipher.decrypt(envelope["ciphertext"].encode())
                elif kind == "keyring" and self.backend is not None:
                    self.account = str(envelope["account"])
                    decoded = self.backend.get_password(self.SERVICE, self.account)
                    if decoded is None:
                        raise ValueError("Missing keyring record")
                else:
                    raise ValueError("Storage backend not available")
            elif self.platform == "win32":
                # Retain compatibility with existing raw bilibili.dpapi files.
                decoded = dpapi(raw, True)
            else:
                raise ValueError("Foreign credential file")
            result = json.loads(decoded)
            if not isinstance(result, dict) or not isinstance(
                result.get("cookies"), dict
            ):
                raise ValueError("Invalid credential payload")
            return result
        except Exception:
            self.read_error = True
            self.warning = "原凭据暂时无法读取，原文件已保留。请检查系统密钥环或 SHENGWEN_SECRET_KEY；也可重新导入登录信息。"
            return None

    def save(self, data) -> bool:
        if self.mode == "session":
            return False
        try:
            encoded = json.dumps(data, ensure_ascii=False).encode()
            if self.mode == "windows_dpapi":
                content = dpapi(encoded)
            elif self.mode == "encrypted_file":
                content = json.dumps(
                    {
                        "version": 1,
                        "backend": "fernet",
                        "ciphertext": self.cipher.encrypt(encoded).decode(),
                    }
                ).encode()
            else:
                self.backend.set_password(self.SERVICE, self.account, encoded.decode())
                # Confirm the backend stored the value before clearing a legacy configuration.
                if (
                    self.backend.get_password(self.SERVICE, self.account)
                    != encoded.decode()
                ):
                    raise ValueError("Keyring did not persist the value")
                content = json.dumps(
                    {"version": 1, "backend": "keyring", "account": self.account}
                ).encode()
            if self.read_error and self.path.exists():
                backup = self.path.with_name(
                    self.path.name + ".unreadable-" + uuid.uuid4().hex + ".bak"
                )
                atomic_write(backup, self.path.read_bytes())
            atomic_write(self.path, content)
            self.warning = ""
            self.read_error = False
            return True
        except Exception:
            self.warning = "安全存储写入失败；新凭据仅在本次运行中生效，磁盘上的原配置未清除。请检查密钥环或目录权限。"
            return False

    def disconnect(self):
        # A non-secret tombstone also works on headless machines and prevents environment fallback after restart.
        atomic_write(
            self.path, json.dumps({"version": 1, "backend": "disconnected"}).encode()
        )
        self.warning = ""
        if self.backend is not None:
            try:
                self.backend.delete_password(self.SERVICE, self.account)
            except Exception:
                self.warning = (
                    "连接已停用；密钥环条目暂时无法删除，可在系统凭据管理中清理。"
                )
        self.read_error = False
