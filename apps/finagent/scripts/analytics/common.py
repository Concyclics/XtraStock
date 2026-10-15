# -*- coding: utf-8 -*-
"""Shared helpers for finance analytics batch jobs."""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any, Iterable

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_env_file(path: Path | None = None) -> dict[str, str]:
    from xtrastock.config import load_legacy_env
    return load_legacy_env(path)


def connect(autocommit: bool = True, row_factory=dict_row, admin: bool = False, attempts: int = 10):
    from xtrastock.config import Settings
    kwargs = {**Settings().postgres_kwargs('admin' if admin else 'app'),
              'autocommit': autocommit, 'row_factory': row_factory}
    last_error: psycopg.OperationalError | None = None
    for attempt in range(max(1, attempts)):
        try:
            return psycopg.connect(**kwargs)
        except psycopg.OperationalError as exc:
            last_error = exc
            if attempt + 1 >= max(1, attempts):
                raise
            time.sleep(min(0.5 * (2**attempt), 4.0))
    raise last_error  # pragma: no cover


def jsonb(value: Any) -> Jsonb:
    return Jsonb(value)


def add_common_db_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dry-run", action="store_true", help="Print what would run without writing.")


def parse_source_types(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [x.strip() for x in value.split(",") if x.strip()]


def as_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def chunks(items: list[Any], size: int) -> Iterable[list[Any]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]
