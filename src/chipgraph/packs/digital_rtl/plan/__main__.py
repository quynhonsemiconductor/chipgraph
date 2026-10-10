"""`python -m chipgraph.packs.digital_rtl.plan [PATH]`: write the plan file JSON Schema."""

import sys
from pathlib import Path

from chipgraph.packs.digital_rtl.plan.model import schema_json


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("schemas/formats/plan.schema.json")
    out.write_text(schema_json(), encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
