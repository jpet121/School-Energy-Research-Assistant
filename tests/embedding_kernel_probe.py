"""Exercise the pinned ONNX runtime under a real process memory limit."""

from pathlib import Path
import os
import resource
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from retrofit.contracts import ApiError
from retrofit.embeddings import Embeddings


directory, = sys.argv[1:]
model = Embeddings(Path(directory))
model.tokenizer("bge-base")
size = int(Path("/proc/self/statm").read_text().split()[0]) * os.sysconf("SC_PAGE_SIZE")
original = resource.getrlimit(resource.RLIMIT_AS)
resource.setrlimit(resource.RLIMIT_AS, (size + 4 * 1024 * 1024, original[1]))
try:
    try:
        model.encode("bge-base", "x " * 480)
    except ApiError as exc:
        result = exc.code
    else:
        raise AssertionError("The pinned model unexpectedly fit the deliberately constrained process.")
finally:
    resource.setrlimit(resource.RLIMIT_AS, original)
print(result)
