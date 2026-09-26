import contextlib
import io
import json
import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from src.main.python.sheng_wen.startup import preflight, probe_server


@contextlib.contextmanager
def listener(document):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(document).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


class StartupTests(unittest.TestCase):
    def config(self, root, port):
        path = Path(root) / 'config' / 'settings.json'
        path.parent.mkdir()
        path.write_text(json.dumps({'app': {'host': '127.0.0.1', 'port': port}}), encoding='utf-8')

    def test_repeated_launch_reuses_service_and_opens_browser(self):
        doc = {'info': {'title': 'ShengWen API'}, 'paths': {'/version': {}, '/tasks/': {}}}
        with listener(doc) as port, tempfile.TemporaryDirectory() as root:
            self.config(root, port)
            with patch('src.main.python.sheng_wen.startup.webbrowser.open') as browser, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(preflight(root), 0)
            browser.assert_called_once_with(f'http://127.0.0.1:{port}/')

    def test_check_only_does_not_open_browser(self):
        with tempfile.TemporaryDirectory() as root:
            self.config(root, 8000)
            with patch('src.main.python.sheng_wen.startup.probe_server', return_value='shengwen'), patch('src.main.python.sheng_wen.startup.webbrowser.open') as browser, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(preflight(root, check_only=True), 0)
            browser.assert_not_called()

    def test_other_service_is_not_treated_as_shengwen(self):
        with listener({'info': {'title': 'Other service'}}) as port:
            self.assertEqual(probe_server('127.0.0.1', port), 'occupied')

    def test_available_port_allows_normal_start(self):
        with tempfile.TemporaryDirectory() as root:
            self.config(root, 8000)
            with patch('src.main.python.sheng_wen.startup.probe_server', return_value='available'):
                self.assertIsNone(preflight(root))

    def test_broken_config_is_preserved(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'config' / 'settings.json'
            path.parent.mkdir()
            path.write_text('{invalid', encoding='utf-8')
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(preflight(root), 1)
            self.assertEqual(path.read_text(), '{invalid')

    def test_uvicorn_binds_before_lifespan_is_entered(self):
        import uvicorn
        entered = []
        async def app(scope, receive, send):
            entered.append(scope['type'])
        with socket.socket() as occupied:
            if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                occupied.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            occupied.bind(('127.0.0.1', 0))
            occupied.listen(1)
            config = uvicorn.Config(app, host='127.0.0.1', port=occupied.getsockname()[1], log_level='critical')
            created = []
            original_socket = socket.socket
            def tracked_socket(*args, **kwargs):
                item = original_socket(*args, **kwargs)
                created.append(item)
                return item
            try:
                with patch('uvicorn.config.socket.socket', side_effect=tracked_socket):
                    with self.assertRaises(SystemExit):
                        config.bind_socket()
            finally:
                for item in created:
                    item.close()
        self.assertEqual(entered, [])


    def test_prebound_socket_serves_requests_and_shuts_down(self):
        import time
        import uvicorn
        from urllib.request import build_opener, ProxyHandler
        async def app(scope, receive, send):
            if scope['type'] == 'lifespan':
                while True:
                    message = await receive()
                    if message['type'] == 'lifespan.startup':
                        await send({'type':'lifespan.startup.complete'})
                    else:
                        await send({'type':'lifespan.shutdown.complete'})
                        return
            else:
                await send({'type':'http.response.start','status':200,'headers':[]})
                await send({'type':'http.response.body','body':b'OK'})
        config = uvicorn.Config(app, host='127.0.0.1', port=0, log_level='critical')
        bound = config.bind_socket()
        port = bound.getsockname()[1]
        server = uvicorn.Server(config)
        thread = threading.Thread(target=lambda: server.run(sockets=[bound]), daemon=True)
        thread.start()
        try:
            for _ in range(100):
                if server.started:
                    break
                time.sleep(.01)
            self.assertTrue(server.started)
            with build_opener(ProxyHandler({})).open(f'http://127.0.0.1:{port}/', timeout=2) as response:
                self.assertEqual(response.read(), b'OK')
        finally:
            server.should_exit = True
            thread.join(timeout=3)
            bound.close()
        self.assertFalse(thread.is_alive())


if __name__ == '__main__':
    unittest.main()
