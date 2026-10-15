#!/usr/bin/env python3
"""Real cold-start and GPU-release acceptance using synthetic local inputs."""

from xtrastock.config import bootstrap_environment
bootstrap_environment()

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import subprocess
import time

from dotenv import dotenv_values
import httpx

from xtrastock.config import Settings
settings = Settings()
from runtime_paths import DEPLOY_ROOT
root = DEPLOY_ROOT
destination = settings.path('paths.state') / 'migration/on-demand-smoke'
destination.mkdir(mode=0o700, parents=True, exist_ok=True)
report_path = settings.path('paths.state') / 'migration/on-demand-validation.json'
if report_path.exists():
    raise SystemExit('STOP: existing validation report; preserve it before another acceptance run')
from xtrastock.config import Settings
settings = Settings()
config = settings.legacy_environment()
gateway_url = settings.get('ocr.base_url').removesuffix('/v1')
headers = {'Authorization': 'Bearer ' + config['FINAGENT_GATEWAY_KEY']}
units = ['finagent-ocr-model.service', 'finagent-embedding-model.service']
report = {'passed': False, 'started_utc': datetime.now(timezone.utc).isoformat(),
          'scope': 'local on-demand model lifecycle; not full migration acceptance', 'tests': {}}


def save(stage):
    report['stage'] = stage
    temporary = report_path.with_suffix('.tmp')
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    temporary.replace(report_path)
    print(stage, flush=True)


def states():
    result = {}
    for unit in units:
        text = subprocess.check_output(['systemctl', '--user', 'show', unit,
                                        '-p', 'ActiveState', '-p', 'MainPID', '-p', 'UnitFileState'], text=True)
        result[unit] = dict(line.split('=', 1) for line in text.splitlines() if '=' in line)
    return result


def gpu_memory():
    text = subprocess.check_output(['nvidia-smi', '--query-gpu=index,memory.used',
                                    '--format=csv,noheader,nounits'], text=True)
    return {index.strip(): int(memory.strip()) for index, memory in
            (line.split(',') for line in text.splitlines())}


def status():
    response = httpx.get(gateway_url + '/internal/models/status', headers=headers, timeout=5)
    response.raise_for_status()
    return response.json()


try:
    save('checking_idle_gateway_and_authentication')
    report['before'] = {'units': states(), 'gpu_memory_mib': gpu_memory()}
    assert all(item['ActiveState'] == 'inactive' and item['UnitFileState'] == 'static'
               for item in report['before']['units'].values())
    assert httpx.get(gateway_url + '/health', timeout=5).json()['mode'] == 'on_demand'
    assert httpx.get(gateway_url + '/v1/models', headers=headers, timeout=5).status_code == 200
    assert httpx.post(gateway_url + '/v1/embeddings', json={'model': 'Qwen/Qwen3-Embedding-0.6B'},
                       timeout=5).status_code == 401
    assert all(item['state'] == 'stopped' for item in status()['models'].values())
    assert all(item['ActiveState'] == 'inactive' for item in states().values())
    report['tests']['health_listing_auth_do_not_wake_gpu'] = True

    save('embedding_cold_start_concurrent_requests_timeout_30_seconds')

    def embed(number):
        started = time.monotonic()
        response = httpx.post(gateway_url + '/v1/embeddings', headers=headers,
                              json={'model': 'Qwen/Qwen3-Embedding-0.6B',
                                    'input': f'FinAgent synthetic lifecycle test {number}',
                                    'encoding_format': 'float'}, timeout=30)
        response.raise_for_status()
        vector = response.json()['data'][0]['embedding']
        assert len(vector) == 1024 and all(math.isfinite(value) for value in vector)
        return {'seconds': round(time.monotonic() - started, 3), 'dimensions': len(vector)}

    with ThreadPoolExecutor(max_workers=4) as pool:
        cold = list(pool.map(embed, range(4)))
    report['tests']['embedding_cold_concurrent'] = cold
    report['tests']['embedding_only_requested_model_started'] = (
        states()[units[0]]['ActiveState'] == 'inactive' and states()[units[1]]['ActiveState'] == 'active')
    assert report['tests']['embedding_only_requested_model_started']
    report['tests']['embedding_warm'] = embed(5)
    report['embedding_ready_status'] = status()

    save('ocr_cold_start_original_pdf_command_default_timeout_180_seconds')
    env = {**os.environ, **settings.legacy_environment()}
    env['TZ'] = 'Asia/Shanghai'
    import fitz
    with fitz.open() as fixture:
        page = fixture.new_page()
        page.insert_text((72,72), 'XtraStock OCR smoke test 12345')
        fixture.save(destination / 'test.pdf')
    command = [__import__('sys').executable, str(settings.path('paths.app') / 'pdf_ocr_markdown.py'),
               str(destination / 'test.pdf'),
               '-o', str(destination / 'pdf-md'), '--base-url', gateway_url + '/v1',
               '--model', 'deepseek-ai/DeepSeek-OCR-2', '--workers', '1', '--no-progress']
    started = time.monotonic()
    log_path = destination / 'ocr-default-command.log'
    with log_path.open('x') as log:
        completed = subprocess.run(command, cwd=settings.path('paths.app'), env=env, stdout=log,
                                    stderr=subprocess.STDOUT, timeout=600)
    markdown = list((destination / 'pdf-md').rglob('*.md'))
    content = '\n'.join(path.read_text() for path in markdown)
    report['tests']['ocr_original_command'] = {
        'exit': completed.returncode, 'seconds': round(time.monotonic() - started, 3),
        'default_api_timeout_seconds': 180, 'markdown_count': len(markdown),
        'expected_synthetic_content_found': '12345' in content and 'FinAgent' in content,
        'log': str(log_path)}
    assert completed.returncode == 0 and report['tests']['ocr_original_command']['expected_synthetic_content_found']
    report['busy_then_idle_status'] = status()
    assert all(item['active_requests'] == 0 for item in report['busy_then_idle_status']['models'].values())
    report['loaded_gpu_memory_mib'] = gpu_memory()

    save('waiting_for_automatic_idle_stop_120_seconds')
    started = time.monotonic()
    deadline = started + status()['idle_timeout_seconds'] + 35
    while True:
        current = status()
        if all(item['state'] == 'stopped' for item in current['models'].values()):
            break
        if time.monotonic() > deadline:
            raise RuntimeError('Idle model shutdown did not complete in the expected grace period')
        time.sleep(2)
    report['idle_wait_seconds'] = round(time.monotonic() - started, 3)
    report['after'] = {'units': states(), 'models': current, 'gpu_memory_mib': gpu_memory()}
    assert all(item['ActiveState'] == 'inactive' and item['MainPID'] == '0'
               for item in report['after']['units'].values())
    assert all(report['after']['gpu_memory_mib'][index] <= report['before']['gpu_memory_mib'][index] + 16
               for index in ['2', '3'])
    report['tests']['automatic_idle_stop_and_gpu_release'] = True
    report['passed'] = True
    report['finished_utc'] = datetime.now(timezone.utc).isoformat()
    save('passed')
except BaseException as error:
    report['error_type'] = type(error).__name__
    # Request exceptions may contain URLs but never print headers or environment.
    report['error'] = str(error)
    save('failed')
    raise
