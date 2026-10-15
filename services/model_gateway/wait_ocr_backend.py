#!/usr/bin/env python3
"""Wait for the CPU gateway and database without loading a GPU model."""

from xtrastock.config import bootstrap_environment
bootstrap_environment()

import argparse
import json
import os
from pathlib import Path
import time

import psycopg
import requests

parser = argparse.ArgumentParser()
parser.add_argument('--timeout', type=int, default=900)
parser.add_argument('--probe-only', action='store_true')
args = parser.parse_args()
from xtrastock.config import Settings
settings = Settings()
config = settings.legacy_environment()
gateway_url = settings.get('ocr.base_url').removesuffix('/v1')

def probe():
    status = {'ocr_on_demand': False, 'authenticated_gateway': False, 'database': False,
              'database_outside_restore_mode': False}
    try:
        response = requests.get(gateway_url + '/v1/models', timeout=3,
                                headers={'Authorization': 'Bearer ' + config['FINAGENT_GATEWAY_KEY']})
        status['authenticated_gateway'] = response.status_code == 200
        health = requests.get(gateway_url + '/health', timeout=3)
        status['ocr_on_demand'] = (health.status_code == 200 and
                                  health.json().get('mode') == 'on_demand' and
                                  any(item.get('id') == 'deepseek-ai/DeepSeek-OCR-2'
                                      for item in response.json().get('data', [])))
    except (requests.RequestException, KeyError, ValueError):
        pass
    try:
        with psycopg.connect(**{**settings.postgres_kwargs('app'), 'connect_timeout': 3, 'autocommit': True}) as conn:
            conn.execute('SELECT 1')
            status['database'] = True
            status['database_outside_restore_mode'] = conn.execute('SHOW timescaledb.restoring').fetchone()[0] == 'off'
    except (psycopg.Error, KeyError, ValueError):
        pass
    status['ready'] = all(status.values())
    return status

deadline = time.monotonic() + args.timeout
previous = None
while True:
    status = probe()
    if status != previous:
        print(json.dumps(status), flush=True)
        previous = status
    if args.probe_only or status['ready']:
        break
    if time.monotonic() >= deadline:
        raise SystemExit('OCR gateway/database prerequisites did not become ready; no queue work was started')
    time.sleep(5)
