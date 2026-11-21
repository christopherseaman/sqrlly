import hashlib
import json
from pathlib import Path

raw = Path("data/raw")
metadata = json.loads((raw / "source.json").read_text())
with (raw / "places.csv").open("rb") as source:
    digest = hashlib.file_digest(source, "sha256").hexdigest()
size = (raw / "places.csv").stat().st_size
(raw / "digest.json").write_text(json.dumps({**metadata, "sha256": digest, "bytes": size}, indent=2) + "\n")
