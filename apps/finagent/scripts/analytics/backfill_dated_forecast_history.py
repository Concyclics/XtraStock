#!/usr/bin/env python3
"""Cache dated Eastmoney report bodies; no rolling API forecast values are used.

The cache is resumable and source-bound. A missing/changed body is an explicit
failure, never a fabricated zero. No OCR, embedding, trading or message jobs are
enqueued. Ingestion is a separate reviewed step.
"""
from __future__ import annotations

import argparse
import calendar
import concurrent.futures
import datetime as dt
import gzip
import hashlib
import json
from pathlib import Path
import re
import threading
import time
import multiprocessing

import lxml.html
import requests
from urllib.parse import urlparse

API = 'https://reportapi.eastmoney.com/report/list'
HEADERS = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://data.eastmoney.com/report/'}
_local = threading.local()
_lock = threading.Lock()
_next_request = 0.0


def save(path, value):
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    tmp.replace(path)


def request(url, rate, **kwargs):
    global _next_request
    if not hasattr(_local, 'session'):
        _local.session = requests.Session()
        _local.session.headers.update(HEADERS)
    for attempt in range(4):
        with _lock:
            delay = max(0., _next_request - time.monotonic())
            _next_request = max(_next_request, time.monotonic()) + 1 / rate
        if delay:
            time.sleep(delay)
        try:
            r = _local.session.get(url, timeout=(10, 30), **kwargs)
            if r.status_code in (429, 503):
                # Honour a bounded Retry-After; also slow the shared limiter.
                wait = min(60., max(5., float(r.headers.get('Retry-After', 5 * (attempt + 1)))))
                with _lock:
                    _next_request = max(_next_request, time.monotonic() + wait)
            r.raise_for_status()
            return r
        except (requests.RequestException, ValueError):
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)


def months(start, end):
    cur = dt.date.fromisoformat(start)
    end = dt.date.fromisoformat(end)
    while cur <= end:
        last = min(end, dt.date(cur.year, cur.month, calendar.monthrange(cur.year, cur.month)[1]))
        yield cur.isoformat(), last.isoformat()
        cur = last + dt.timedelta(days=1)


def listing(output, start, end, rate):
    directory = output / 'listing'
    directory.mkdir(exist_ok=True)
    all_rows = {}
    audit = []
    for begin, finish in months(start, end):
        found = {}
        page, expected = 1, None
        while True:
            path = directory / f'{begin}-{finish}-{page:04d}.json.gz'
            if path.exists():
                with gzip.open(path, 'rt') as f:
                    item = json.load(f)
            else:
                params = dict(industryCode='*', pageSize=100, industry='*', rating='', ratingChange='',
                              beginTime=begin, endTime=finish, pageNo=page, fields='', qType=0,
                              orgCode='', code='*', rcode='')
                r = request(API, rate, params=params)
                item = {'url': r.url, 'observed_at': dt.datetime.now(dt.timezone.utc).isoformat(), 'payload': r.json()}
                with gzip.open(path.with_suffix('.tmp'), 'wt') as f:
                    json.dump(item, f, ensure_ascii=False)
                path.with_suffix('.tmp').replace(path)
            body = item['payload']
            if expected is None:
                expected = int(body['hits'])
            elif expected != int(body['hits']):
                raise RuntimeError('Historical listing changed during pagination: ' + begin)
            rows = body.get('data') or []
            for row in rows:
                day = str(row.get('publishDate', ''))[:10]
                if not begin <= day <= finish or not re.fullmatch(r'AP\d+', row.get('infoCode', '')):
                    raise ValueError('Unexpected report date or identity')
                ident = row['infoCode']
                if ident in found:
                    raise ValueError('Duplicate report while paginating ' + ident)
                found[ident] = row
            if len(rows) < 100:
                break
            page += 1
            if page > 500:
                raise ValueError('Unexpected monthly page count')
        if len(found) != expected:
            raise ValueError(f'Incomplete month {begin}: {len(found)} != {expected}')
        for ident, row in found.items():
            if ident in all_rows:
                raise ValueError('Cross-month duplicate ' + ident)
            all_rows[ident] = row
        audit.append({'begin': begin, 'end': finish, 'hits': expected, 'received': len(found), 'pages': page})
        save(output / 'LISTING_PROGRESS.json', audit)
        print(json.dumps(audit[-1]), flush=True)
    rows = sorted(all_rows.values(), key=lambda x: (x['publishDate'], x['infoCode']))
    with gzip.open(output / 'listing.jsonl.gz', 'wt') as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + '\n')
    save(output / 'LISTING_COMPLETE.json', {'reports': len(rows), 'start': start, 'end': end, 'months': audit})
    return rows


def extract_body(html, expected_title):
    tree = lxml.html.fromstring(html)
    nodes = tree.xpath('//*[@id="ctx-content"]')
    if len(nodes) != 1:
        raise ValueError('Missing/ambiguous dated report body')
    parts = [' '.join(n.text_content().split()) for n in nodes[0].xpath('.//p')]
    text = '\n'.join(p for p in parts if p)
    if len(text) < 80:
        raise ValueError('Missing/too-short report text')
    # Bind the response to its directory identity, not just a successful HTTP status.
    title = ''.join(tree.xpath('//title/text()'))
    norm = lambda s: re.sub(r'\s+', '', str(s))
    if norm(expected_title) not in norm(title):
        raise ValueError('Report title does not match directory entry')
    return text, title


def pdf_identity_supported(first_pages, stock_code, stock_name='', title=''):
    compact = re.sub(r'\s+', '', first_pages)
    if str(stock_code) in compact:
        return 'security_code_on_first_two_pages'
    normal = lambda s: re.sub(r'\W+', '', s)
    if stock_name and title and normal(stock_name) in normal(first_pages) and normal(title) in normal(first_pages):
        return 'exact_issuer_name_and_report_title_on_first_two_pages'
    raise ValueError('PDF first pages do not identify the expected security')


def extract_pdf(raw, stock_code, stock_name='', title=''):
    """Run only inside an isolated worker process, never concurrent threads."""
    import fitz
    fitz.TOOLS.mupdf_display_errors(False)
    fitz.TOOLS.mupdf_display_warnings(False)
    fitz.TOOLS.mupdf_warnings(reset=True)
    if not raw.startswith(b'%PDF'):
        raise ValueError('PDF endpoint returned non-PDF data')
    with fitz.open(stream=raw, filetype='pdf') as doc:
        if not 1 <= len(doc) <= 200:
            raise ValueError('PDF page count outside bounded company-report scope')
        first = '\n'.join(doc[i].get_text('text', sort=True) for i in range(min(2, len(doc))))
        pdf_identity_supported(first, stock_code, stock_name, title)
        # Join visual wrapping within paragraphs; preserve separate blocks and
        # page boundaries. Do not concatenate table rows into invented facts.
        paragraphs = []
        for page in doc:
            for block in page.get_text('dict', sort=True, flags=fitz.TEXTFLAGS_DICT & ~fitz.TEXT_PRESERVE_IMAGES)['blocks']:
                if block.get('type') != 0:
                    continue
                lines = [''.join(span['text'] for span in line['spans']) for line in block['lines']]
                paragraphs.append(' '.join(lines))
        text = '\n'.join(paragraphs)
        if len(text) < 200:
            raise ValueError('PDF has no usable text layer; OCR deliberately not auto-started')
        return text, len(doc), fitz.TOOLS.mupdf_warnings(reset=True)


def detail(output, rows, rate, workers):
    directory = output / 'bodies-v2'
    directory.mkdir(exist_ok=True)
    counts = {'total': len(rows), 'ok': 0, 'failed': 0, 'cached': 0}
    errors = []
    start = time.monotonic()
    pdf_pool = concurrent.futures.ProcessPoolExecutor(max_workers=3,
        mp_context=multiprocessing.get_context('spawn'), max_tasks_per_child=100)

    def fetch(row):
        ident = row['infoCode']
        path = directory / (ident + '.json.gz')
        if path.exists():
            with gzip.open(path, 'rt') as f:
                cached = json.load(f)
            if cached['info_code'] != ident or hashlib.sha256(cached['text'].encode()).hexdigest() != cached['text_sha256']:
                raise ValueError('Corrupt cached body: ' + ident)
            return 'cached'
        legacy_path = output / 'bodies' / (ident + '.json.gz')
        if legacy_path.exists():
            with gzip.open(legacy_path, 'rt') as f:
                cached = json.load(f)
            old_sha = cached['text_sha256']
            if hashlib.sha256(cached['text'].encode()).hexdigest() != old_sha:
                raise ValueError('Corrupt legacy cache')
            if cached.get('body_source') == 'dated_original_pdf_text_layer':
                raw = Path(cached['pdf_path']).read_bytes()
                if hashlib.sha256(raw).hexdigest() != cached['pdf_sha256']:
                    raise ValueError('Legacy PDF hash mismatch')
                text, pages, warnings = pdf_pool.submit(extract_pdf, raw, row['stockCode']).result(timeout=90)
                cached.update(text=text, text_sha256=hashlib.sha256(text.encode()).hexdigest(),
                              pdf_pages=pages, pdf_warnings=warnings, pdf_parser='isolated-process-text-v2')
            else:
                if extract_body(cached['html'], row['title'])[0] != cached['text']:
                    raise ValueError('Legacy HTML body mismatch')
                cached['body_source'] = 'dated_html_summary'
            cached.update(previous_text_sha256=old_sha, isolated_reparse_changed=(cached['text_sha256'] != old_sha))
            with gzip.open(path.with_suffix('.tmp'), 'wt') as f:
                json.dump(cached, f, ensure_ascii=False)
            path.with_suffix('.tmp').replace(path)
            return 'cached'
        url = 'https://data.eastmoney.com/report/info/' + ident + '.html'
        response_dir = output / 'html-responses'
        response_dir.mkdir(exist_ok=True)
        response_path = response_dir / (ident + '.json.gz')
        if response_path.exists():
            with gzip.open(response_path, 'rt') as f:
                response = json.load(f)
        else:
            r = request(url, rate)
            response = {'html': r.content.decode('utf-8'), 'sha256': hashlib.sha256(r.content).hexdigest(),
                        'observed_at': dt.datetime.now(dt.timezone.utc).isoformat()}
            with gzip.open(response_path.with_suffix('.tmp'), 'wt') as f:
                json.dump(response, f, ensure_ascii=False)
            response_path.with_suffix('.tmp').replace(response_path)
        html = response['html']
        extra = {}
        try:
            text, title = extract_body(html, row['title'])
            extra['body_source'] = 'dated_html_summary'
        except ValueError as html_error:
            tree = lxml.html.fromstring(html)
            title = ''.join(tree.xpath('//title/text()'))
            if re.sub(r'\s+', '', row['title']) not in re.sub(r'\s+', '', title):
                raise html_error
            links = tree.xpath('//a[contains(@class,"pdf-link")]/@href')
            if not links:
                raise html_error
            pdf_url = links[0]
            if not (urlparse(pdf_url).hostname == 'pdf.dfcfw.com' and ident in pdf_url):
                raise ValueError('PDF link is not bound to the source report')
            pdf_dir = output / 'pdf'
            pdf_dir.mkdir(exist_ok=True)
            pdf_path = pdf_dir / (ident + '.pdf')
            raw = pdf_path.read_bytes() if pdf_path.exists() else request(pdf_url, rate).content
            if not pdf_path.exists() and raw.startswith(b'%PDF'):
                pdf_path.with_suffix('.tmp').write_bytes(raw)
                pdf_path.with_suffix('.tmp').replace(pdf_path)
            text, pages, warnings = pdf_pool.submit(extract_pdf, raw, row['stockCode']).result(timeout=90)
            extra = {'body_source': 'dated_original_pdf_text_layer', 'html_body_error': str(html_error),
                     'pdf_url': pdf_url, 'pdf_sha256': hashlib.sha256(raw).hexdigest(),
                     'pdf_pages': pages, 'pdf_path': str(pdf_path.resolve()),
                     'pdf_warnings': warnings, 'pdf_parser': 'isolated-process-text-v2'}
        value = {'info_code': ident, 'url': url, 'observed_at': response['observed_at'],
                 'html_sha256': response['sha256'], 'html': html,
                 'text_sha256': hashlib.sha256(text.encode()).hexdigest(), 'text': text, 'page_title': title, **extra}
        with gzip.open(path.with_suffix('.tmp'), 'wt') as f:
            json.dump(value, f, ensure_ascii=False)
        path.with_suffix('.tmp').replace(path)
        return 'ok'

    # A bounded sliding window lets a slow PDF retry coexist with other work;
    # one slow document must not block the next whole chunk of 200 reports.
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        source = iter(rows)
        futures = {}
        def submit_one():
            row = next(source, None)
            if row is not None:
                futures[ex.submit(fetch, row)] = row
        for _ in range(workers * 2):
            submit_one()
        while futures:
            completed, _ = concurrent.futures.wait(futures, return_when=concurrent.futures.FIRST_COMPLETED)
            for future in completed:
                row = futures.pop(future)
                try:
                    counts[future.result()] += 1
                except Exception as exc:
                    counts['failed'] += 1
                    errors.append({'info_code': row['infoCode'], 'date': row['publishDate'], 'error': str(exc)[:300]})
                submit_one()
            attempted = sum(counts[x] for x in ('ok', 'cached', 'failed'))
            if attempted % 200 >= len(completed) and futures:
                continue
            state = {**counts, 'attempted': sum(counts[x] for x in ('ok', 'cached', 'failed')),
                     'elapsed_seconds': round(time.monotonic() - start, 2)}
            save(output / 'DETAIL_PROGRESS.json', state)
            save(output / 'DETAIL_FAILURES.json', errors)
            print(json.dumps(state), flush=True)
    save(output / 'DETAIL_COMPLETE.json', {**state, 'complete': not errors})
    pdf_pool.shutdown(wait=True)
    return not errors


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--start', default='2020-01-01')
    ap.add_argument('--end', default='2022-12-08')
    ap.add_argument('--rate', type=float, default=8.)
    ap.add_argument('--workers', type=int, default=12)
    ap.add_argument('--phase', choices=('all', 'listing', 'detail'), default='all')
    args = ap.parse_args()
    if not 0 < args.rate <= 12 or not 1 <= args.workers <= 16:
        ap.error('Bounded public-source request limits exceeded')
    args.output.mkdir(parents=True, exist_ok=True)
    if args.phase in ('all', 'listing'):
        rows = listing(args.output, args.start, args.end, args.rate)
    else:
        with gzip.open(args.output / 'listing.jsonl.gz', 'rt') as f:
            rows = [json.loads(line) for line in f]
    if args.phase in ('all', 'detail'):
        return 0 if detail(args.output, rows, args.rate, args.workers) else 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
