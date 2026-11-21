import json
import sys
from pathlib import Path

fixture = json.loads(Path(sys.argv[1]).read_text())
preview = {"source": fixture["source"], "candidate_count": len(fixture["records"]),
           "candidate_titles": [record["title"] for record in fixture["records"]]}
Path("output").mkdir(exist_ok=True)
Path("output/scout.json").write_text(json.dumps(preview, indent=2) + "\n")
