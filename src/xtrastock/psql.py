"""Run psql with centralized configuration; never put passwords in argv."""
from __future__ import annotations

import argparse
import os
import subprocess

from xtrastock.config import Settings


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, add_help=False)
    parser.add_argument('--role', choices=['reader', 'app', 'admin'], default='reader')
    args, remaining = parser.parse_known_args(argv)
    config = Settings().postgres_kwargs(args.role)
    env = {**os.environ, **{
        'PGHOST': str(config['host']), 'PGPORT': str(config['port']),
        'PGDATABASE': config['dbname'], 'PGUSER': config['user'],
        'PGPASSWORD': config['password'], 'PGSSLMODE': config['sslmode'],
    }}
    return subprocess.run(['psql', '-v', 'ON_ERROR_STOP=1', *remaining], env=env, check=False).returncode


if __name__ == '__main__':
    raise SystemExit(main())
