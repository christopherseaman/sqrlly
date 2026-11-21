#!/usr/bin/env python3
"""Copy the generated static app into the preview handoff."""

import json
import shutil
from pathlib import Path


def main() -> None:
    source = Path("app/index.html")
    content = source.read_text()
    if "<main" not in content or "./app.js" not in content:
        raise SystemExit("generated index.html lacks the required browser shape")
    Path("preview").mkdir(exist_ok=True)
    Path("preview/index.html").write_text(content)
    shutil.copy2("app/app.js", "preview/app.js")
    manifest = {
        "artifacts": [
            {"path": "preview/index.html", "kind": "preview"},
            {"path": "preview/app.js", "kind": "preview-asset"},
            {"path": "app/acceptance.json", "kind": "acceptance"},
        ]
    }
    Path("artifact-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest))


if __name__ == "__main__":
    main()
