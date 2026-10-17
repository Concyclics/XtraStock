#!/usr/bin/env python3
"""Convert PDF pages to OCR markdown through an OpenAI-compatible vLLM server."""

from __future__ import annotations

from xtrastock.config import bootstrap_environment
bootstrap_environment()


import argparse
import base64
import hashlib
import io
import json
import logging
import os
import re
import sys
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable, Optional

import fitz
import requests
from PIL import Image
from tqdm import tqdm
from report_attachments import convert_native, detect_format, sha256, validate_outputs
from inference_http import post_with_capacity_wait


DEFAULT_BASE_URL = os.getenv(
    "FINANCE_OCR_BASE_URL",
    os.getenv("FINANCE_API_BASE_URL", "http://localhost:8082/v1"),
)
DEFAULT_PROMPT = "<image>\n<|grounding|>Convert the document to markdown."
DEFAULT_FIGURE_PROMPT = "<image>\nFree OCR."
LAYOUT_TAG_RE = re.compile(
    r"(?m)^[A-Za-z_]+\[\[[^\n]*?\]\]\s*$\n?"
)
SPECIAL_TOKEN_RE = re.compile(r"<\|[^>\n]{1,80}\|>")
BOX_TAG_RE = re.compile(r"(?m)^(?P<tag>[A-Za-z_]+)\[\[(?P<body>[^\n]+?)\]\]")


@dataclass
class PageQuality:
    page: int
    ok: bool
    chars: int
    cjk_chars: int
    tables: int
    headings: int
    layout_tags: int
    replacement_chars: int
    embedded_text_chars: int
    embedded_text_similarity: Optional[float]
    finish_reason: Optional[str]
    elapsed_sec: float
    warnings: list[str]


@dataclass
class PageResult:
    page: int
    markdown: str
    raw_markdown: str
    quality: PageQuality
    usage: Optional[dict[str, Any]]
    error: Optional[str] = None


def parse_page_range(spec: Optional[str], total_pages: int) -> list[int]:
    """Return zero-based page indexes from a one-based range string."""
    if not spec:
        return list(range(total_pages))

    pages: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start_s, end_s = part.split("-", 1)
            start = int(start_s) if start_s else 1
            end = int(end_s) if end_s else total_pages
            if start > end:
                raise ValueError(f"invalid page range: {part}")
            pages.update(range(start - 1, end))
        else:
            pages.add(int(part) - 1)

    bad = [p + 1 for p in pages if p < 0 or p >= total_pages]
    if bad:
        raise ValueError(f"page out of range: {bad}; total pages: {total_pages}")
    return sorted(pages)


def iter_pdfs(input_path: Path, glob_pattern: str, recursive: bool) -> list[Path]:
    if input_path.is_file():
        return [input_path]
    if not input_path.is_dir():
        raise FileNotFoundError(input_path)

    iterator = input_path.rglob(glob_pattern) if recursive else input_path.glob(glob_pattern)
    return sorted(path for path in iterator if path.is_file() and path.suffix.lower() == ".pdf")


def file_sort_time(path: Path, sort_by: str) -> float:
    stat = path.stat()
    if sort_by == "ctime":
        return stat.st_ctime
    return stat.st_mtime


def order_and_filter_pdfs(
    pdfs: list[Path],
    sort_by: str,
    min_age_seconds: int,
) -> list[Path]:
    if min_age_seconds > 0:
        cutoff = time.time() - min_age_seconds
        pdfs = [path for path in pdfs if path.stat().st_mtime <= cutoff]

    if sort_by == "path":
        return sorted(pdfs)
    return sorted(pdfs, key=lambda path: (file_sort_time(path, sort_by), str(path)))


def normalize_for_compare(text: str, max_chars: int = 12000) -> str:
    text = SPECIAL_TOKEN_RE.sub("", text)
    text = re.sub(r"[\W_]+", "", text, flags=re.UNICODE)
    return text[:max_chars]


def clean_markdown(text: str, keep_layout_tags: bool) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # DeepSeek-OCR-2 wraps labels in <|ref|>/<|det|>. Unwrap before
    # matching layout lines; the reverse order leaks coordinates into text.
    text = SPECIAL_TOKEN_RE.sub("", text)
    if not keep_layout_tags:
        text = LAYOUT_TAG_RE.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_layout_boxes(raw_markdown: str, tags: set[str]) -> list[tuple[str, tuple[int, int, int, int]]]:
    boxes: list[tuple[str, tuple[int, int, int, int]]] = []
    raw_markdown = SPECIAL_TOKEN_RE.sub("", raw_markdown)
    for match in BOX_TAG_RE.finditer(raw_markdown):
        tag = match.group("tag").lower()
        if tag not in tags:
            continue
        numbers = [int(value) for value in re.findall(r"-?\d+", match.group("body"))]
        for index in range(0, len(numbers) - 3, 4):
            x1, y1, x2, y2 = numbers[index : index + 4]
            if x2 <= x1 or y2 <= y1:
                continue
            boxes.append((tag, (x1, y1, x2, y2)))
    return boxes


def crop_normalized_box(
    image: Image.Image,
    box: tuple[int, int, int, int],
    padding: int,
) -> Image.Image:
    width, height = image.size
    x1, y1, x2, y2 = box
    left = max(0, round((x1 - padding) / 1000 * width))
    top = max(0, round((y1 - padding) / 1000 * height))
    right = min(width, round((x2 + padding) / 1000 * width))
    bottom = min(height, round((y2 + padding) / 1000 * height))
    return image.crop((left, top, right, bottom))


def append_figure_markdown(markdown: str, figure_markdowns: list[str]) -> str:
    clean_figures = [item.strip() for item in figure_markdowns if item.strip()]
    if not clean_figures:
        return markdown

    parts = [markdown.rstrip(), "", "### 图表补充识别"]
    for index, figure_markdown in enumerate(clean_figures, start=1):
        parts.extend(["", f"#### 图表 {index}", figure_markdown])
    return "\n".join(parts).strip()


def native_vector_figure_text(page: fitz.Page, box: tuple[int, int, int, int]) -> Optional[str]:
    """Read selectable chart labels directly only when no raster overlaps.

    Mixed/scanned figures still require OCR. Rotated pages are excluded because
    rendered layout coordinates and PDF text coordinates can differ.
    """
    if page.rotation:
        return None
    rect = fitz.Rect(box[0] * page.rect.width / 1000, box[1] * page.rect.height / 1000,
                     box[2] * page.rect.width / 1000, box[3] * page.rect.height / 1000)
    rect = (rect + (-2, -2, 2, 2)) & page.rect
    if any((rect & fitz.Rect(image["bbox"])).get_area() > 1 for image in page.get_image_info()):
        return None
    text = page.get_text("text", clip=rect, sort=True).strip()
    if len(text) < 20 or "\ufffd" in text:
        return None
    return "图表原文文字（按页面顺序）：\n\n" + text


def is_small_figure(image: Image.Image, box: tuple[int, int, int, int]) -> bool:
    # Measure the detected box before adding padding. With the old test, 3.5%
    # padding on each side made every tiny chapter icon exceed the size cutoff.
    width = (box[2] - box[0]) * image.width / 1000
    height = (box[3] - box[1]) * image.height / 1000
    return width < max(40, image.width * .05) or height < max(40, image.height * .04)


def render_page_to_image(
    page: fitz.Page,
    dpi: int,
    max_side: int,
    rotate_clockwise: int = 0,
) -> Image.Image:
    if rotate_clockwise not in (0, 90, 180, 270):
        raise ValueError("OCR rotation must be 0, 90, 180 or 270 degrees clockwise")
    matrix = fitz.Matrix(dpi / 72.0, dpi / 72.0)
    pix = page.get_pixmap(matrix=matrix, alpha=False)
    image = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
    if rotate_clockwise:
        # Some filings contain sideways outlined glyphs but no PDF rotation
        # flag or extractable text. Rotate only this OCR input, never the source.
        image = image.transpose({90: Image.Transpose.ROTATE_270,
                                 180: Image.Transpose.ROTATE_180,
                                 270: Image.Transpose.ROTATE_90}[rotate_clockwise])
    if max_side > 0 and max(image.size) > max_side:
        image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    return image


def image_to_jpeg(image: Image.Image, jpeg_quality: int) -> bytes:
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=jpeg_quality, optimize=True)
    return output.getvalue()


def render_page_to_jpeg(
    page: fitz.Page,
    dpi: int,
    max_side: int,
    jpeg_quality: int,
) -> bytes:
    return image_to_jpeg(render_page_to_image(page, dpi, max_side), jpeg_quality)


def api_headers() -> dict[str, str]:
    key_env = os.getenv("FINANCE_OCR_API_KEY_ENV", "FINANCE_API_KEY")
    api_key = os.getenv(key_env, "").strip()
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def get_first_model(base_url: str, timeout: int) -> str:
    url = base_url.rstrip("/") + "/models"
    response = requests.get(url, headers=api_headers(), timeout=timeout)
    response.raise_for_status()
    data = response.json()
    models = data.get("data") or []
    if not models:
        raise RuntimeError(f"no models returned from {url}")
    return models[0]["id"]


def call_ocr(
    *,
    session: requests.Session,
    base_url: str,
    model: str,
    image_bytes: bytes,
    prompt: str,
    max_tokens: int,
    temperature: float,
    timeout: int,
    retries: int,
) -> tuple[str, Optional[str], Optional[dict[str, Any]]]:
    image_b64 = base64.b64encode(image_bytes).decode("ascii")
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/jpeg;base64," + image_b64},
                    },
                ],
            }
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        # DeepSeek marks <td>/<tr> as special tokens. Dropping them joins
        # adjacent cells into false numbers, even when the OCR itself is right.
        "skip_special_tokens": False,
    }

    url = base_url.rstrip("/") + "/chat/completions"
    last_error: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            response = post_with_capacity_wait(session, url, json=payload, timeout=max(timeout, 480))
            response.raise_for_status()
            data = response.json()
            choice = data["choices"][0]
            content = choice["message"]["content"]
            finish_reason = choice.get("finish_reason") or choice.get("stop_reason")
            usage = data.get("usage")
            return content, finish_reason, usage
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt >= retries:
                break
            time.sleep(min(2**attempt, 8))
    raise RuntimeError(f"OCR request failed after {retries + 1} attempts: {last_error}")


def call_figure_ocr(**kwargs):
    """Retry truncated free-OCR with the model's grounded document prompt.

    Stamps/logos can cause repetition under Free OCR. Never accept truncated
    text, invent missing content, or silently discard the figure.
    """
    raw, finish, usage = call_ocr(**kwargs)
    if finish not in {None, "stop", "eos"}:
        logging.warning("figure OCR truncated (%s); retrying grounded transcription", finish)
        raw, finish, usage = call_ocr(**{**kwargs, "prompt": DEFAULT_PROMPT})
    if finish not in {None, "stop", "eos"}:
        raise ValueError(f"figure output incomplete: {finish}")
    if not clean_markdown(raw, False).strip():
        raise ValueError('figure OCR returned no readable text; review required')
    return raw, finish, usage


def assess_quality(
    *,
    page_number: int,
    markdown: str,
    raw_markdown: str,
    embedded_text: str,
    finish_reason: Optional[str],
    elapsed_sec: float,
) -> PageQuality:
    chars = len(markdown)
    cjk_chars = len(re.findall(r"[\u4e00-\u9fff]", markdown))
    tables = markdown.count("<table")
    headings = len(re.findall(r"(?m)^#{1,6}\s+", markdown))
    layout_tags = len(LAYOUT_TAG_RE.findall(raw_markdown))
    replacement_chars = markdown.count("\ufffd") + markdown.count("?")
    embedded_text_chars = len(embedded_text.strip())

    similarity: Optional[float] = None
    if embedded_text_chars >= 80 and chars >= 80:
        ref = normalize_for_compare(embedded_text)
        hyp = normalize_for_compare(markdown)
        if ref and hyp:
            similarity = SequenceMatcher(None, ref, hyp, autojunk=False).ratio()

    warnings: list[str] = []
    if chars < 80 and not (0 < embedded_text_chars < 80 and markdown.strip()):
        warnings.append("short_output")
    if finish_reason and finish_reason not in {"stop", "eos"}:
        warnings.append(f"finish_reason_{finish_reason}")
    if replacement_chars > max(5, chars // 100):
        warnings.append("many_replacement_or_question_marks")
    if embedded_text_chars >= 300 and similarity is not None and similarity < 0.35:
        warnings.append("low_similarity_to_embedded_text")
    if raw_markdown.strip() and not markdown.strip():
        warnings.append("cleaning_removed_all_text")

    ok = not warnings
    return PageQuality(
        page=page_number,
        ok=ok,
        chars=chars,
        cjk_chars=cjk_chars,
        tables=tables,
        headings=headings,
        layout_tags=layout_tags,
        replacement_chars=replacement_chars,
        embedded_text_chars=embedded_text_chars,
        embedded_text_similarity=similarity,
        finish_reason=finish_reason,
        elapsed_sec=round(elapsed_sec, 3),
        warnings=warnings,
    )


def markdown_document(pdf_path: Path, page_results: list[PageResult]) -> str:
    parts = [
        f"<!-- OCR source: {pdf_path.name} -->",
        "",
    ]
    for result in page_results:
        parts.append(f"<!-- page {result.page} -->")
        if result.error:
            parts.append(f"\n> OCR failed on page {result.page}: {result.error}\n")
        else:
            parts.append(result.markdown)
        parts.append("")
    return "\n".join(parts).strip() + "\n"


def quality_summary(pdf_path: Path, page_results: list[PageResult]) -> dict[str, Any]:
    qualities = [result.quality for result in page_results]
    ok_pages = sum(1 for quality in qualities if quality.ok)
    failed_pages = [result.page for result in page_results if result.error]
    warn_pages = {
        str(quality.page): quality.warnings
        for quality in qualities
        if quality.warnings
    }
    comparable = [
        q.embedded_text_similarity
        for q in qualities
        if q.embedded_text_similarity is not None
    ]
    avg_similarity = round(sum(comparable) / len(comparable), 4) if comparable else None

    return {
        "source_pdf": str(pdf_path),
        "pages": len(page_results),
        "ok_pages": ok_pages,
        "failed_pages": failed_pages,
        "warning_pages": warn_pages,
        "total_chars": sum(q.chars for q in qualities),
        "total_cjk_chars": sum(q.cjk_chars for q in qualities),
        "total_tables": sum(q.tables for q in qualities),
        "avg_embedded_text_similarity": avg_similarity,
        "page_quality": [asdict(q) for q in qualities],
    }


def failed_summary(pdf_path: Path, error: str) -> dict[str, Any]:
    return {
        "source_pdf": str(pdf_path),
        "pages": 0,
        "ok_pages": 0,
        "failed_pages": ["open_or_convert"],
        "warning_pages": {"pdf": ["pdf_error"]},
        "total_chars": 0,
        "total_cjk_chars": 0,
        "total_tables": 0,
        "avg_embedded_text_similarity": None,
        "error": error,
        "page_quality": [],
    }


def convert_pdf_text_fallback(
    *,
    pdf_path: Path,
    output_stem: Path,
    pages_spec: Optional[str],
    resume: bool,
) -> dict[str, Any]:
    output_stem.parent.mkdir(parents=True, exist_ok=True)
    md_path = output_stem.with_suffix(".md")
    json_path = output_stem.with_name(output_stem.name + "_quality.json")
    quality_md_path = output_stem.with_name(output_stem.name + "_quality.md")
    if resume and md_path.exists() and json_path.exists():
        logging.info("skip existing text fallback: %s", pdf_path)
        with json_path.open("r", encoding="utf-8") as fh:
            return json.load(fh)

    results: list[PageResult] = []
    with fitz.open(pdf_path) as doc:
        page_indexes = parse_page_range(pages_spec, len(doc))
        logging.info("text fallback converting %s (%s pages selected)", pdf_path, len(page_indexes))
        for index in page_indexes:
            page_number = index + 1
            started = time.monotonic()
            embedded_text = doc[index].get_text("text") or ""
            markdown = embedded_text.strip()
            elapsed = time.monotonic() - started
            quality = assess_quality(
                page_number=page_number,
                markdown=markdown,
                raw_markdown=markdown,
                embedded_text=embedded_text,
                finish_reason="text_fallback",
                elapsed_sec=elapsed,
            )
            quality.warnings.append("text_fallback")
            if not markdown:
                quality.ok = False
                quality.warnings.append("empty_embedded_text")
            results.append(
                PageResult(
                    page=page_number,
                    markdown=markdown,
                    raw_markdown=markdown,
                    quality=quality,
                    usage=None,
                    error=None if markdown else "empty embedded text",
                )
            )

    md_path.write_text(markdown_document(pdf_path, results), encoding="utf-8")
    summary = quality_summary(pdf_path, results)
    summary["error"] = "text_fallback_used"
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_quality_markdown(quality_md_path, summary)
    return summary


def append_warning_log(warning_log: Optional[Path], summary: dict[str, Any]) -> None:
    if not warning_log:
        return
    warning_pages = summary.get("warning_pages") or {}
    failed_pages = summary.get("failed_pages") or []
    if not warning_pages and not failed_pages and not summary.get("error"):
        return

    warning_log.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "source_pdf": summary.get("source_pdf"),
        "failed_pages": failed_pages,
        "warning_pages": warning_pages,
        "error": summary.get("error"),
    }
    with warning_log.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def write_quality_markdown(report_path: Path, summary: dict[str, Any]) -> None:
    lines = [
        f"# OCR Quality Report: {Path(summary['source_pdf']).name}",
        "",
        f"- Pages: {summary['pages']}",
        f"- OK pages: {summary['ok_pages']}/{summary['pages']}",
        f"- Failed pages: {', '.join(map(str, summary['failed_pages'])) or 'None'}",
        f"- Total chars: {summary['total_chars']}",
        f"- Total CJK chars: {summary['total_cjk_chars']}",
        f"- Tables: {summary['total_tables']}",
        f"- Avg embedded-text similarity: {summary['avg_embedded_text_similarity']}",
        "",
        "| Page | OK | Chars | CJK | Tables | Similarity | Warnings |",
        "| --- | --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for page in summary["page_quality"]:
        warnings = ", ".join(page["warnings"]) if page["warnings"] else ""
        similarity = page["embedded_text_similarity"]
        similarity_text = "" if similarity is None else f"{similarity:.3f}"
        lines.append(
            f"| {page['page']} | {page['ok']} | {page['chars']} | "
            f"{page['cjk_chars']} | {page['tables']} | {similarity_text} | "
            f"{warnings} |"
        )
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def output_stem_for(pdf_path: Path, output_dir: Path, input_root: Optional[Path]) -> Path:
    def safe_stem(stem: str, max_bytes: int = 150) -> str:
        if len(stem.encode("utf-8")) <= max_bytes:
            return stem
        digest = hashlib.sha1(stem.encode("utf-8", errors="replace")).hexdigest()[:12]
        suffix = "_" + digest
        budget = max_bytes - len(suffix.encode("utf-8"))
        encoded = stem.encode("utf-8", errors="replace")[:budget]
        while encoded:
            try:
                return encoded.decode("utf-8").rstrip("._- ") + suffix
            except UnicodeDecodeError:
                encoded = encoded[:-1]
        return digest

    if input_root and input_root.is_dir():
        rel = pdf_path.relative_to(input_root)
        stem = output_dir / rel.with_name(safe_stem(rel.stem)).with_suffix("")
    else:
        stem = output_dir / safe_stem(pdf_path.stem)
    stem.parent.mkdir(parents=True, exist_ok=True)
    return stem


def convert_pdf(
    *,
    pdf_path: Path,
    output_stem: Path,
    base_url: str,
    model: str,
    prompt: str,
    pages_spec: Optional[str],
    dpi: int,
    max_side: int,
    jpeg_quality: int,
    max_tokens: int,
    temperature: float,
    timeout: int,
    retries: int,
    keep_layout_tags: bool,
    save_raw: bool,
    figure_pass: bool,
    figure_prompt: str,
    figure_padding: int,
    resume: bool,
    rotate_clockwise: int = 0,
    page_rotations: Optional[dict[int, int]] = None,
    confirmed_blank_pages: Optional[list[int]] = None,
    blank_source_sha256: Optional[str] = None,
    retry_failed_pages: bool = False,
) -> dict[str, Any]:
    output_stem.parent.mkdir(parents=True, exist_ok=True)
    md_path = output_stem.with_suffix(".md")
    json_path = output_stem.with_name(output_stem.name + "_quality.json")
    quality_md_path = output_stem.with_name(output_stem.name + "_quality.md")
    raw_dir = output_stem.with_name(output_stem.name + "_raw_pages")
    page_rotations = page_rotations or {}
    confirmed_blank_pages = confirmed_blank_pages or []
    source_hash = sha256(pdf_path.read_bytes())
    if confirmed_blank_pages and blank_source_sha256 != source_hash:
        raise ValueError("Reviewed blank pages must be bound to this source SHA-256")

    if resume and md_path.exists() and json_path.exists():
        try:
            summary = validate_outputs(pdf_path, md_path, json_path)
            if summary.get("rotate_clockwise", 0) != rotate_clockwise:
                raise ValueError("OCR input orientation changed")
            if summary.get('page_rotations',{}) != {str(k):v for k,v in page_rotations.items()} or summary.get('confirmed_blank_pages',[]) != confirmed_blank_pages:
                raise ValueError('Page-specific input review changed')
        except (ValueError, OSError):
            logging.warning("existing output failed validation; retrying: %s", pdf_path)
        else:
            logging.info("skip existing validated output: %s", pdf_path)
            return summary

    cached = {}
    old_summary = {}
    if resume and retry_failed_pages and md_path.exists() and json_path.exists():
        try:
            old_summary=json.loads(json_path.read_text())
            if old_summary.get('source_sha256') != source_hash or old_summary.get('content_sha256') != sha256(md_path.read_bytes()):
                raise ValueError('Page cache checksum mismatch')
            pieces=re.split(r'(?m)^<!-- page (\d+) -->\s*\n',md_path.read_text())
            bodies={int(pieces[i]):pieces[i+1].strip() for i in range(1,len(pieces),2)}
            for quality in old_summary.get('page_quality',[]):
                n=quality['page']
                old_rotation=old_summary.get('page_rotations',{}).get(str(n),old_summary.get('rotate_clockwise',0))
                if quality['ok'] and n not in old_summary.get('failed_pages',[]) and n not in confirmed_blank_pages and bodies.get(n) and old_rotation==page_rotations.get(n,rotate_clockwise):
                    cached[n]=PageResult(n,bodies[n],bodies[n],PageQuality(**quality),None)
        except (ValueError,KeyError,TypeError,OSError):
            logging.warning('Ignoring invalid per-page cache for %s',pdf_path)
            cached={}
    results: list[PageResult] = []
    session = requests.Session()
    session.headers.update(api_headers())

    with fitz.open(pdf_path) as doc:
        page_indexes = parse_page_range(pages_spec, len(doc))
        if any(n<1 or n>len(doc) for n in [*page_rotations,*confirmed_blank_pages]):
            raise ValueError('Reviewed page number out of source range')
        if old_summary.get('pages') != len(doc):
            cached={}
        logging.info("converting %s (%s pages selected)", pdf_path, len(page_indexes))
        for index in page_indexes:
            page_number = index + 1
            if page_number in cached:
                results.append(cached[page_number])
                logging.info('reuse checksum-validated page %s',page_number)
                continue
            if page_number in confirmed_blank_pages:
                # Explicit human review, tied to immutable source content; an
                # empty model response alone never grants this exception.
                note='<!-- 人工核对：本页无正文，仅含页眉或页码。 -->'
                quality=assess_quality(page_number=page_number,markdown=note,raw_markdown=note,
                  embedded_text='',finish_reason='stop',elapsed_sec=0)
                quality.ok=True
                quality.warnings=['reviewed_blank_body']
                results.append(PageResult(page_number,note,note,quality,None))
                continue
            page = doc[index]
            embedded_text = page.get_text("text") or ""
            started = time.monotonic()
            try:
                rotation=page_rotations.get(page_number,rotate_clockwise)
                page_image = render_page_to_image(page, dpi, max_side, rotation)
                image_bytes = image_to_jpeg(page_image, jpeg_quality)
                raw_markdown, finish_reason, usage = call_ocr(
                    session=session,
                    base_url=base_url,
                    model=model,
                    image_bytes=image_bytes,
                    prompt=prompt,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    timeout=timeout,
                    retries=retries,
                )
                elapsed = time.monotonic() - started
                markdown = clean_markdown(raw_markdown, keep_layout_tags)
                figure_markdowns: list[str] = []
                figure_errors: list[str] = []
                if figure_pass:
                    image_boxes = extract_layout_boxes(raw_markdown, {"image", "figure"})
                    for _, box in image_boxes:
                        # Layout boxes refer to the rotated raster; they cannot
                        # safely clip native text in the original coordinates.
                        native_figure = None if rotation else native_vector_figure_text(page, box)
                        if native_figure:
                            figure_markdowns.append(native_figure)
                            continue
                        if is_small_figure(page_image, box):
                            continue
                        crop = crop_normalized_box(page_image, box, figure_padding)
                        try:
                            figure_raw, figure_finish, _ = call_figure_ocr(
                                session=session,
                                base_url=base_url,
                                model=model,
                                image_bytes=image_to_jpeg(crop, jpeg_quality),
                                prompt=figure_prompt,
                                max_tokens=max(1024, min(max_tokens, 4096)),
                                temperature=temperature,
                                timeout=timeout,
                                retries=retries,
                            )
                            if figure_finish not in {None, "stop", "eos"}:
                                raise ValueError(f"figure output incomplete: {figure_finish}")
                            figure_markdowns.append(clean_markdown(figure_raw, keep_layout_tags))
                        except Exception as exc:  # noqa: BLE001
                            figure_errors.append(str(exc))
                            logging.warning(
                                "page %s figure OCR failed for box %s: %s",
                                page_number,
                                box,
                                exc,
                            )
                    markdown = append_figure_markdown(markdown, figure_markdowns)
                quality = assess_quality(
                    page_number=page_number,
                    markdown=markdown,
                    raw_markdown=raw_markdown,
                    embedded_text=embedded_text,
                    finish_reason=finish_reason,
                    elapsed_sec=elapsed,
                )
                page_errors = []
                if finish_reason not in {None, "stop", "eos"}:
                    page_errors.append(f"page output incomplete: {finish_reason}")
                if not markdown.strip():
                    page_errors.append("empty page output")
                if figure_errors:
                    page_errors.append("figure OCR failed: " + "; ".join(figure_errors))
                result = PageResult(
                    page=page_number,
                    markdown=markdown,
                    raw_markdown=raw_markdown,
                    quality=quality,
                    usage=usage,
                    error="; ".join(page_errors) if page_errors else None,
                )
                if figure_errors:
                    result.quality.ok = False
                    result.quality.warnings.append("figure_ocr_error")
                logging.info(
                    "page %s done: chars=%s warnings=%s",
                    page_number,
                    quality.chars,
                    ",".join(quality.warnings) or "none",
                )
            except Exception as exc:  # noqa: BLE001
                elapsed = time.monotonic() - started
                error = str(exc)
                logging.exception("page %s failed: %s", page_number, error)
                quality = assess_quality(
                    page_number=page_number,
                    markdown="",
                    raw_markdown="",
                    embedded_text=embedded_text,
                    finish_reason=None,
                    elapsed_sec=elapsed,
                )
                quality.ok = False
                quality.warnings.append("ocr_error")
                result = PageResult(
                    page=page_number,
                    markdown="",
                    raw_markdown="",
                    quality=quality,
                    usage=None,
                    error=error,
                )
            results.append(result)

            if save_raw and result.raw_markdown:
                raw_dir.mkdir(parents=True, exist_ok=True)
                raw_path = raw_dir / f"page_{page_number:04d}.md"
                raw_path.write_text(result.raw_markdown, encoding="utf-8")

    if sha256(pdf_path.read_bytes()) != source_hash:
        raise ValueError("source changed during PDF conversion")
    md_path.write_text(markdown_document(pdf_path, results), encoding="utf-8")
    summary = quality_summary(pdf_path, results)
    summary["source_sha256"] = source_hash
    summary["content_sha256"] = sha256(md_path.read_bytes())
    summary.update(attachment_format="pdf", extraction_method="pdf_ocr",
                   parser_version="pdf-ocr-layout-v3", search_full_body=True,
                   rotate_clockwise=rotate_clockwise,
                   page_rotations={str(k):v for k,v in page_rotations.items()},
                   confirmed_blank_pages=confirmed_blank_pages,
                   cached_pages=[r.page for r in results if r.page in cached])
    json_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    write_quality_markdown(quality_md_path, summary)
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert PDF files to OCR markdown through DeepSeek-OCR-2/vLLM.",
    )
    parser.add_argument("input", type=Path, help="PDF file or directory.")
    parser.add_argument("-o", "--output-dir", type=Path, default=Path("OCR_MD"))
    parser.add_argument("--glob", default="*.pdf", help="Directory mode PDF glob.")
    parser.add_argument("--recursive", action="store_true", help="Recurse directories.")
    parser.add_argument("--limit", type=int, default=0, help="Max PDFs to process.")
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Number of PDFs to convert concurrently.",
    )
    parser.add_argument(
        "--sort-by",
        choices=["path", "mtime", "ctime"],
        default="path",
        help="PDF processing order. Use mtime for oldest completed downloads first.",
    )
    parser.add_argument(
        "--min-age-seconds",
        type=int,
        default=0,
        help="Skip PDFs modified more recently than this many seconds.",
    )
    parser.add_argument("--pages", help="One-based page range, e.g. 1-3,5.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument(
        "--model",
        default=os.getenv("FINANCE_OCR_MODEL"),
        help="Model id. Defaults to FINANCE_OCR_MODEL or the first /v1/models entry.",
    )
    parser.add_argument(
        "--text-fallback-on-backend-error",
        action="store_true",
        help="When the OCR backend is unavailable, extract embedded PDF text instead of failing the whole PDF.",
    )
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--dpi", type=int, default=144)
    parser.add_argument("--max-side", type=int, default=1800)
    parser.add_argument("--jpeg-quality", type=int, default=88)
    parser.add_argument("--max-tokens", type=int, default=4096)
    parser.add_argument("--rotate-clockwise", type=int, choices=(0, 90, 180, 270), default=0,
                        help="Explicit OCR input rotation; leaves the source PDF unchanged.")
    parser.add_argument('--page-rotation', action='append', default=[], help='Reviewed page:clockwise_degrees override.')
    parser.add_argument('--confirmed-blank-page', action='append', type=int, default=[])
    parser.add_argument('--blank-source-sha256', help='Required source checksum for human-confirmed blank pages.')
    parser.add_argument('--retry-failed-pages', action='store_true', help='Reuse successful pages only when source and cached output checksums match.')
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--keep-layout-tags", action="store_true")
    parser.add_argument("--save-raw", action="store_true")
    parser.add_argument(
        "--no-figure-pass",
        action="store_true",
        help="Disable the second OCR pass for detected chart/figure regions.",
    )
    parser.add_argument("--figure-prompt", default=DEFAULT_FIGURE_PROMPT)
    parser.add_argument(
        "--figure-padding",
        type=int,
        default=35,
        help="Padding in model coordinate units around detected figure boxes.",
    )
    parser.add_argument("--no-resume", action="store_true")
    parser.add_argument("--no-progress", action="store_true")
    parser.add_argument(
        "--warning-log",
        type=Path,
        help="Append JSONL records for PDFs with warnings or failures.",
    )
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    input_path = args.input.resolve()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        pdfs = iter_pdfs(input_path, args.glob, args.recursive)
        pdfs = order_and_filter_pdfs(pdfs, args.sort_by, args.min_age_seconds)
        if args.limit:
            pdfs = pdfs[: args.limit]
        if not pdfs:
            raise RuntimeError(f"no PDF files found under {input_path}")

        formats = {path: detect_format(path.read_bytes())[0] for path in pdfs}
        needs_pdf_backend = any(kind == "pdf" for kind in formats.values())
        backend_error: Exception | None = None
        model = args.model
        if needs_pdf_backend and not model:
            try:
                model = get_first_model(args.base_url, args.timeout)
            except Exception as exc:  # noqa: BLE001
                backend_error = exc
                if not args.text_fallback_on_backend_error:
                    raise
                logging.warning("OCR backend unavailable, using embedded text fallback: %s", exc)
        logging.info("using model: %s", model or "text_fallback")
        logging.info("using workers: %s", args.workers)

        summaries = []
        pdf_iter = tqdm(
            total=len(pdfs),
            desc="PDF OCR",
            unit="pdf",
            dynamic_ncols=True,
            disable=args.no_progress,
        )

        def process_pdf(pdf_path: Path) -> dict[str, Any]:
            input_root = input_path if input_path.is_dir() else None
            output_stem = output_stem_for(pdf_path.resolve(), output_dir, input_root)
            try:
                if formats[pdf_path] != "pdf":
                    return convert_native(pdf_path, output_dir)
                if backend_error is not None:
                    return convert_pdf_text_fallback(
                        pdf_path=pdf_path.resolve(),
                        output_stem=output_stem,
                        pages_spec=args.pages,
                        resume=not args.no_resume,
                    )
                return convert_pdf(
                    pdf_path=pdf_path.resolve(),
                    output_stem=output_stem,
                    base_url=args.base_url,
                    model=model,
                    prompt=args.prompt,
                    pages_spec=args.pages,
                    dpi=args.dpi,
                    max_side=args.max_side,
                    jpeg_quality=args.jpeg_quality,
                    max_tokens=args.max_tokens,
                    temperature=args.temperature,
                    timeout=args.timeout,
                    retries=args.retries,
                    keep_layout_tags=args.keep_layout_tags,
                    save_raw=args.save_raw,
                    figure_pass=not args.no_figure_pass,
                    figure_prompt=args.figure_prompt,
                    figure_padding=args.figure_padding,
                    resume=not args.no_resume,
                    rotate_clockwise=args.rotate_clockwise,
                    page_rotations={int(v.split(':')[0]):int(v.split(':')[1]) for v in args.page_rotation},
                    confirmed_blank_pages=args.confirmed_blank_page,
                    blank_source_sha256=args.blank_source_sha256,
                    retry_failed_pages=args.retry_failed_pages,
                )
            except Exception as exc:  # noqa: BLE001
                logging.exception("PDF failed: %s", pdf_path)
                return failed_summary(pdf_path.resolve(), str(exc))

        workers = max(1, args.workers)
        if workers == 1:
            for pdf_path in pdfs:
                pdf_iter.set_postfix_str(pdf_path.name[:40])
                summary = process_pdf(pdf_path)
                append_warning_log(args.warning_log, summary)
                summaries.append(summary)
                pdf_iter.update(1)
        else:
            next_index = 0
            pending: dict[Future[dict[str, Any]], Path] = {}
            with ThreadPoolExecutor(max_workers=workers) as executor:
                while next_index < len(pdfs) and len(pending) < workers:
                    pdf_path = pdfs[next_index]
                    pending[executor.submit(process_pdf, pdf_path)] = pdf_path
                    next_index += 1

                while pending:
                    done, _ = wait(pending, return_when=FIRST_COMPLETED)
                    for future in done:
                        pdf_path = pending.pop(future)
                        try:
                            summary = future.result()
                        except Exception as exc:  # noqa: BLE001
                            logging.exception("PDF worker failed: %s", pdf_path)
                            summary = failed_summary(pdf_path.resolve(), str(exc))
                        append_warning_log(args.warning_log, summary)
                        summaries.append(summary)
                        pdf_iter.set_postfix_str(pdf_path.name[:40])
                        pdf_iter.update(1)

                        if next_index < len(pdfs):
                            next_path = pdfs[next_index]
                            pending[executor.submit(process_pdf, next_path)] = next_path
                            next_index += 1

        pdf_iter.close()

        index_path = output_dir / "ocr_index.json"
        index_path.write_text(
            json.dumps(summaries, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        logging.info("done. index: %s", index_path)
        failed = [s for s in summaries if s.get("failed_pages") or (s.get("error") and s.get("error") != "text_fallback_used")]
        if failed:
            logging.error("attachment conversion failed for %s file(s): %s", len(failed), "; ".join(str(s.get("error") or s.get("failed_pages")) for s in failed))
            return 1
        return 0
    except Exception as exc:  # noqa: BLE001
        logging.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
