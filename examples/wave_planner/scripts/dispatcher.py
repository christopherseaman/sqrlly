"""Emit the pending questions for this wave of repeated workers."""
import json
import sys
from pathlib import Path


def main() -> None:
    state = json.loads(Path("state.json").read_text())
    pending = [q for q in state["questions"].values() if q["status"] == "pending"]
    print(json.dumps({"items": pending}))


if __name__ == "__main__":
    sys.exit(main() or 0)
