import argparse
import os
from pathlib import Path
import sys

def main() -> None:
    parser = argparse.ArgumentParser(description="Start the Retrofit Evidence loopback workbench")
    parser.add_argument("--data", type=Path, required=True, help="Private directory for source and application data")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--models", type=Path, help="Directory containing verified local embedding assets")
    parser.add_argument("--hosted-policy", type=Path, help="Private operator-supplied D3 and verified provider configuration")
    args = parser.parse_args()
    if os.name != "posix" or not sys.platform.startswith("linux"):
        parser.error("The local application requires the supported Linux parser-isolation runtime.")
    import fcntl

    import uvicorn

    from .app import create_app

    args.data.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.data.stat().st_mode & 0o077:
        parser.error("Use a private mode-0700 data directory.")
    origin = f"http://127.0.0.1:{args.port}"
    provider = None
    authority = None
    if args.hosted_policy:
        from .openai_provider import load_policy
        try:
            provider, authority = load_policy(args.hosted_policy)
        except (OSError, ValueError):
            parser.error("The private hosted policy, verification date or provider credential is invalid.")
    fd = os.open(args.data / ".server.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "r+b") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error("This data directory is already open in another workbench process.")
        print(f"Retrofit Evidence is starting at {origin}", flush=True)
        uvicorn.run(
            create_app(args.data, origin=origin, models=args.models, provider=provider, paid_authority=authority),
            host="127.0.0.1", port=args.port,
            access_log=False, proxy_headers=False, server_header=False,
        )


if __name__ == "__main__":
    main()
