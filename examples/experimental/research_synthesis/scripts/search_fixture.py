import hashlib
import json
import sys
from pathlib import Path

fixture_path, protocol_path = map(Path, sys.argv[1:])
fixture_bytes = fixture_path.read_bytes()
protocol_bytes = protocol_path.read_bytes()
fixture = json.loads(fixture_bytes)
protocol = json.loads(protocol_bytes)
if fixture["source"] != "frozen_synthetic_search" or not fixture["records"]:
    raise SystemExit("expected a nonempty frozen synthetic search response")
if fixture["query"] != protocol["search"]["query"]:
    raise SystemExit("frozen search query differs from the protocol")
ids = [record["id"] for record in fixture["records"]]
if len(ids) != len(set(ids)):
    raise SystemExit("frozen search record IDs must be unique")
output = Path("output")
output.mkdir(exist_ok=True)
(output / "search.json").write_bytes(fixture_bytes)
provenance = {
    "source": fixture["source"], "query": fixture["query"], "retrieved_at": fixture["retrieved_at"],
    "search_sha256": hashlib.sha256(fixture_bytes).hexdigest(),
    "protocol_sha256": hashlib.sha256(protocol_bytes).hexdigest(),
}
(output / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
