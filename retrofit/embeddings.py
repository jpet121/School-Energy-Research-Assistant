from hashlib import file_digest
from pathlib import Path
from threading import Lock
from typing import Literal, TypedDict

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

from .contracts import ApiError


ModelName = Literal["bge-small", "bge-base"]
TOKENIZER_HASH = "d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66"


class ModelSpec(TypedDict):
    name: str
    revision: str
    dimension: int
    model_sha256: str


MODELS: dict[str, ModelSpec] = {
    "bge-small": {
        "name": "BAAI/bge-small-en-v1.5", "revision": "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a",
        "dimension": 384, "model_sha256": "828e1496d7fabb79cfa4dcd84fa38625c0d3d21da474a00f08db0f559940cf35",
    },
    "bge-base": {
        "name": "BAAI/bge-base-en-v1.5", "revision": "a5beb1e3e68b9ab74eb54cfd186867f64f240e1a",
        "dimension": 768, "model_sha256": "9bc579acdba21c253c62a9bf866891355a63ffa3442b52c8a37d75b2ccb91848",
    },
}
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "
MODEL_INPUT_LIMIT = 512
CHUNK_INPUT_LIMIT = 480


def verify_file(path: Path, expected: str) -> None:
    try:
        with path.open("rb") as stream:
            found = file_digest(stream, "sha256").hexdigest()
    except OSError as exc:
        raise ApiError(503, "embedding_unavailable", "The pinned embedding model files are unavailable.") from exc
    if found != expected:
        raise ApiError(409, "embedding_config_mismatch", "The local model or tokenizer fails its recorded identity check.")


class Embeddings:
    def __init__(self, root: Path | None) -> None:
        self.root = root.resolve() if root else None
        self.tokenizers: dict[str, Tokenizer] = {}
        self.sessions: dict[str, ort.InferenceSession] = {}
        self.lock = Lock()

    def assets(self, name: str) -> Path:
        if self.root is None or name not in MODELS:
            raise ApiError(503, "embedding_unavailable", "Configure a pinned local embedding model. Lexical search is available.")
        return self.root / str(MODELS[name]["name"]).split("/")[-1]

    def tokenizer(self, name: str) -> Tokenizer:
        with self.lock:
            if name not in self.tokenizers:
                path = self.assets(name) / "tokenizer.json"
                verify_file(path, TOKENIZER_HASH)
                tokenizer = Tokenizer.from_file(str(path))
                tokenizer.no_truncation()
                tokenizer.no_padding()
                self.tokenizers[name] = tokenizer
            return self.tokenizers[name]

    def configured(self) -> list[str]:
        if self.root is None:
            return []
        return [name for name in MODELS if (self.assets(name) / "tokenizer.json").is_file()]

    def config(self, name: str) -> dict:
        return {
            "key": name, **MODELS[name], "tokenizer_sha256": TOKENIZER_HASH,
            "model_input_limit": MODEL_INPUT_LIMIT, "normalized": "l2", "dtype": "float32-le",
            "query_prefix": QUERY_PREFIX, "pooling": "cls", "runtime": f"onnxruntime-{ort.__version__}-cpu",
        }

    def _session(self, name: str) -> ort.InferenceSession:
        with self.lock:
            if name not in self.sessions:
                path = self.assets(name) / "model.onnx"
                verify_file(path, str(MODELS[name]["model_sha256"]))
                options = ort.SessionOptions()
                options.intra_op_num_threads = 2
                options.inter_op_num_threads = 1
                try:
                    self.sessions[name] = ort.InferenceSession(str(path), sess_options=options, providers=["CPUExecutionProvider"])
                except Exception as exc:
                    raise ApiError(503, "embedding_unavailable", "The local model could not start. Lexical search remains available.") from exc
            return self.sessions[name]

    def encode(self, name: str, text: str, *, query: bool = False) -> bytes:
        tokenizer = self.tokenizer(name)
        encoded = tokenizer.encode((QUERY_PREFIX if query else "") + text)
        if len(encoded.ids) > MODEL_INPUT_LIMIT or (
            not query and len(tokenizer.encode(text, add_special_tokens=False).ids) > CHUNK_INPUT_LIMIT
        ):
            raise ApiError(400, "embedding_input_too_long", "Shorten the question for semantic search or use lexical search.")
        session = self._session(name)
        inputs = {
            "input_ids": np.asarray([encoded.ids], dtype=np.int64),
            "attention_mask": np.asarray([encoded.attention_mask], dtype=np.int64),
            "token_type_ids": np.asarray([encoded.type_ids], dtype=np.int64),
        }
        return infer_embedding(session, inputs, MODELS[name]["dimension"])


def infer_embedding(session: ort.InferenceSession, inputs: dict[str, np.ndarray], dimension: int) -> bytes:
    try:
        result = session.run(None, {item.name: inputs[item.name] for item in session.get_inputs()})[0]
        vector = np.asarray(result[0, 0] if result.ndim == 3 else result[0], dtype="<f4")
    except Exception as exc:
        raise ApiError(503, "embedding_failed", "Local embedding failed. Try lexical search.") from exc
    norm = checked_norm(vector, dimension)
    return np.asarray(vector / norm, dtype="<f4").tobytes()


def checked_norm(vector: np.ndarray, dimension: int) -> float:
    norm = float(np.linalg.norm(vector))
    if vector.shape != (dimension,) or not np.isfinite(vector).all() or not np.isfinite(norm) or norm == 0:
        raise ApiError(409, "embedding_config_mismatch", "The vector does not match the pinned model configuration.")
    return norm


def valid_vector(data: bytes, dimension: int) -> np.ndarray:
    if len(data) != dimension * 4:
        raise ApiError(409, "embedding_config_mismatch", "A vector does not match the recorded index dimension.")
    vector = np.frombuffer(data, dtype="<f4")
    if abs(checked_norm(vector, dimension) - 1.0) > 1e-4:
        raise ApiError(409, "embedding_config_mismatch", "A vector does not match the recorded normalization.")
    return vector
