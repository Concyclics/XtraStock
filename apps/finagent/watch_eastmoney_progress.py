from __future__ import annotations

import argparse
import re
import time
from pathlib import Path

from tqdm import tqdm


PROGRESS_RE = re.compile(
    r"(?P<desc>[^:\r\n]+):\s+"
    r"(?P<pct>\d+)%\|.*?\|\s+"
    r"(?P<done>\d+)/(?P<total>\d+)\s+"
    r"\[(?P<meta>[^\]]+)\]"
)


def parse_latest_progress(path: Path, tail_bytes: int) -> re.Match[str] | None:
    if not path.exists():
        return None
    with path.open("rb") as file:
        file.seek(0, 2)
        size = file.tell()
        file.seek(max(0, size - tail_bytes))
        text = file.read().decode("utf-8", errors="replace")
    matches = list(PROGRESS_RE.finditer(text.replace("\r", "\n")))
    return matches[-1] if matches else None


def main() -> None:
    parser = argparse.ArgumentParser(description="Render Eastmoney crawler progress from a tqdm log")
    parser.add_argument("log", nargs="?", default="crawler_detail_full_20260623_0255.log")
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--tail-bytes", type=int, default=200_000)
    args = parser.parse_args()

    log_path = Path(args.log)
    bar: tqdm | None = None
    current_key: tuple[str, int] | None = None

    try:
        while True:
            match = parse_latest_progress(log_path, args.tail_bytes)
            if match is None:
                tqdm.write(f"waiting for progress in {log_path}")
                time.sleep(args.interval)
                continue

            desc = match.group("desc").strip()
            done = int(match.group("done"))
            total = int(match.group("total"))
            meta = match.group("meta").strip()
            key = (desc, total)

            if bar is None or current_key != key:
                if bar is not None:
                    bar.close()
                bar = tqdm(total=total, desc=desc, dynamic_ncols=True, mininterval=0)
                current_key = key

            bar.n = done
            bar.set_postfix_str(meta, refresh=False)
            bar.refresh()
            time.sleep(args.interval)
    finally:
        if bar is not None:
            bar.close()


if __name__ == "__main__":
    main()
