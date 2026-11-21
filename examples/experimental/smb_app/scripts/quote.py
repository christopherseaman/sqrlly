#!/usr/bin/env python3
"""Write one quote line, using integer cents to avoid money rounding."""

import json
import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) != 6:
        raise SystemExit("usage: quote.py CUSTOMER ID NAME AMOUNT_CENTS CURRENCY")
    customer, item_id, name, amount, currency = sys.argv[1:]
    subtotal = int(amount)
    tax = subtotal // 5
    quote = {
        "customer": customer,
        "item_id": item_id,
        "name": name,
        "currency": currency,
        "subtotal_cents": subtotal,
        "tax_cents": tax,
        "total_cents": subtotal + tax,
    }
    out = Path("build/quotes")
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{item_id}.json").write_text(json.dumps(quote, indent=2) + "\n")
    print(json.dumps(quote))


if __name__ == "__main__":
    main()
