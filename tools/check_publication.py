"""Inspect exactly the Git index; never print matched secret values."""
from __future__ import annotations

import argparse
import ipaddress
import json
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]


def inspect_text(text: str) -> list[tuple[int, str]]:
    findings = []
    patterns = {
        "private-key": r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
        "access-token": r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|sk-[A-Za-z0-9_-]{24,}|AKIA[A-Z0-9]{16})\b",
        "url-credentials": r"[a-z]+://[^\s/:]+:[^\s/@]+@",
        "personal-home": r"/(?:home|Users)/[A-Za-z0-9_.-]+/",
    }
    for number, line in enumerate(text.splitlines(), 1):
        for kind, pattern in patterns.items():
            if re.search(pattern, line):
                findings.append((number, kind))
        for candidate in re.findall(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])", line):
            try:
                address = ipaddress.ip_address(candidate)
            except ValueError:
                continue
            if any(address in net for net in (ipaddress.ip_network("10.0.0.0/8"), ipaddress.ip_network("172.16.0.0/12"), ipaddress.ip_network("192.168.0.0/16"))):
                # The scanner's own network definitions are not host addresses.
                if candidate not in {"10.0.0.0", "172.16.0.0", "192.168.0.0"}:
                    findings.append((number, "private-network-address"))
    return findings


def unapproved_hosts(text: str, allowed: set[str]) -> list[tuple[int, str]]:
    findings = []
    for number, line in enumerate(text.splitlines(), 1):
        for url in re.findall(r"https?://[^\s/]+(?:/[^\s]*)?", line):
            url = re.split(r'''["'<>)\\]''', url)[0]
            try:
                host = urlsplit(url).hostname or ""
            except ValueError:
                continue
            if not re.fullmatch(r"(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}", host):
                continue
            if host in allowed or host.endswith((".invalid", ".test", ".example")):
                continue
            findings.append((number, "unreviewed-url-host"))
    return findings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-reference", action="append", default=[], type=Path,
                        help="Local dotenv file used for exact-match checks; never copied or printed")
    args = parser.parse_args()
    allowed = set(json.loads(subprocess.check_output(
        ['git', 'show', ':config/public_hosts.json'], cwd=ROOT)))
    tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).split(b"\0")
    needles = []
    for reference in args.private_reference:
        for line in reference.read_text().splitlines():
            if "=" not in line or line.lstrip().startswith("#"):
                continue
            key, value = line.split("=", 1)
            value = value.strip().strip('"').strip("'")
            if (len(value) >= 8 and not key.endswith("_ENV") and
                re.search(r"password|secret|token|key|base_url|host", key, re.I)):
                if not any(host in value for host in ("localhost", "127.0.0.1")):
                    needles.append(value)
    findings = []
    count = 0
    for raw in tracked:
        if not raw:
            continue
        relative = raw.decode()
        path = ROOT / relative
        count += 1
        if path.is_symlink():
            if not path.resolve().is_relative_to(ROOT) or not path.exists():
                findings.append((relative, 0, "invalid-symlink"))
            continue
        # Read the staged content, not an unstaged replacement.
        blob = subprocess.check_output(["git", "show", ":" + relative], cwd=ROOT)
        if len(blob) > 2_000_000:
            findings.append((relative, 0, "oversized-file"))
        if ((path.name.startswith(".env") and path.name != ".env.example") or
            path.name in {"local.toml", "mmclaw.json"} or
            any(part in {"workspaces", "memory", ".venv", ".local"} for part in Path(relative).parts) or
            path.suffix in {".db", ".sqlite", ".sqlite3", ".pdf", ".xlsx", ".parquet", ".pem", ".key"}):
            findings.append((relative, 0, "private-artifact"))
        try:
            text = blob.decode("utf-8")
        except UnicodeDecodeError:
            findings.append((relative, 0, "unexpected-binary"))
            continue
        findings.extend((relative, n, kind) for n, kind in inspect_text(text))
        findings.extend((relative, n, kind) for n, kind in unapproved_hosts(text, allowed))
        if any(needle in text for needle in needles):
            findings.append((relative, 0, "matches-private-reference"))
    for path, line, kind in findings:
        print(f"{path}:{line}: {kind}")
    print(f"Scanned {count} staged files; {len(findings)} findings; no secret values printed")
    return int(bool(findings))


if __name__ == "__main__":
    raise SystemExit(main())
