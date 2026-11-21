# Synthetic research synthesis

This example runs a reproducible literature-review-shaped workflow without network access or real studies.

```bash
set -euo pipefail
example=$PWD/examples/experimental/research_synthesis
scratch=$(mktemp -d)
uv run sqrlly validate "$example/workflow.yaml"
uv run sqrlly run "$example/workflow.yaml" --workdir "$scratch" --log "$scratch/run.jsonl" --quiet || test $? -eq 1
cat "$scratch/output/deduplicated.json"
cat "$example/decisions.fixture.json"
# Review the record decisions before copying the synthetic answers.
cp "$example/decisions.fixture.json" "$scratch/decisions.reviewed.json"
uv run sqrlly run "$example/workflow.yaml" --workdir "$scratch" --resume --log "$scratch/run.jsonl" --quiet
```

The workflow reads `question.md` and writes a synthetic search preview at `output/scout.json`. It then checks the frozen search against `protocol.json` and records both digests in `output/provenance.json`. Screening removes the duplicate DOI and applies `decisions.reviewed.json` from the runtime directory. The remaining nodes extract toy effects, plot the estimates and confidence intervals, and verify `output/manuscript.md`.

The fixture identifies four records, removes one duplicate, excludes one record, and includes two studies. The pooled result is 3.00 wellbeing points with a 95% interval from 1.61 to 4.39.

`decisions.reviewed.json` is the manual copy/review boundary. The committed `decisions.fixture.json` supplies synthetic answers for the scratch run, but a real reviewer must replace it with checked records and reasons. The file records source links but does not prove reviewer identity or approval.

PRISMA 2020 recommends transparent reporting of identification, screening, eligibility, and inclusion, while Cochrane guidance covers question formulation, searching, extraction, bias assessment, and synthesis:

- [PRISMA 2020 statement](https://www.bmj.com/content/372/bmj.n71)
- [Cochrane review questions](https://www.cochrane.org/authors/handbooks-and-manuals/handbook/current/chapter-01)
- [Cochrane data extraction](https://www.cochrane.org/authors/handbooks-and-manuals/handbook/current/chapter-05)

The records, effect sizes, chart, and manuscript are synthetic. The fixed-effect calculation omits real-study heterogeneity, risk-of-bias tooling, publication bias, and independent duplicate extraction.
