import sys
from pathlib import Path

channel = sys.argv[1]
message = Path("message.txt").read_text().strip()
if message != "release notes ready":
    sys.exit("message.txt must contain 'release notes ready'")

destination = Path(f"{channel}.txt")
if not destination.is_file():
    sys.exit(f"Missing destination file: {destination}")

sys.stdout.write(f"{destination.read_text().strip()}: {message}")
