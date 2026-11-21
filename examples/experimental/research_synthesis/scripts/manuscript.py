import json, sys
from pathlib import Path

prisma_path, synthesis_path, destination = map(Path, sys.argv[1:])
p = json.loads(prisma_path.read_text())
s = json.loads(synthesis_path.read_text())
destination.write_text(f'''# Exercise and wellbeing, synthetic review

## Question
Among generally healthy adults, does structured exercise improve self-reported wellbeing compared with usual activity or an inactive control?

## Methods
This demonstration used a frozen synthetic response, explicit inclusion decisions, DOI deduplication, and deterministic inverse-variance fixed-effect synthesis. Human review must confirm each decision before using real evidence.

## PRISMA flow
The search identified {p["identified"]} records. Deduplication removed {p["duplicates_removed"]}, leaving {p["screened"]} for screening. The review logged {p["excluded"]} exclusions and {p["included_studies"]} inclusions.

## Results
The toy pooled mean difference was {s["estimate"]:.2f} wellbeing points (95% CI {s["ci95"][0]:.2f} to {s["ci95"][1]:.2f}). See `forest.svg`.

## Limitations
All records and values are invented. The estimate illustrates the calculation on synthetic inputs.

## Sources
- https://www.bmj.com/content/372/bmj.n71
- https://www.cochrane.org/authors/handbooks-and-manuals/handbook/current/chapter-05
''')
