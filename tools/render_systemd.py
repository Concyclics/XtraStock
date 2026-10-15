"""Render deployment templates into ignored local files. Never install/start units."""
from __future__ import annotations

import argparse
from pathlib import Path
import re

from xtrastock.config import Settings


from xtrastock.deployment import render

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    settings = Settings()
    templates = settings.root / "deploy/systemd"
    count = 0
    for path in sorted(templates.rglob("*")):
        if not path.is_file():
            continue
        content = render(path.read_text(), settings)
        if not args.check:
            target = settings.root / ".local/systemd" / path.relative_to(templates)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        count += 1
    print(f"{'Checked' if args.check else 'Rendered'} {count} templates; no service was installed or started")


if __name__ == "__main__":
    main()
