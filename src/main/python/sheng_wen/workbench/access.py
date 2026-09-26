from __future__ import annotations
import ipaddress, os, secrets
from urllib.parse import urlsplit
from fastapi.responses import JSONResponse


def allowed(scope):
    headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
    host = scope.get("client", ("", 0))[0]
    try:
        local = ipaddress.ip_address(host).is_loopback
    except ValueError:
        local = host == "localhost"
    token = os.getenv("SHENGWEN_ACCESS_TOKEN", "")
    authenticated = bool(
        token
        and secrets.compare_digest(headers.get("authorization", ""), "Bearer " + token)
    )
    if not local and not authenticated:
        return False
    request_host = headers.get("host", "").split(":")[0]
    if (
        local
        and not authenticated
        and request_host not in ("localhost", "127.0.0.1", "[")
    ):
        return False
    origin = headers.get("origin")
    if origin:
        source = urlsplit(origin)
        if source.scheme not in ("http", "https"):
            return False
        if source.netloc != headers.get("host") and source.hostname not in (
            "localhost",
            "127.0.0.1",
            "::1",
        ):
            return False
    return True


class AccessMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket") and not allowed(scope):
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            await JSONResponse(
                {
                    "detail": "仅允许本机访问；局域网客户端需要 SHENGWEN_ACCESS_TOKEN 和 Bearer 认证"
                },
                status_code=403,
            )(scope, receive, send)
            return
        await self.app(scope, receive, send)


async def value_error_handler(request, exc):
    return JSONResponse({"detail": str(exc)}, status_code=400)
