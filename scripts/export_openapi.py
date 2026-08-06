"""Export the deterministic public OpenAPI contract."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.api.docs import public_schema  # noqa: E402
from app.main import app  # noqa: E402


def main():
    output = ROOT / "build" / "openapi-public.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(
        json.dumps(public_schema(app), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
