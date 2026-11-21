import json
import math
from pathlib import Path

root = Path("output")
prisma = json.loads((root / "prisma.json").read_text())
synthesis = json.loads((root / "synthesis.json").read_text())
assert prisma["identified"] - prisma["duplicates_removed"] == prisma["screened"]
assert prisma["screened"] - prisma["excluded"] == prisma["included_studies"]
assert len(prisma["excluded_reasons"]) == prisma["excluded"]
assert len(synthesis["studies"]) == prisma["included_studies"]
low, high = synthesis["ci95"]
estimate = synthesis["estimate"]
assert math.isfinite(low) and math.isfinite(high) and low < estimate < high
assert (root / "forest.svg").read_text().startswith("<svg")
manuscript = (root / "manuscript.md").read_text()
for section in ("## Question", "## Methods", "## PRISMA flow", "## Results", "## Limitations", "## Sources"):
    assert section in manuscript
assert f"{estimate:.2f} wellbeing points (95% CI {low:.2f} to {high:.2f})" in manuscript
print(json.dumps({"verified": True, "included_studies": prisma["included_studies"], "estimate": estimate}))
