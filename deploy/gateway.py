"""Expose the verified loopback app through an explicit single-operator boundary."""

from base64 import b64encode
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
from secrets import compare_digest
from urllib.parse import urlsplit

from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send
import uvicorn

from retrofit.app import create_app

PORT = 8765
LOCAL_ORIGIN = f"http://127.0.0.1:{PORT}"


class Gateway:
    def __init__(self, app: ASGIApp, public_origin: str, password: str | None) -> None:
        address = urlsplit(public_origin)
        if password is None:
            if (address.scheme != "http" or address.hostname != "127.0.0.1"
                or not address.port or public_origin != f"http://127.0.0.1:{address.port}"):
                raise ValueError("Desktop origin must be the designated loopback address.")
        elif (
            address.scheme != "https" or not address.hostname
            or public_origin != f"https://{address.netloc}"
            or address.username is not None or address.password is not None
            or len(password) < 24
        ):
            raise ValueError("Hosted mode requires an HTTPS origin and a strong operator password.")
        self.app = app
        self.origin = public_origin.encode("ascii")
        self.host = address.netloc.encode("ascii")
        self.authorization = (
            None if password is None else b"Basic " + b64encode(f"operator:{password}".encode("utf-8"))
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = scope["headers"]
        hosts = [value for name, value in headers if name == b"host"]
        origins = [value for name, value in headers if name == b"origin"]
        if hosts != [self.host] or (origins and origins != [self.origin]):
            await PlainTextResponse(
                "Use the configured Retrofit Evidence address.", status_code=403,
                headers={"Cache-Control": "no-store"},
            )(scope, receive, send)
            return
        if self.authorization is not None:
            credentials = [value for name, value in headers if name == b"authorization"]
            if len(credentials) != 1 or not compare_digest(credentials[0], self.authorization):
                await PlainTextResponse(
                    "Operator sign-in required.", status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="Retrofit Evidence", charset="UTF-8"',
                             "Cache-Control": "no-store"},
                )(scope, receive, send)
                return
        translated = [
            (name, value) for name, value in headers
            if name not in {b"host", b"origin", b"authorization", b"forwarded"}
            and not name.startswith(b"x-forwarded-")
        ]
        translated.append((b"host", b"127.0.0.1:8765"))
        if origins:
            translated.append((b"origin", LOCAL_ORIGIN.encode("ascii")))
        # The app enforces the existing per-launch token, fetch site and bounded request.
        await self.app({**scope, "headers": translated, "client": ("127.0.0.1", 0), "scheme": "http"},
                       receive, send)


@contextmanager
def exclusive_store(path: Path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    status = path.lstat()
    if path.is_symlink() or status.st_uid != os.getuid() or status.st_mode & 0o077:
        raise ValueError("The store must be a private mode-0700 directory owned by the service.")
    fd = os.open(path / ".server.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "r+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def main() -> None:
    os.umask(0o077)
    data = Path("/data")
    public_origin = os.environ.get("RETROFIT_PUBLIC_ORIGIN")
    password = None
    if public_origin is not None:
        password = Path("/run/secrets/retrofit-web-password").read_text(encoding="utf-8").strip()
    models = Path("/models") if Path("/models").is_dir() else None
    with exclusive_store(data):
        app = create_app(data, origin=LOCAL_ORIGIN, models=models)
        local_port = int(os.environ.get("RETROFIT_LOCAL_PORT", str(PORT)))
        gateway = Gateway(app, public_origin or f"http://127.0.0.1:{local_port}", password)
        uvicorn.run(gateway, host="0.0.0.0", port=PORT, proxy_headers=False, access_log=False,
                    server_header=False, ws="none", limit_concurrency=16)


if __name__ == "__main__":
    main()
