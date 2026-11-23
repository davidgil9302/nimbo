"""A loopback HTTP adapter for the in-memory request handler."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import httpx

from .faults import SimulatedCrash

_Route = Callable[[httpx.Request, str], httpx.Response]


class _RequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: _DriveServer

    def log_message(self, format: str, *args: object) -> None:
        # Avoid recording headers, OAuth codes or query strings in test output.
        pass

    def _request(self) -> None:
        try:
            content = self._body()
        except (ValueError, OSError):
            self.send_error(400, "Malformed HTTP body")
            return
        try:
            base = self.server.base
            request = httpx.Request(
                self.command, base + self.path, headers=self.headers.items(), content=content
            )
            self._reply(self.server.route(request, base))
        except (httpx.TransportError, SimulatedCrash, OSError):
            # There is no response to serialize if the request or connection fails.
            self.close_connection = True

    def _body(self) -> bytes:
        transfer_encoding = self.headers.get("Transfer-Encoding")
        if transfer_encoding is None:
            length = int(self.headers.get("Content-Length", "0"))
            return self._read_exact(length)
        if transfer_encoding.lower() != "chunked" or "Content-Length" in self.headers:
            raise ValueError("Invalid message framing")
        result = bytearray()
        while True:
            line = self.rfile.readline(8192)
            if not line.endswith(b"\r\n"):
                raise ValueError("Invalid chunk header")
            size = int(line.partition(b";")[0].strip(), 16)
            if size == 0:
                self._read_trailers()
                return bytes(result)
            result.extend(self._read_exact(size))
            if self._read_exact(2) != b"\r\n":
                raise ValueError("Invalid chunk framing")

    def _read_trailers(self) -> None:
        while (line := self.rfile.readline(8192)) != b"\r\n":
            if not line.endswith(b"\r\n"):
                raise ValueError("Invalid chunk trailer")

    def _read_exact(self, length: int) -> bytes:
        if length < 0:
            raise ValueError("Invalid content length")
        content = self.rfile.read(length)
        if len(content) != length:
            raise ValueError("Unexpected end of request body")
        return content

    def _reply(self, response: httpx.Response) -> None:
        self.send_response(response.status_code)
        for key, value in response.headers.multi_items():
            self.send_header(key, value)
        if "Content-Length" not in response.headers and response.status_code not in {204, 304}:
            self.send_header("Content-Length", str(len(response.content)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(response.content)

    do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = _request
    do_OPTIONS = do_TRACE = do_CONNECT = _request


class _DriveServer(ThreadingHTTPServer):
    def __init__(self, route: _Route) -> None:
        super().__init__(("127.0.0.1", 0), _RequestHandler)
        self.base = f"http://127.0.0.1:{self.server_port}"
        self.route = route


@contextmanager
def serve(handler: _Route) -> Iterator[str]:
    """Run the shared handler in a threaded HTTP server and yield its base URL."""
    with _DriveServer(handler) as server:
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            yield server.base
        finally:
            server.shutdown()
            thread.join()
