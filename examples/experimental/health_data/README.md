# County health estimates

This example stages and hashes a CSV, inspects missing values, cleans identifiers and percentages, and produces a chart and Markdown report. The synthetic fixture has three county rows. Their mean estimates are 10.0% for diabetes and 15.0% for frequent mental distress.

## Run and build

Run both workflows from the sqrlly checkout. Keep the working files in a fresh directory.

```bash
set -euo pipefail
example=$PWD/examples/experimental/health_data
work=$(mktemp -d)
uv run sqrlly run "$example/workflow.yaml" --workdir "$work" --log "$work/analysis.jsonl"
cp -R "$example/site/_includes" "$example/site/.eleventy.js" \
  "$example/site/package.json" "$example/site/package-lock.json" "$work/site/"
npm ci --prefix "$work/site"
uv run sqrlly run "$example/site.yaml" --workdir "$work" --log "$work/build.jsonl"
test -f "$work/site/_site/index.html"
cmp "$work/reports/prevalence.svg" "$work/site/_site/plot.svg"
```

The data workflow records input SHA-256 and release metadata in `data/raw/digest.json`. Inspect `reports/eda.json`, `reports/analysis.json`, `reports/prevalence.svg`, and `site/index.md`. The site workflow publishes a local preview at `site/_site/index.html`.

## CDC PLACES input

Download the county GIS CSV or query the county GIS Socrata resource. This walkthrough uses three rows to exercise the real column mapping. To select a different sample, preserve its exact query and use a new work directory. [CDC PLACES dataset](https://data.cdc.gov/500-Cities-Places/PLACES-County-Data-GIS-Friendly-Format-2025-releas/i46a-9kgh)

```bash
data=$(mktemp -d)
curl --fail --silent --show-error --get 'https://data.cdc.gov/resource/i46a-9kgh.csv' \
  --data-urlencode '$limit=3' --data-urlencode '$order=countyfips' \
  --output "$data/places.csv"
work=$(mktemp -d)
uv run sqrlly run "$example/workflow.yaml" --workdir "$work" \
  --var "input=$data/places.csv" --var 'source_query=$limit=3&$order=countyfips'
```

The adapter reads `countyfips`, `countyname`, `diabetes_crudeprev`, and `mhlth_crudeprev`. Keep county identifiers as five digit strings. Blank measures stay missing and each summary carries its count of available estimates. The pipeline rejects duplicate counties and percentages outside the valid range.

The reports describe the selected counties. A mean county estimate gives each county equal weight. CDC uses modeled small area estimates, so the report avoids conclusions about individuals, causation, or national prevalence. [CDC methodology](https://www.cdc.gov/places/methodology/index.html), [Release notes](https://www.cdc.gov/places/current-release-notes/index.html)

The input is copied into the work directory to preserve the analyzed bytes. Use storage with room for that copy and the staged tables. The scripts stream data files, and cleaning retains a set of county IDs to detect duplicates. Put large work directories outside Git checkouts.

## GitHub Actions handoff

An analysis job runs `workflow.yaml` and uploads the report Markdown, plot, and Eleventy configuration as an artifact. A build job downloads them, installs the locked Node dependencies with `npm ci`, and runs `site.yaml`. Pages can consume only `site/_site` from that job, with publishing approval configured in the destination repository. [Custom GitHub Pages workflows](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages)
