import json
import shutil
import sys
from pathlib import Path

fixtures = Path(__file__).resolve().parents[1] / "fixtures"
input_path, query = sys.argv[1:]
metadata = json.loads((fixtures / "source.json").read_text())
source = Path(input_path) if input_path else fixtures / "places.csv"
if not source.is_absolute() or not source.is_file():
    raise SystemExit("input must be an absolute path to a CSV file")
output = Path("data/raw")
output.mkdir(parents=True, exist_ok=True)
if input_path:
    metadata = {
        "source_kind": "CDC PLACES county GIS CSV supplied by operator",
        "release": "2025 county GIS release",
        "official_reference": metadata["official_reference"],
        "methodology_reference": metadata["methodology_reference"],
        "source_file": source.name,
        "source_query": query or None,
    }
shutil.copyfile(source, output / "places.csv")
(output / "source.json").write_text(json.dumps(metadata, indent=2) + "\n")
