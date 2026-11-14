"""Check the filesystem and SQLite boundary using real native Windows I/O."""

from pathlib import Path
import sys

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))

from retrofit.contracts import Admission, digest
from retrofit.store import Store


data = Path(sys.argv[1])
original = (root / "evidence" / "browser" / "illustrative-controls.pdf").read_bytes()
source = Admission.model_validate({
    "schema_version": "1.1", "title": "Native storage fixture", "publisher": "Local fixture",
    "source_url": "https://example.org/native-fixture.pdf", "publication_date": "2025-01-01",
    "scope": "Native filesystem boundary", "use_basis": "Local engineering fixture", "public_document": True,
    "coverage_regions": [{"first_page": 1, "last_page": 1, "use_basis": "Synthetic fixture."}],
})
store = Store(data)
job = store.stage(source, original, "native-store")
reopened = Store(data)
document = reopened.ingestion(job)["document"]
assert document["byte_hash"] == digest(original)
assert reopened.original(document) == original
assert reopened.stage(source, original, "native-store") == job
print("Native store reopened the identical content and intake identity.")
