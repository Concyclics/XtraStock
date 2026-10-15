from __future__ import annotations

import datetime as dt
from typing import Any

from psycopg.types.json import Jsonb

from .outbox import _connect


def _items(value: Any) -> set[str]:
    if isinstance(value, str):
        value = [value]
    return {str(item) for item in (value or []) if str(item)}


class AccessController:
    """Database-backed connector policy and quota controller."""

    def __init__(self, channel: str, config: dict[str, Any]):
        self.channel = channel
        self.config = config
        self.sync_config()

    def sync_config(self) -> None:
        user_key = "default_daily_budget_per_member" if self.channel == "qq" else "default_daily_budget_per_user"
        user_budget = int(self.config.get(user_key, 0) or 0)
        group_budget = int(self.config.get("default_daily_budget_per_group", 0) or 0)
        interval = int(self.config.get("default_min_interval_seconds", 0) or 0)
        allowed_users = _items(self.config.get("allowed_users"))
        allowed_groups = _items(self.config.get("allowed_groups"))
        with _connect() as conn:
            for subject_type, budget, whitelist in (
                ("user", user_budget, bool(allowed_users)),
                ("group", group_budget, bool(allowed_groups)),
            ):
                conn.execute(
                    """
                    INSERT INTO agent.access_defaults (
                      channel,subject_type,default_daily_budget,
                      default_min_interval_seconds,whitelist_enabled,metadata,updated_at
                    ) VALUES (%s,%s,%s,%s,%s,%s,now())
                    ON CONFLICT (channel,subject_type) DO UPDATE SET
                      default_daily_budget=EXCLUDED.default_daily_budget,
                      default_min_interval_seconds=EXCLUDED.default_min_interval_seconds,
                      whitelist_enabled=EXCLUDED.whitelist_enabled,
                      metadata=agent.access_defaults.metadata||EXCLUDED.metadata,updated_at=now()
                    """,
                    (self.channel, subject_type, budget, interval, whitelist,
                     Jsonb({"migrated_from": "mmclaw.json"})),
                )
            policy_rows: dict[tuple[str, str], dict[str, Any]] = {}
            for subject_type, allowed, blocked in (
                ("user", allowed_users, _items(self.config.get("blocked_users"))),
                ("group", allowed_groups, _items(self.config.get("blocked_groups"))),
            ):
                for subject_id in allowed:
                    policy_rows[(subject_type, subject_id)] = {"policy_status": "allowed"}
                for subject_id in blocked:
                    policy_rows[(subject_type, subject_id)] = {"policy_status": "blocked"}
            for subject_id in _items(self.config.get("admin_users")):
                policy_rows[("user", subject_id)] = {
                    "policy_status": "allowed", "daily_budget": 0,
                    "min_interval_seconds": 0, "is_admin": True,
                }
            for subject_id, override in (self.config.get("member_limits") or {}).items():
                row = policy_rows.setdefault(("user", str(subject_id)), {"policy_status": "allowed"})
                row.update({
                    "daily_budget": int((override or {}).get("daily_budget", user_budget) or 0),
                    "min_interval_seconds": int((override or {}).get("min_interval_seconds", interval) or 0),
                })
            for (subject_type, subject_id), policy in policy_rows.items():
                conn.execute(
                    """
                    INSERT INTO agent.access_policies (
                      channel,subject_type,subject_id,policy_status,daily_budget,
                      min_interval_seconds,is_admin,metadata,updated_at
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,now())
                    ON CONFLICT (channel,subject_type,subject_id) DO UPDATE SET
                      policy_status=EXCLUDED.policy_status,daily_budget=EXCLUDED.daily_budget,
                      min_interval_seconds=EXCLUDED.min_interval_seconds,is_admin=EXCLUDED.is_admin,
                      metadata=agent.access_policies.metadata||EXCLUDED.metadata,updated_at=now()
                    """,
                    (
                        self.channel, subject_type, subject_id, policy.get("policy_status", "allowed"),
                        int(policy.get("daily_budget", user_budget if subject_type == "user" else group_budget)),
                        int(policy.get("min_interval_seconds", interval)), bool(policy.get("is_admin", False)),
                        Jsonb({"migrated_from": "mmclaw.json"}),
                    ),
                )

    def policy(self, subject_type: str, subject_id: str | None) -> dict[str, Any]:
        with _connect() as conn:
            default = conn.execute(
                "SELECT * FROM agent.access_defaults WHERE channel=%s AND subject_type=%s",
                (self.channel, subject_type),
            ).fetchone()
            override = conn.execute(
                "SELECT * FROM agent.access_policies WHERE channel=%s AND subject_type=%s AND subject_id=%s",
                (self.channel, subject_type, subject_id or "unknown"),
            ).fetchone()
        result = {
            "policy_status": "allowed",
            "daily_budget": int((default or {}).get("default_daily_budget", 0)),
            "min_interval_seconds": int((default or {}).get("default_min_interval_seconds", 0)),
            "whitelist_enabled": bool((default or {}).get("whitelist_enabled", False)),
            "is_admin": False, "has_override": bool(override),
        }
        if override:
            result.update({
                "policy_status": override["policy_status"], "daily_budget": int(override["daily_budget"]),
                "min_interval_seconds": int(override["min_interval_seconds"]),
                "is_admin": bool(override["is_admin"]),
            })
        return result

    def is_admin(self, user_id: str | None) -> bool:
        return bool(self.policy("user", user_id)["is_admin"])

    def allowed(self, user_id: str | None, group_id: str | None = None) -> tuple[bool, str]:
        user = self.policy("user", user_id)
        if user["policy_status"] == "blocked":
            return False, "blocked_user"
        if user["whitelist_enabled"] and not user["has_override"]:
            return False, "user_not_allowed"
        if group_id:
            group = self.policy("group", group_id)
            if group["policy_status"] == "blocked":
                return False, "blocked_group"
            if group["whitelist_enabled"] and not group["has_override"]:
                return False, "group_not_allowed"
        return True, ""

    def consume(self, user_id: str | None, group_id: str | None = None) -> tuple[bool, str]:
        subjects = [("user", user_id or "unknown")]
        if group_id:
            subjects.append(("group", group_id))
        with _connect() as conn:
            policies = {(kind, value): self.policy(kind, value) for kind, value in subjects}
            for subject_type, subject_id in subjects:
                conn.execute(
                    """
                    INSERT INTO agent.usage_daily (usage_date,channel,subject_type,subject_id)
                    VALUES (current_date,%s,%s,%s) ON CONFLICT DO NOTHING
                    """,
                    (self.channel, subject_type, subject_id),
                )
                usage = conn.execute(
                    """
                    SELECT * FROM agent.usage_daily
                    WHERE usage_date=current_date AND channel=%s
                      AND subject_type=%s AND subject_id=%s FOR UPDATE
                    """,
                    (self.channel, subject_type, subject_id),
                ).fetchone()
                policy = policies[(subject_type, subject_id)]
                if not policy["is_admin"] and policy["daily_budget"] > 0 and usage["request_count"] >= policy["daily_budget"]:
                    return False, f"{subject_type}_daily_budget_exceeded"
                if usage["last_request_at"] and policy["min_interval_seconds"] > 0:
                    elapsed = (dt.datetime.now(dt.timezone.utc) - usage["last_request_at"]).total_seconds()
                    if elapsed < policy["min_interval_seconds"]:
                        return False, f"{subject_type}_rate_limited"
            for subject_type, subject_id in subjects:
                conn.execute(
                    """
                    UPDATE agent.usage_daily SET request_count=request_count+1,
                      last_request_at=now(),updated_at=now()
                    WHERE usage_date=current_date AND channel=%s
                      AND subject_type=%s AND subject_id=%s
                    """,
                    (self.channel, subject_type, subject_id),
                )
        return True, ""

    def usage(self, subject_type: str, subject_id: str | None) -> dict[str, Any]:
        policy = self.policy(subject_type, subject_id)
        with _connect() as conn:
            row = conn.execute(
                """
                SELECT request_count,last_request_at FROM agent.usage_daily
                WHERE usage_date=current_date AND channel=%s AND subject_type=%s AND subject_id=%s
                """,
                (self.channel, subject_type, subject_id or "unknown"),
            ).fetchone()
        return {
            **policy, "request_count": int((row or {}).get("request_count", 0)),
            "last_request_at": (row or {}).get("last_request_at"),
        }

    def set_user_limit(self, subject_id: str, daily_budget: int, min_interval_seconds: int) -> None:
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO agent.access_policies (
                  channel,subject_type,subject_id,policy_status,daily_budget,
                  min_interval_seconds,is_admin,metadata,updated_at
                ) VALUES (%s,'user',%s,'allowed',%s,%s,false,'{}',now())
                ON CONFLICT (channel,subject_type,subject_id) DO UPDATE SET
                  daily_budget=EXCLUDED.daily_budget,min_interval_seconds=EXCLUDED.min_interval_seconds,
                  updated_at=now()
                """,
                (self.channel, subject_id, daily_budget, min_interval_seconds),
            )

    def unset_user_limit(self, subject_id: str) -> None:
        with _connect() as conn:
            conn.execute(
                """
                DELETE FROM agent.access_policies
                WHERE channel=%s AND subject_type='user' AND subject_id=%s AND is_admin=false
                """,
                (self.channel, subject_id),
            )
