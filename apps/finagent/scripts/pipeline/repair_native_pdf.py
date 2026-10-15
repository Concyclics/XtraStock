"""Rebuild a reviewed native-text PDF, OCRing every non-branding raster image.

The source-hashed review must classify every displayed raster image. Unknown
images, empty native pages, truncation and source changes stop publication.
"""

from xtrastock.config import bootstrap_environment
bootstrap_environment()

from pathlib import Path
import argparse
import io
import json
import os
import sys
import time

from common import ROOT, load_env
sys.path.insert(0, str(ROOT))
import fitz
from PIL import Image
import requests
import pdf_ocr_markdown as ocr
from report_attachments import atomic_write, output_paths, sha256


def repair(source: Path, review_path: Path, destination: Path) -> dict:
    review = json.loads(review_path.read_text())
    source_hash = sha256(source.read_bytes())
    if source_hash != review["source_sha256"]:
        raise ValueError("image review does not match PDF")
    ignored = set(review["branding_image_xrefs"])
    required = set(review["data_image_xrefs"])
    if ignored & required:
        raise ValueError("ambiguous image classification")
    destination.mkdir(parents=True, exist_ok=True)
    cache = destination / "image-checkpoints"; cache.mkdir(exist_ok=True)
    base = os.getenv("FINANCE_OCR_BASE_URL", os.getenv("FINANCE_API_BASE_URL", ""))
    from urllib.parse import urlparse
    if urlparse(base).hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("only local OCR is allowed")
    session = requests.Session(); session.headers.update(ocr.api_headers())
    model = os.getenv("FINANCE_OCR_MODEL")
    results, evidence, seen, image_reviews = [], [], set(), {}
    with fitz.open(source) as doc:
        for number, page in enumerate(doc, 1):
            started = time.monotonic()
            native = page.get_text("text", sort=True).strip()
            if not native:
                raise ValueError(f"page {number} has no native text; manual/scanned-page treatment required")
            xrefs = list(dict.fromkeys(x["xref"] for x in page.get_image_info(xrefs=True)))
            if set(xrefs) - ignored - required:
                raise ValueError(f"page {number}: unreviewed raster images")
            seen.update(xrefs)
            figures = []
            for xref in xrefs:
                if xref in ignored:
                    continue
                checkpoint = cache / f"image-{xref}.json"
                saved = json.loads(checkpoint.read_text()) if checkpoint.exists() else {}
                if saved.get("source_sha256") == source_hash and saved.get("finish_reason") in {"stop", "eos"}:
                    if saved.get("manual_review"):
                        image_reviews[str(xref)] = saved["manual_review"]
                    figures.append(saved["markdown"]); continue
                data = doc.extract_image(xref)["image"]
                image = Image.open(io.BytesIO(data)).convert("RGB")
                if image.width < 1400:
                    image = image.resize((image.width * 2, image.height * 2), Image.Resampling.LANCZOS)
                model = model or ocr.get_first_model(base, 180)
                raw, finish, usage = ocr.call_ocr(session=session, base_url=base, model=model,
                    image_bytes=ocr.image_to_jpeg(image, 96), prompt=ocr.DEFAULT_FIGURE_PROMPT,
                    max_tokens=4096, temperature=0, timeout=180, retries=2)
                text = ocr.clean_markdown(raw, False)
                atomic_write(checkpoint, json.dumps({"source_sha256": source_hash, "page": number,
                    "xref": xref, "markdown": text, "raw": raw, "finish_reason": finish,
                    "usage": usage}, ensure_ascii=False, indent=2).encode())
                if finish not in {"stop", "eos"} or not text.strip():
                    raise ValueError(f"page {number}, image {xref}: incomplete OCR ({finish})")
                figures.append(text)
            body = ocr.append_figure_markdown(native, figures)
            quality = ocr.assess_quality(page_number=number, markdown=body, raw_markdown=body,
                embedded_text=native, finish_reason="stop", elapsed_sec=time.monotonic()-started)
            if any(image_reviews.get(str(x), {}).get("uncertain_text") for x in xrefs):
                quality.warnings.append("source_image_small_text_uncertain")
                quality.ok = False
            results.append(ocr.PageResult(number, body, body, quality, None))
            evidence.append({"page": number, "native_chars": len(native),
                "native_sha256": sha256(native.encode()), "native_text_preserved": native in body,
                "data_image_xrefs": [x for x in xrefs if x in required]})
            if figures:
                print(json.dumps({"page": number, "raster_images": len(figures)},ensure_ascii=False),flush=True)
    if seen != ignored | required or sha256(source.read_bytes()) != source_hash:
        raise ValueError("image review/source changed during conversion")
    body = ocr.markdown_document(source, results)
    summary = ocr.quality_summary(source, results)
    summary.update(source_sha256=source_hash, content_sha256=sha256(body.encode()),
        attachment_format="pdf", extraction_method="pdf_native_with_image_ocr" if required else "pdf_native_text",
        parser_version="pdf-native-reviewed-v1", search_full_body=True,
        native_text_verified_pages=evidence, reviewed_branding_image_xrefs=sorted(ignored),
        raster_data_images_processed=sorted(required), manual_image_review=image_reviews)
    md,q,qm = output_paths(source,destination)
    atomic_write(md,body.encode());atomic_write(q,(json.dumps(summary,ensure_ascii=False,indent=2)+"\n").encode())
    ocr.write_quality_markdown(qm,summary)
    return summary


def main():
    parser=argparse.ArgumentParser();parser.add_argument("source",type=Path)
    parser.add_argument("review",type=Path);parser.add_argument("destination",type=Path)
    args=parser.parse_args();os.environ.update(load_env(ROOT/".env.finance-agent-v3"))
    summary=repair(args.source,args.review,args.destination)
    print(json.dumps({"pages":summary["pages"],"failed_pages":summary["failed_pages"]}),flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
