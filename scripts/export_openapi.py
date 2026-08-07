"""Export the deterministic public OpenAPI contract."""

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from app.api.docs import public_schema  # noqa: E402
from app.main import app  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    output = args.output_root / "build" / "openapi-public.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(public_schema(app), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
