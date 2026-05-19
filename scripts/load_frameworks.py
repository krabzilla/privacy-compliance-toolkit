"""Load every framework CSV in FRAMEWORK_REGISTRY into the DB."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.frameworks import FRAMEWORK_REGISTRY, load_framework_csv  # noqa: E402


def main() -> None:
    if not FRAMEWORK_REGISTRY:
        print("No frameworks registered.")
        return
    for name, spec in FRAMEWORK_REGISTRY.items():
        try:
            n = load_framework_csv(spec)
            print(f"  loaded {name} v{spec.version} — {n} articles")
        except FileNotFoundError as e:
            print(f"  skipped {name}: {e}")


if __name__ == "__main__":
    main()
