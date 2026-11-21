import json
from pathlib import Path

MEASURES = {"diabetes_pct": "Diabetes", "mental_health_not_good_pct": "Frequent mental distress"}


def read_rows(path):
    with Path(path).open() as stream:
        for line in stream:
            yield json.loads(line)
