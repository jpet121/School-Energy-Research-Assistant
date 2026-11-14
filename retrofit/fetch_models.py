"""Fetch and verify the pinned optional local embedding model assets."""

import argparse
from pathlib import Path
from tempfile import NamedTemporaryFile

import httpx

from .contracts import ApiError
from .embeddings import MODELS, TOKENIZER_HASH, verify_file


def fetch(client: httpx.Client, source: str, destination: Path, sha256: str, size_limit: int) -> None:
    if destination.is_file():
        verify_file(destination, sha256)
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=destination.parent, delete=False) as output:
        temporary = Path(output.name)
        try:
            size = 0
            with client.stream("GET", source) as response:
                response.raise_for_status()
                for block in response.iter_bytes():
                    size += len(block)
                    if size > size_limit:
                        raise ValueError("Model asset exceeded its download size limit")
                    output.write(block)
            output.close()
            verify_file(temporary, sha256)
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--model", choices=tuple(MODELS), action="append")
    args = parser.parse_args()
    with httpx.Client(follow_redirects=True, timeout=60) as client:
        for key in args.model or MODELS:
            model = MODELS[key]
            name = str(model["name"])
            target = args.destination / name.split("/")[-1]
            base = f"https://huggingface.co/{name}/resolve/{model['revision']}"
            try:
                fetch(client, f"{base}/tokenizer.json", target / "tokenizer.json", TOKENIZER_HASH, 8_000_000)
                fetch(client, f"{base}/onnx/model.onnx", target / "model.onnx", str(model["model_sha256"]), 500_000_000)
            except (ApiError, httpx.HTTPError, ValueError, OSError):
                parser.error(f"Could not fetch or verify {name}. Correct the file or directory and retry.")
            print(f"Verified {name} at {model['revision']}")


if __name__ == "__main__":
    main()
