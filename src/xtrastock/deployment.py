"""Render deployment templates into ignored local files. Never install/start units."""
from __future__ import annotations

import argparse
from pathlib import Path
import re

from xtrastock.config import Settings


def render(source: str, settings: Settings) -> str:
    values = {
        "REPO": settings.root, "APP": settings.path("paths.app"),
        "STATE": settings.path("paths.state"), "WORKSPACES": settings.path("paths.workspaces"),
        "MODELS": settings.path("paths.models"),
        "VLLM_BIN": settings.get("deployment.vllm_bin", "/usr/local/bin/vllm"),
        "OCR_GPU": settings.get("deployment.ocr_gpu", "0"),
        "EMBEDDING_GPU": settings.get("deployment.embedding_gpu", "0"),
    }
    for key, value in values.items():
        # Unit assignments/ExecStart are not shell strings. Reject ambiguous
        # whitespace/control characters instead of producing an unsafe unit.
        if any(c.isspace() for c in str(value)) or any(c in str(value) for c in ('"', "'", "%", "\\")):
            raise ValueError(f"Deployment {key} must not contain whitespace, quotes, percent or backslash")
        source = source.replace(f"@{key}@", str(value))
    if re.search(r"@[A-Z_]+@", source):
        raise ValueError("Unresolved deployment token")
    return source
