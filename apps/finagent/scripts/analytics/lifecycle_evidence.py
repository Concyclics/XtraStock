"""Resolve lifecycle versions after applying validity and knowledge cutoffs.

An inferred refresh cannot replace reviewed evidence. Conflicts share the
reviewed tier so a later conflict blocks an older certification. Within a tier,
the latest known revision wins. This does not certify unreviewed ST fields.
"""
from __future__ import annotations

import re


def lifecycle_order(alias: str = "") -> str:
    if alias and not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", alias):
        raise ValueError("invalid lifecycle SQL alias")
    prefix = alias + "." if alias else ""
    return (
        f"CASE WHEN {prefix}quality_status IN ('verified','conflict') THEN 1 ELSE 0 END DESC,"
        f"{prefix}available_at DESC,{prefix}valid_from DESC,{prefix}lifecycle_version_id DESC"
    )
