"""Start a real workbench process connected only to a test loopback provider."""

from pathlib import Path
import sys

import httpx
import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from retrofit.app import create_app
from retrofit.hosted_policy import PaidAuthority
from test_openai_transport import provider


data, origin, endpoint = sys.argv[1:]
app = create_app(
    Path(data), origin=origin, provider=provider(httpx.URL(endpoint)),
    paid_authority=PaidAuthority("local-crash-fixture", 1_000_000),
)
uvicorn.run(app, host="127.0.0.1", port=int(origin.rsplit(":", 1)[1]), access_log=False, proxy_headers=False)
