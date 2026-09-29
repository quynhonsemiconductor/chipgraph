"""`python -m chipgraph.adapters.format [PATH]`: write the chip.yml JSON Schema."""

import sys
from pathlib import Path

from chipgraph.adapters.format.chip_yaml import schema_json


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("schemas/formats/chip.schema.json")
    out.write_text(schema_json(), encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
