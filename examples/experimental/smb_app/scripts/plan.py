#!/usr/bin/env python3
"""Create a deterministic quote plan for the example SMB request."""

import json
import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: plan.py CUSTOMER CURRENCY")
    customer, currency = sys.argv[1], sys.argv[2].upper()
    if not customer.strip() or len(currency) != 3 or not currency.isalpha():
        raise SystemExit("customer must be nonempty and currency must be three letters")
    plan = {
        "customer": customer,
        "currency": currency,
        "items": [
            {"id": "setup", "name": "Setup", "amount_cents": 12000, "currency": currency},
            {"id": "support", "name": "Monthly support", "amount_cents": 4500, "currency": currency},
        ],
    }
    Path("build").mkdir(exist_ok=True)
    Path("build/plan.json").write_text(json.dumps(plan, indent=2) + "\n")
    print(json.dumps(plan))


if __name__ == "__main__":
    main()
