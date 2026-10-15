#!/usr/bin/env python3
import argparse
import csv
import difflib
import hashlib
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

from tokenizers import Tokenizer


def normalize_for_dedupe(text: str) -> str:
    text = text or ""
    text = re.sub(r"《人民日报》（\d{4}年\d{2}月\d{2}日第\d+版）", "", text)
    text = re.sub(r"\s+", "", text)
    return text


def format_news(row: dict) -> str:
    title = (row.get("title") or "").strip()
    content = (row.get("content") or "").strip()
    return (
        f"[NEWS_ID: {row['id']}]\n"
        f"[DATE: {row['date']}]\n"
        f"[TITLE: {title}]\n"
        "[CONTENT]\n"
        f"{content}\n"
        "[/NEWS]\n"
    )


def load_rows(csv_path: Path, tokenizer: Tokenizer) -> list[dict]:
    csv.field_size_limit(sys.maxsize)
    rows = []
    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            formatted = format_news(row)
            rows.append(
                {
                    "id": row["id"],
                    "date": row["date"],
                    "title": (row.get("title") or "").strip(),
                    "content": row.get("content") or "",
                    "formatted": formatted,
                    "tokens": len(tokenizer.encode(formatted).ids),
                    "norm_title": normalize_for_dedupe(row.get("title") or ""),
                    "norm_content": normalize_for_dedupe(row.get("content") or ""),
                }
            )
    return rows


def find_near_duplicates(rows: list[dict], threshold: float) -> dict[int, int]:
    by_title = defaultdict(list)
    for i, row in enumerate(rows):
        by_title[row["norm_title"]].append(i)

    duplicate_of = {}
    for title, indexes in by_title.items():
        if not title:
            continue
        representatives = []
        for i in indexes:
            content = rows[i]["norm_content"]
            matched = None
            for j in representatives:
                rep_content = rows[j]["norm_content"]
                if not content or not rep_content:
                    continue
                length_ratio = min(len(content), len(rep_content)) / max(len(content), len(rep_content))
                if length_ratio < 0.92:
                    continue

                content_sig = content[:300] + content[-300:]
                rep_sig = rep_content[:300] + rep_content[-300:]
                quick_ratio = difflib.SequenceMatcher(None, content_sig, rep_sig).ratio()
                if quick_ratio < 0.96:
                    continue

                if len(content) > 10000 and quick_ratio >= 0.985:
                    ratio = quick_ratio
                else:
                    ratio = difflib.SequenceMatcher(None, content, rep_content).ratio()
                if ratio >= threshold:
                    matched = j
                    break

            if matched is None:
                representatives.append(i)
            else:
                duplicate_of[i] = matched

    return duplicate_of


def stable_jitter(row: dict) -> float:
    digest = hashlib.md5(row["id"].encode("utf-8")).hexdigest()
    return int(digest[:12], 16) / float(16**12)


def select_evenly_by_token_budget(candidates: list[dict], target_tokens: int) -> list[dict]:
    total = sum(row["tokens"] for row in candidates)
    if total <= target_tokens:
        return list(candidates)

    # Deterministic weighted downsampling: rank by how well each document's
    # cumulative token position aligns with a regular target-token grid.
    ratio = target_tokens / total
    scored = []
    cumulative = 0
    for row in candidates:
        center = cumulative + row["tokens"] / 2
        grid_distance = abs((center * ratio) - round(center * ratio / 1000) * 1000)
        score = grid_distance + stable_jitter(row)
        scored.append((score, row))
        cumulative += row["tokens"]

    selected = []
    selected_tokens = 0
    for _, row in sorted(scored, key=lambda item: item[0]):
        if selected_tokens + row["tokens"] <= target_tokens:
            selected.append(row)
            selected_tokens += row["tokens"]

    selected.sort(key=lambda row: (row["date"], row["id"]))
    return selected


def write_outputs(selected: list[dict], duplicate_of: dict[int, int], rows: list[dict], args: argparse.Namespace) -> None:
    args.output_dir.mkdir(parents=True, exist_ok=True)
    context_path = args.output_dir / "renmin_daily_2026_4m_context.txt"
    metadata_path = args.output_dir / "renmin_daily_2026_4m_metadata.jsonl"
    report_path = args.output_dir / "renmin_daily_2026_4m_report.json"

    token_offset = 0
    with context_path.open("w", encoding="utf-8") as context_f, metadata_path.open("w", encoding="utf-8") as meta_f:
        for idx, row in enumerate(selected, 1):
            if idx > 1:
                context_f.write("\n")
                token_offset += args.separator_tokens
            start_token = token_offset
            context_f.write(row["formatted"])
            token_offset += row["tokens"]
            end_token = token_offset
            meta_f.write(
                json.dumps(
                    {
                        "seq": idx,
                        "id": row["id"],
                        "date": row["date"],
                        "title": row["title"],
                        "tokens": row["tokens"],
                        "start_token": start_token,
                        "end_token": end_token,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

    selected_ids = {row["id"] for row in selected}
    candidates = [
        row
        for i, row in enumerate(rows)
        if i not in duplicate_of and args.min_tokens <= row["tokens"] <= args.max_tokens
    ]
    report = {
        "source_csv": str(args.csv),
        "tokenizer_json": str(args.tokenizer),
        "target_tokens": args.target_tokens,
        "actual_context_tokens_estimate": token_offset,
        "selected_rows": len(selected),
        "selected_doc_tokens": sum(row["tokens"] for row in selected),
        "selected_min_date": min(row["date"] for row in selected) if selected else None,
        "selected_max_date": max(row["date"] for row in selected) if selected else None,
        "input_rows": len(rows),
        "input_doc_tokens": sum(row["tokens"] for row in rows),
        "dedupe_threshold": args.dedupe_threshold,
        "duplicate_rows_removed": len(duplicate_of),
        "min_tokens": args.min_tokens,
        "max_tokens": args.max_tokens,
        "candidate_rows_after_filter": len(candidates),
        "candidate_doc_tokens_after_filter": sum(row["tokens"] for row in candidates),
        "excluded_too_short": sum(1 for i, row in enumerate(rows) if i not in duplicate_of and row["tokens"] < args.min_tokens),
        "excluded_too_long": sum(1 for i, row in enumerate(rows) if i not in duplicate_of and row["tokens"] > args.max_tokens),
        "unselected_candidate_rows": sum(1 for row in candidates if row["id"] not in selected_ids),
        "context_path": str(context_path),
        "metadata_path": str(metadata_path),
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", type=Path, default=Path("clean_data/RenMin_Daily_2026.csv"))
    parser.add_argument("--tokenizer", type=Path, default=Path.home() / ".cache" / "qwen3_5_9b_tokenizer.json")
    parser.add_argument("--output-dir", type=Path, default=Path("clean_data/long_context_2026_4m"))
    parser.add_argument("--target-tokens", type=int, default=4_000_000)
    parser.add_argument("--min-tokens", type=int, default=200)
    parser.add_argument("--max-tokens", type=int, default=4000)
    parser.add_argument("--dedupe-threshold", type=float, default=0.97)
    parser.add_argument("--separator-tokens", type=int, default=0)
    args = parser.parse_args()

    tokenizer = Tokenizer.from_file(str(args.tokenizer))
    rows = load_rows(args.csv, tokenizer)
    duplicate_of = find_near_duplicates(rows, args.dedupe_threshold)
    candidates = [
        row
        for i, row in enumerate(rows)
        if i not in duplicate_of and args.min_tokens <= row["tokens"] <= args.max_tokens
    ]
    candidates.sort(key=lambda row: (row["date"], row["id"]))
    selected = select_evenly_by_token_budget(candidates, args.target_tokens)
    write_outputs(selected, duplicate_of, rows, args)

    print(f"input_rows={len(rows)}")
    print(f"duplicate_rows_removed={len(duplicate_of)}")
    print(f"candidate_rows_after_filter={len(candidates)}")
    print(f"candidate_doc_tokens_after_filter={sum(row['tokens'] for row in candidates)}")
    print(f"selected_rows={len(selected)}")
    print(f"selected_doc_tokens={sum(row['tokens'] for row in selected)}")
    print(f"selected_date_range={selected[0]['date']}..{selected[-1]['date']}")
    print(f"output_dir={args.output_dir}")


if __name__ == "__main__":
    main()
