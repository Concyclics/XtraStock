#!/usr/bin/env python3
"""Optional cold-start preparation for clients with short request timeouts."""
import argparse
import json
from pathlib import Path
import time

from dotenv import dotenv_values
import httpx

parser = argparse.ArgumentParser()
choice = parser.add_mutually_exclusive_group(required=True)
choice.add_argument('--model', choices=['ocr', 'embedding'])
choice.add_argument('--status', action='store_true')
parser.add_argument('--timeout', type=float, default=360)
args = parser.parse_args()
from runtime_paths import RUNTIME_STATE
from xtrastock.config import Settings
settings = Settings()
config = settings.legacy_environment()
gateway_url = settings.get('ocr.base_url').removesuffix('/v1')
headers = {'Authorization': 'Bearer ' + config['FINAGENT_GATEWAY_KEY']}
started = time.monotonic()
if args.status:
    model = None
    response = httpx.get(gateway_url + '/internal/models/status', headers=headers, timeout=5)
else:
    model = {'ocr': 'deepseek-ai/DeepSeek-OCR-2',
             'embedding': 'Qwen/Qwen3-Embedding-0.6B'}[args.model]
    response = httpx.post(gateway_url + '/internal/models/warmup',
                          headers=headers, json={'model': model}, timeout=args.timeout)
response.raise_for_status()
print(json.dumps({'model': model, 'warmup_seconds': round(time.monotonic() - started, 3),
                  'status': response.json()}, ensure_ascii=False))
