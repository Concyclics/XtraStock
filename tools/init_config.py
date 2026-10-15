"""Create private local config without overwriting existing settings."""
from pathlib import Path
import os
import shutil

ROOT = Path(__file__).resolve().parents[1]


def main():
    for source, target in ((".env.example", ".env"), ("config/local.example.toml", "config/local.toml")):
        destination = ROOT / target
        try:
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            print(f"Preserved existing {target}")
            continue
        with os.fdopen(descriptor, "wb") as output, (ROOT / source).open("rb") as template:
            shutil.copyfileobj(template, output)
        print(f"Created {target}; fill credentials locally")


if __name__ == "__main__":
    main()
