import asyncio
import threading
from enum import Enum
from http import HTTPStatus
from typing import Iterable, AnyStr

from .types import (
    Message,
    Scope,
    Environ,
    StartResponse,
    ASGIApp,
)

__all__ = ("ASGIMiddleware",)

global_loop = asyncio.new_event_loop()
threading.Thread(
    target=global_loop.run_forever, daemon=True, name="global_loop"
).start()


def build_scope(environ: Environ) -> Scope:
    scope = {
        "type": "http",
        "asgi": {"version": "2.1", "spec_version": "2.1",},
        "http_version": environ["SERVER_PROTOCOL"].split("/")[1],
        "method": environ["REQUEST_METHOD"],
        "scheme": environ.get("wsgi.url_scheme", "http"),
        "path": environ["PATH_INFO"],
        "query_string": environ["QUERY_STRING"].encode("ascii"),
        "root_path": environ.get("SCRIPT_NAME", ""),
        "client": None,
        "server": (environ["SERVER_NAME"], int(environ["SERVER_PORT"])),
    }
    headers = [
        (
            each[5:].lower().replace("_", "-").encode("latin1"),
            environ[each].encode("latin1"),
        )
        for each in environ.keys()
        if each.startswith("HTTP_")
    ]
    if environ.get("CONTENT_TYPE"):
        headers.append((b"content-type", environ["CONTENT_TYPE"].encode("latin1")))
    if environ.get("CONTENT_LENGTH"):
        headers.append((b"content-length", environ["CONTENT_LENGTH"].encode("latin1")))
    scope["headers"] = headers

    return scope


class ASGIMiddleware:
    """
    Convert ASGIApp to WSGIApp.

    wait_time: After the http response ends, the maximum time to wait for the ASGI app to run.
    """

    def __init__(
        self,
        app: ASGIApp,
        wait_time: float = None,
        loop: asyncio.AbstractEventLoop = global_loop,
    ) -> None:
        self.app = app
        self.loop = loop
        self.wait_time = wait_time

    def __call__(
        self, environ: Environ, start_response: StartResponse
    ) -> Iterable[AnyStr]:
        return ASGIResponder(environ, start_response, self.loop)(
            self.app, wait_time=self.wait_time
        )


class ASGIState(Enum):
    RECEIVE = "R"
    SEND = "S"
    ERROR = "E"


class ASGIResponder:
    def __init__(
        self,
        environ: Environ,
        start_response: StartResponse,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self.environ = environ
        self.start_response = start_response
        self.loop = loop
        self.sync_event = threading.Event()
        self.async_event = asyncio.Event(loop=self.loop)
        self.state = None
        self.body = bytearray()
        self.more_body = True
        self.exception = None
        self.asgi_msg: Message = None

    def _done_callback(self, future: asyncio.Future) -> None:
        if future.exception():
            e = future.exception()
            self.exception = type(e), e, e.__traceback__
            self.state = ASGIState.ERROR
        self.sync_event.set()

    def __call__(self, app: ASGIApp, wait_time: float) -> Iterable[AnyStr]:
        scope = build_scope(self.environ)
        run_asgi: asyncio.Task = self.loop.create_task(
            app(scope, self.receive, self.send)
        )
        run_asgi.add_done_callback(self._done_callback)
        read_count, body = 0, self.environ["wsgi.input"]
        content_length = int(self.environ.get("CONTENT_LENGTH", 0))
        self.more_body = content_length > 0
        self.loop.call_soon_threadsafe(lambda: None)  # call loop to run
        while not run_asgi.done() or self.state:
            self.sync_event.wait()
            self.sync_event.clear()
            if self.state == ASGIState.RECEIVE:
                data = body.read(min(16384, content_length - read_count))
                self.body.extend(data)
                read_count += len(data)
                if read_count >= content_length:
                    self.more_body = False
            elif self.state == ASGIState.SEND:
                message = self.asgi_msg
                if message["type"] == "http.response.start":
                    status = message["status"]
                    headers = [
                        (
                            name.strip().decode("latin1").lower(),
                            value.strip().decode("latin1"),
                        )
                        for name, value in message["headers"]
                    ]
                    self.start_response(
                        f"{status} {HTTPStatus(status).phrase}", headers
                    )
                elif message["type"] == "http.response.body":
                    yield message.get("body", b"")
                    if not message.get("more_body", False):
                        break
                elif message["type"] == "http.response.disconnect":
                    break
                else:
                    run_asgi.cancel()
                    raise RuntimeError("What's wrong with the ASGI app?")
                self.asgi_msg = None
            elif self.state == ASGIState.ERROR:
                self.start_response(
                    f"{500} {HTTPStatus(500).phrase}",
                    [
                        ("Content-Type", "text/plain; charset=utf-8"),
                        ("Content-Length", str(len(HTTPStatus(500).description))),
                    ],
                    self.exception,
                )
                yield str(HTTPStatus(500).description).encode("utf-8")
                return
            self.state = None
            self.loop.call_soon_threadsafe(
                lambda event: (event.set(), event.clear()), self.async_event
            )
        # HTTP response ends, wait for run_asgi
        self.loop.call_soon_threadsafe(self.async_event.set)
        if not run_asgi.done():
            self.sync_event.wait(wait_time)
        run_asgi.cancel()

    async def receive(self) -> Message:
        if not self.more_body:
            return {"type": "http.request", "body": b"", "more_body": False}

        self.state = ASGIState.RECEIVE
        self.sync_event.set()
        await self.async_event.wait()
        message = {
            "type": "http.request",
            "body": bytes(self.body),
            "more_body": self.more_body,
        }
        del self.body[:]
        return message

    async def send(self, message: Message) -> None:
        self.state = ASGIState.SEND
        self.asgi_msg = message
        self.sync_event.set()
        await self.async_event.wait()