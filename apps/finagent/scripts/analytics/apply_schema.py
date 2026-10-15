# -*- coding: utf-8 -*-
"""Apply analytics schema to the finance corpus database."""
from __future__ import annotations

import argparse

from common import connect, repo_root


def read_sql(path):
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("\\"):
            continue
        lines.append(line)
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--sql",
        default="docker/postgres/schema_analytics_signals.sql",
        help="SQL file to execute, relative to repo root by default.",
    )
    parser.add_argument("--admin", action="store_true", help="Use POSTGRES_USER/POSTGRES_PASSWORD from .env.finance-db.")
    args = parser.parse_args()

    sql_path = repo_root() / args.sql
    sql = read_sql(sql_path)
    with connect(autocommit=True, admin=args.admin) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
    print(f"applied {sql_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
