import json
import math
import sys
from pathlib import Path

included_path, destination = map(Path, sys.argv[1:])
included = json.loads(included_path.read_text())["records"]
studies = []
for record in included:
    decision = record["review"]
    study_id = record["id"]
    sample = decision.get("sample")
    effect = decision.get("mean_difference")
    error = decision.get("standard_error")
    measure = decision.get("outcome_measure")
    if type(sample) is not int or sample < 2:
        raise SystemExit(f"{study_id}: sample must be an integer of at least 2")
    if type(effect) not in (int, float) or not math.isfinite(effect):
        raise SystemExit(f"{study_id}: mean_difference must be finite")
    if type(error) not in (int, float) or not math.isfinite(error) or error <= 0:
        raise SystemExit(f"{study_id}: standard_error must be finite and positive")
    if measure != "Wellbeing scale points":
        raise SystemExit(f"{study_id}: expected the protocol's wellbeing scale points")
    studies.append({"study_id": study_id, "source_url": record["source_url"], "sample": sample,
                    "mean_difference": effect, "standard_error": error, "outcome_measure": measure})
destination.write_text(json.dumps({"studies": studies}, indent=2) + "\n")
