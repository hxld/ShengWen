"""Check for an existing instance before importing the database or workers."""
from __future__ import annotations

import json
import socket
import webbrowser
from pathlib import Path
from urllib.request import ProxyHandler, build_opener


def probe_server(host: str, port: int) -> str:
    """Return available, shengwen, or occupied without changing the running service."""
    target = {'0.0.0.0': '127.0.0.1', '::': '::1'}.get(host, host)
    try:
        with socket.create_connection((target, port), timeout=0.6):
            pass
    except OSError:
        return 'available'
    authority = f'[{target}]' if ':' in target else target
    try:
        # Local service discovery must not use HTTP_PROXY / Clash.
        opener = build_opener(ProxyHandler({}))
        with opener.open(f'http://{authority}:{port}/openapi.json', timeout=2) as response:
            data = json.load(response)
        paths = data.get('paths', {})
        if data.get('info', {}).get('title') == 'ShengWen API' and '/version' in paths and '/tasks/' in paths:
            return 'shengwen'
    except (OSError, ValueError, AttributeError):
        pass
    return 'occupied'


def preflight(project_root: str | Path, *, check_only: bool = False) -> int | None:
    """None permits startup; an integer is the exit status for an existing listener."""
    path = Path(project_root) / 'config' / 'settings.json'
    try:
        config = json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {}
        settings = config.get('app', {})
        host = str(settings.get('host') or '127.0.0.1')
        port = int(settings.get('port', 8000))
        if not 1 <= port <= 65535:
            raise ValueError('port out of range')
    except (OSError, ValueError, AttributeError, TypeError):
        print('启动配置无法读取，请检查 config/settings.json；原文件未修改。')
        return 1
    state = probe_server(host, port)
    target = {'0.0.0.0': '127.0.0.1', '::': '::1'}.get(host, host)
    authority = f'[{target}]' if ':' in target else target
    url = f'http://{authority}:{port}/'
    if state == 'shengwen':
        print(f'声文智汇已在运行，无需重复启动：{url}')
        if not check_only:
            try:
                if not webbrowser.open(url):
                    print('请在浏览器中打开上面的地址。')
            except Exception:
                print('请在浏览器中打开上面的地址。')
        return 0
    if state == 'occupied':
        print(f'端口 {port} 已被占用，但尚未确认是可用的 ShengWen 服务。')
        print('服务可能仍在启动；请稍后重试，或检查占用进程。未停止任何进程。')
        return 1
    if check_only:
        print(f'启动检查通过，端口 {port} 可用。')
        return 0
    return None
