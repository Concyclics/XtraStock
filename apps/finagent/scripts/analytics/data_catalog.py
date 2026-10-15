"""Bounded, read-only model access to the unified dataset registry."""
from __future__ import annotations


def load_data_catalog(conn, cutoff, dataset_keys=None, limit=50, intent="", offset=0):
    """Keep files private and distinguish registration from successful collection.

    Definitions are a current operational catalog, not bitemporal history. An
    updated definition is omitted at earlier cutoffs instead of backdating it.
    No base table is counted or scanned to build this response.
    """
    keys = list(dict.fromkeys(dataset_keys or []))
    offset=int(offset)
    if not 0<=offset<=1000:raise ValueError('catalog_offset must be between 0 and 1000')
    return conn.execute("""
        SELECT r.dataset_key,r.title,r.storage_kind,r.grain,r.usage_contract,
               r.dependencies,r.updated_at AS catalog_available_at,
               CASE WHEN r.storage_kind='sql' THEN r.locator ELSE NULL END AS canonical_relation,
               w.expected_date,w.data_through,w.row_count,w.eligible_count,w.coverage_pct,
               coalesce(w.quality_status,'unknown') AS quality_status,
               w.completed_at AS status_available_at,
               CASE WHEN w.dataset_key IS NULL THEN 'not_measured' ELSE 'recorded' END AS measurement_status
        FROM ops.data_asset_registry r
        LEFT JOIN LATERAL (
            SELECT dataset_key,expected_date,data_through,row_count,eligible_count,
                   coverage_pct,quality_status,completed_at
            FROM ops.dataset_watermarks
            WHERE dataset_key=r.dataset_key AND partition_key='default' AND completed_at<=%(cutoff)s
            ORDER BY completed_at DESC LIMIT 1
        ) w ON true
        WHERE r.updated_at<=%(cutoff)s
          AND (NOT %(has_keys)s OR r.dataset_key=ANY(%(keys)s))
          AND (%(intent)s='' OR r.dataset_key ILIKE %(pattern)s OR r.title ILIKE %(pattern)s
               OR r.usage_contract ILIKE %(pattern)s)
        ORDER BY r.dataset_key LIMIT %(limit)s OFFSET %(offset)s
    """, {"cutoff": cutoff, "keys": keys, "has_keys": bool(keys), "intent": intent,
            "pattern": "%" + intent + "%", "limit": max(1, min(int(limit), 100)), "offset":offset}).fetchall()
