"""Validate that node output is valid JSON with exactly 5 joke strings."""

import json
import sys

try:
    data = json.loads(sys.argv[1])
except json.JSONDecodeError as error:
    print(f"Invalid JSON: {error}")
    sys.exit(1)

jokes = data.get("jokes") if isinstance(data, dict) else None
if not isinstance(jokes, list) or len(jokes) != 5 or not all(isinstance(j, str) and j for j in jokes):
    print("Expected a JSON object with five nonempty joke strings")
    sys.exit(1)

print("Found five joke strings")
