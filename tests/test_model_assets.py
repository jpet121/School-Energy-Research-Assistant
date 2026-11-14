from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import subprocess
from threading import Thread

import httpx
import pytest

from processes import python_module
from retrofit.contracts import ApiError
from retrofit.fetch_models import fetch


def test_model_download_checks_digest_and_size_before_promotion(tmp_path):
    fixture = b"Model download fixture."

    class Source(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/model":
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(fixture)))
            self.end_headers()
            self.wfile.write(fixture)

    with ThreadingHTTPServer(("127.0.0.1", 0), Source) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}"
            target = tmp_path / "pinned" / "model.onnx"
            checksum = sha256(fixture).hexdigest()
            with httpx.Client(trust_env=False) as client:
                with pytest.raises(ValueError, match="size limit"):
                    fetch(client, url + "/model", target, checksum, len(fixture) - 1)
                with pytest.raises(ApiError, match="embedding_config_mismatch"):
                    fetch(client, url + "/model", target, "0" * 64, len(fixture))
                with pytest.raises(httpx.HTTPStatusError, match="404"):
                    fetch(client, url + "/missing", target, checksum, len(fixture))
                assert list(target.parent.iterdir()) == []
                fetch(client, url + "/model", target, checksum, len(fixture))
                assert target.read_bytes() == fixture
                fetch(client, url + "/missing", target, checksum, len(fixture))
                assert list(target.parent.iterdir()) == [target]
        finally:
            server.shutdown()
            thread.join(timeout=2)


def test_model_asset_cli_validates_existing_pinned_bytes_without_network(tmp_path):
    assets = os.environ.get("RETROFIT_TEST_MODELS")
    if not assets:
        pytest.skip("Set RETROFIT_TEST_MODELS to verify the real pinned model files.")
    root = Path(assets)
    for path in ("bge-small-en-v1.5", "bge-base-en-v1.5"):
        assert (root / path / "tokenizer.json").is_file() and (root / path / "model.onnx").is_file()
    verified = subprocess.run(python_module("retrofit.fetch_models", root), capture_output=True, text=True, timeout=25)
    assert verified.returncode == 0, verified.stderr
    assert "Verified BAAI/bge-small-en-v1.5" in verified.stdout
    assert "Verified BAAI/bge-base-en-v1.5" in verified.stdout
    corrupt = tmp_path / "bge-small-en-v1.5"
    corrupt.mkdir()
    (corrupt / "tokenizer.json").write_text("Local checksum failure fixture.")
    failed = subprocess.run(
        python_module("retrofit.fetch_models", tmp_path, "--model", "bge-small"),
        capture_output=True, text=True, timeout=15,
    )
    assert failed.returncode == 2 and "Could not fetch or verify BAAI/bge-small-en-v1.5" in failed.stderr
    assert list(corrupt.iterdir()) == [corrupt / "tokenizer.json"]
