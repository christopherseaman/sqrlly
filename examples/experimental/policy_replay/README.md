# Library transfer policy replay

A fictional library network wants to decide when to ship a requested book from another branch. The replay applies two rules to the same 11 synthetic requests. Each rule sees the estimated transfer time, source copy availability, and limited local access flag recorded at decision time. A separate file gives the fixture reviewer's preferred decision for nine requests.

From the repository root, run both comparisons in fresh work directories.

```bash
set -euo pipefail
example=$PWD/examples/experimental/policy_replay
work=$(mktemp -d)
mkdir "$work/hold" "$work/go"

hold_status=0
uv run sqrlly run "$example/workflow.yaml" --workdir "$work/hold" \
  --var candidate=favor_standard || hold_status=$?
test "$hold_status" -eq 1
uv run sqrlly run "$example/workflow.yaml" --workdir "$work/go" \
  --var candidate=protect_access

python3 -m json.tool "$work/hold/replay-report.json"
python3 -m json.tool "$work/go/replay-report.json"
```

The replay node writes `replay-report.json` before the release gate reads it. A hold exits with status 1 and leaves the report available for CI to upload. A go exits with status 0. Exit status 1 can also indicate an execution error, so read the report to confirm a hold. Use a fresh directory for each run to preserve existing reports.

| Rule | Matches reviewer decisions | Ships reviewed priority requests marked for shipment | Changed decisions | Verdict |
| --- | ---: | ---: | ---: | --- |
| Baseline | 5/9 | 2/3 | 0 | Comparison rule |
| `favor_standard` | 7/9 | 1/3 | 6 | Hold |
| `protect_access` | 9/9 | 3/3 | 5 | Go |

Reviewer agreement counts exact matches among the nine adjudicated requests. The priority service rate counts simulated shipments among the three reviewed limited-access requests that the reviewer marked for shipment. Go requires strict improvement in agreement, no drop in that priority service rate, and no removal of a simulated shipment for an unreviewed limited-access request. The gate holds if either metric has no reviewed cases.

Both rules change decisions for S07, which has no adjudication. The rule favoring standard requests also removes the simulated shipment for unreviewed P04. The report lists every changed ID and its paired decisions, plus review coverage and input SHA-256 hashes. Edit `policies.json` to change the thresholds and rerun in a fresh directory. Use `requests.json` for the frozen cohort and `reviews.json` for adjudications.

Each case models a single decision with inventory held fixed. The replay leaves actual delivery times, fulfillment, branch inventory effects, and results on future requests unknown. The nine synthetic adjudications measure agreement on this fixture. A go means the candidate clears the offline gate.

## Two-library consent rehearsal

East uses the original 11-request cohort. West uses a separate seven-request cohort in `west_cohort/`. Both fictional libraries run the same proposed rules against their own decision-time inputs and reviews. For `protect_access`, East's agreement goes from 5/9 to 9/9. West's agreement goes from 3/5 to 5/5. Both clear their local gates. West can still withhold approval.

Run these commands from the repository root. Each member invocation names the directory holding its own `requests.json` and `reviews.json`.

```bash
set -euo pipefail
example=$PWD/examples/experimental/policy_replay
work=$(mktemp -d)
mkdir "$work"/{east,west,west-withholds,unanimous,withheld,missing}

member() {
  uv run sqrlly run "$example/member.yaml" --workdir "$work/$1" \
    --var "member=$2" --var "dataset=$3" --var "position=$4" \
    --var candidate=protect_access --var rehearsal=round-1
}
member east east "$example" approve
member west west "$example/west_cohort" approve
member west-withholds west "$example/west_cohort" withhold

conference() {
  local output=$1
  shift
  uv run sqrlly run "$example/conference.yaml" --workdir "$work/$output" \
    --var "east_decision=$work/east/member-decision.json" \
    --var candidate=protect_access --var rehearsal=round-1 "$@"
}
conference unanimous --var "west_decision=$work/west/member-decision.json"
withheld_status=0
conference withheld --var "west_decision=$work/west-withholds/member-decision.json" \
  || withheld_status=$?
test "$withheld_status" -eq 1
missing_status=0
conference missing || missing_status=$?
test "$missing_status" -eq 1
python3 -m json.tool "$work/withheld/conference-report.json"
```

Keep each `replay-report.json` with its member. Supply only the selected `member-decision.json` to the coordinator. It contains the member slot, rehearsal ID, candidate name, policy and evaluator hashes, local verdict, explicit position, and timestamp. A member can record `withhold` after either local verdict. The member workflow requires a local go before issuing `approve`.

The coordinator uses the fixed roster of East and West, reads only the named receipt paths and its policy configuration, and writes `conference-report.json`. Both approvals must match the named rehearsal, baseline and candidate rule values, and exact `replay.py` bytes. Use `--var policies=/absolute/path/to/policies.json` on a member or coordinator run to select its own copy. Hashes cover the two selected rule objects, with stable key order. Missing evidence, a receipt in the wrong roster slot, mismatched rules, evaluator, or rehearsal IDs, invalid JSON or receipt fields, and decisions older than 24 hours all produce a hold. The unanimous case exits 0. The withheld and missing cases exit 1.

The rehearsal assumes a trusted shared host. Operators choose the correct member datasets, use a new rehearsal ID for each round, and supply each member's current receipt. A member records withdrawal by issuing a new receipt with `position=withhold` for the same rehearsal. The coordinator uses the supplied receipt and cannot detect an older approval from the same still-valid round. The files can be edited by anyone with write access. Hashes check rule and evaluator content. Operators verify who approved each decision. The example has no network exchange, authentication, privacy isolation, cross-run receipt history, or real commitments and transfers.
