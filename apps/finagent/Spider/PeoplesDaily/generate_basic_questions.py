#!/usr/bin/env python3

from xtrastock.config import bootstrap_environment
bootstrap_environment()

import argparse
import concurrent.futures
import csv
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


PREFIX_BUCKETS = [
    ("32k", 0, 32_000),
    ("64k", 32_000, 64_000),
    ("128k", 64_000, 128_000),
    ("256k", 128_000, 256_000),
    ("512k", 256_000, 512_000),
    ("1m", 512_000, 1_000_000),
    ("2m", 1_000_000, 2_000_000),
    ("4m", 2_000_000, 4_000_000),
]

ANSWER_TYPES = ["number", "date", "location", "person", "organization", "phrase"]


def stable_int(value: str, salt: str = "") -> int:
    digest = hashlib.md5((salt + value).encode("utf-8")).hexdigest()
    return int(digest[:12], 16)


def load_news(csv_path: Path) -> dict[str, dict]:
    csv.field_size_limit(sys.maxsize)
    news = {}
    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            news[row["id"]] = {
                "news_id": row["id"],
                "date": row["date"],
                "title": (row.get("title") or "").strip(),
                "content": (row.get("content") or "").strip(),
            }
    return news


def clean_content_for_generation(content: str) -> str:
    # Keep evidence anchored in article text, but hide newspaper mastheads that
    # create low-value questions about issue dates, editions, and source names.
    lines = []
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("《人民日报》") and "第" in stripped and "版" in stripped:
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def bucket_for_doc(row: dict) -> str | None:
    center = (row["start_token"] + row["end_token"]) // 2
    for name, start, end in PREFIX_BUCKETS:
        if start <= center < end:
            return name
    return None


def assign_basic_difficulty(rows: list[dict]) -> list[dict]:
    by_bucket: dict[str, list[dict]] = {}
    for row in rows:
        bucket = bucket_for_doc(row)
        if bucket is None:
            continue
        item = dict(row)
        item["bucket"] = bucket
        by_bucket.setdefault(bucket, []).append(item)

    assigned = []
    for bucket, docs in by_bucket.items():
        docs.sort(key=lambda row: stable_int(row["id"], "basic-" + bucket))
        split = len(docs) // 2
        for i, row in enumerate(docs):
            row["difficulty"] = "easy" if i < split else "medium"
            row["answer_type"] = ANSWER_TYPES[stable_int(row["id"], "answer-type") % len(ANSWER_TYPES)]
            assigned.append(row)

    assigned.sort(key=lambda row: row["seq"])
    return assigned


def select_sample(rows: list[dict], limit_per_bucket: int | None) -> list[dict]:
    if not limit_per_bucket:
        return rows
    picked = []
    for bucket, _, _ in PREFIX_BUCKETS:
        bucket_rows = [row for row in rows if row["bucket"] == bucket]
        easy = [row for row in bucket_rows if row["difficulty"] == "easy"]
        medium = [row for row in bucket_rows if row["difficulty"] == "medium"]
        easy.sort(key=lambda row: stable_int(row["id"], "sample-easy-" + bucket))
        medium.sort(key=lambda row: stable_int(row["id"], "sample-medium-" + bucket))
        half = limit_per_bucket // 2
        picked.extend(easy[:half])
        picked.extend(medium[: limit_per_bucket - half])
    picked.sort(key=lambda row: row["seq"])
    return picked


def build_prompt(item: dict, news: dict) -> str:
    difficulty = item["difficulty"]
    common = f"""输出严格 JSON 对象，不要 Markdown，不要解释。

字段必须为：news_id, question, answer, evidence, answer_type, difficulty, reasoning。
news_id 必须是：{item["id"]}
answer_type 必须严格等于：{item["answer_type"]}
difficulty 必须严格等于：{difficulty}

answer_type 只能是：number, date, location, person, organization, phrase。
问题必须包含新闻标题或日期作为定位锚点，但答案不能直接出现在问题中。
答案必须是短答案，适合自动评测。
number 类型答案必须包含单位，例如“1025亿元”“3500亿美元”“4640户”。
organization 类型必须是机构、公司、部门、学校或组织，不要把会议、峰会、活动、项目当作 organization。
evidence 必须是原文连续子串，不允许省略号，不允许改写，不允许拼接。
答案必须是 evidence 中的完全连续子串，不能补字、换词、改写或改变单位。
date 字段只能用作定位锚点，不能作为答案或 evidence；date 类型必须询问正文中出现的事件日期、会议日期、活动时间等。
不要围绕作者署名、记者、通讯员、本报记者、报纸名称、刊发版面、第几版、电头来源、记者署名、本文刊载于哪里、这篇新闻的发布日期来出题；优先问正文中的事件、人物行动、数据、地点、机构、政策、产品、技术或结论。
"""
    if difficulty == "easy":
        diff_text = """EASY 定义：单 evidence、单句、直接抽取。
- 只能依赖 1 条 evidence。
- 问题定位到新闻后，答案应能从同一句话直接读出。
- 不需要场景推理、条件组合或跨句关联。
- 不要问电头来源地、作者署名、记者、刊发版面，优先选择正文事实。
"""
    else:
        diff_text = """MEDIUM 定义：具体场景定位 + 显式事实抽取。
- 问题需要先定位到新闻中的具体事件、人物动作、协议条款、技术说明或场景，再抽取答案。
- 答案仍必须在 evidence 中直接出现。
- 不要只问电头、标题附近或全文最显眼的第一数字。
- 不需要多证据推理，但要比 easy 多一层场景限定。
"""
    prompt_news = dict(news)
    prompt_news["content"] = clean_content_for_generation(prompt_news["content"])
    return common + "\n" + diff_text + "\n新闻：\n" + json.dumps(prompt_news, ensure_ascii=False, indent=2)


def extract_text(response: dict) -> str:
    texts = []
    for item in response.get("output", []):
        for content in item.get("content", []):
            if content.get("type") in ("output_text", "text") and "text" in content:
                texts.append(content["text"])
    return "\n".join(texts).strip()


def call_api(item: dict, news: dict, api_key: str, url: str, model: str, retries: int) -> dict:
    prompt = build_prompt(item, news)
    payload = {
        "model": model,
        "instructions": "你是严谨的数据集构建助手，负责为中文长上下文新闻问答评测生成高质量、可自动评分的问题。",
        "input": prompt,
        "reasoning": {"effort": "low", "summary": "auto"},
        "text": {"format": {"type": "text"}},
        "store": False,
    }
    last_error = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                response = json.loads(resp.read().decode("utf-8"))
            text = extract_text(response)
            result = json.loads(text)
            result["bucket"] = item["bucket"]
            result["seq"] = item["seq"]
            result["start_token"] = item["start_token"]
            result["end_token"] = item["end_token"]
            result["usage"] = response.get("usage")
            return result
        except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, KeyError) as exc:
            last_error = str(exc)
            time.sleep(min(2**attempt, 8))
    return {
        "news_id": item["id"],
        "bucket": item["bucket"],
        "seq": item["seq"],
        "difficulty": item["difficulty"],
        "answer_type": item["answer_type"],
        "error": last_error,
    }


def validate_question(question: dict, source_text: str) -> list[str]:
    issues = []
    if question.get("difficulty") not in {"easy", "medium"}:
        issues.append("bad_difficulty")
    if question.get("answer_type") not in set(ANSWER_TYPES):
        issues.append("bad_answer_type")
    evidence = question.get("evidence")
    if not isinstance(evidence, str):
        issues.append("bad_evidence_type")
        evidence = ""
    answer = str(question.get("answer", ""))
    if answer and answer not in evidence:
        issues.append("answer_not_in_evidence")
    if evidence and evidence not in source_text:
        issues.append("evidence_not_exact")
    return issues


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", type=Path, default=Path("clean_data/RenMin_Daily_2026.csv"))
    parser.add_argument("--metadata", type=Path, default=Path("clean_data/long_context_2026_4m/renmin_daily_2026_4m_metadata.jsonl"))
    parser.add_argument("--output", type=Path, default=Path("clean_data/long_context_2026_4m/basic_questions_sample.jsonl"))
    parser.add_argument("--model", default=os.environ["FINANCE_AGENT_V3_MODEL"])
    parser.add_argument("--api-url", default=os.environ["FINANCE_API_BASE_URL"].rstrip('/') + '/responses')
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--limit-per-bucket", type=int, default=20)
    parser.add_argument("--retries", type=int, default=2)
    args = parser.parse_args()

    api_key = os.environ.get("FINANCE_API_KEY")
    if not api_key:
        raise SystemExit("FINANCE_API_KEY is required")

    news_by_id = load_news(args.csv)
    metadata = [json.loads(line) for line in args.metadata.open(encoding="utf-8")]
    assigned = assign_basic_difficulty(metadata)
    selected = select_sample(assigned, args.limit_per_bucket)

    done_ids = set()
    if args.output.exists():
        with args.output.open(encoding="utf-8") as f:
            for line in f:
                try:
                    done_ids.add(json.loads(line)["news_id"])
                except Exception:
                    pass
    todo = [row for row in selected if row["id"] not in done_ids]

    args.output.parent.mkdir(parents=True, exist_ok=True)
    source_texts = {
        news_id: item["title"] + "\n" + item["content"]
        for news_id, item in news_by_id.items()
    }
    print(f"selected={len(selected)} done={len(done_ids)} todo={len(todo)} concurrency={args.concurrency}")

    with args.output.open("a", encoding="utf-8") as out:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as executor:
            futures = {
                executor.submit(call_api, row, news_by_id[row["id"]], api_key, args.api_url, args.model, args.retries): row
                for row in todo
            }
            completed = 0
            for future in concurrent.futures.as_completed(futures):
                row = futures[future]
                result = future.result()
                if "error" not in result:
                    result["validation_issues"] = validate_question(result, source_texts[row["id"]])
                out.write(json.dumps(result, ensure_ascii=False) + "\n")
                out.flush()
                completed += 1
                if completed % 10 == 0 or completed == len(todo):
                    print(f"completed={completed}/{len(todo)}")


if __name__ == "__main__":
    main()
