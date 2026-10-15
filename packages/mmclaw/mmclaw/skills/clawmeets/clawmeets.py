#!/usr/bin/env python3
"""ClawMeets CLI — used by MMClaw agent to perform message operations."""

from xtrastock.config import bootstrap_environment
bootstrap_environment()

import argparse
import base64
import json
import mimetypes
import sys
import urllib.request
import urllib.error
import tempfile
from pathlib import Path

SERVER      = "https://testapi.clawmeets.com"
import os
_WORKSPACE = Path(os.environ.get("MMCLAW_WORKSPACE", str(Path.home() / ".mmclaw")))
CONFIG_FILE = _WORKSPACE / "skill-config" / "clawmeets.json"
TMP_DIR     = Path(tempfile.gettempdir()) / "mmclaw-clawmeets"


def load_config():
    if not CONFIG_FILE.exists():
        print("NOT_CONFIGURED")
        sys.exit(1)
    try:
        cfg = json.loads(CONFIG_FILE.read_text())
    except Exception:
        print("INVALID_CONFIG")
        sys.exit(1)
    address = cfg.get("address", "").strip()
    token   = cfg.get("token", "").strip()
    if not address or not token:
        print("INCOMPLETE")
        sys.exit(1)
    return address, token


def load_contacts() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text()).get("contacts", {})
    except Exception:
        return {}


def save_config(address: str, token: str):
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    # preserve existing contacts if any
    contacts = {}
    if CONFIG_FILE.exists():
        try:
            contacts = json.loads(CONFIG_FILE.read_text()).get("contacts", {})
        except Exception:
            pass
    CONFIG_FILE.write_text(json.dumps(
        {"address": address, "token": token, "contacts": contacts}, indent=2
    ))


def save_contact(nickname: str, address: str):
    cfg = {}
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text())
        except Exception:
            pass
    cfg.setdefault("contacts", {})[nickname] = address
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


def resolve_address(target: str) -> str:
    """Resolve a nickname to an address, or return target as-is if not found."""
    return load_contacts().get(target, target)


def make_headers(address=None, token=None):
    h = {"Content-Type": "application/json", "User-Agent": "MMClaw"}
    if address:
        h["X-Address"] = address
        h["X-Token"]   = token
    return h


def api(method, path, address=None, token=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{SERVER}{path}",
        data=data,
        headers=make_headers(address, token),
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            return json.loads(res.read())
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode()}")
        sys.exit(1)


TMP_DIR = Path("/tmp/clawmeets")


def _save_files(message_id: str, files: list) -> list:
    """Decode base64 files to /tmp/clawmeets/<message_id>/, return list with 'path' replacing 'data'."""
    msg_dir = TMP_DIR / message_id
    msg_dir.mkdir(parents=True, exist_ok=True)
    result = []
    for f in files:
        dest = msg_dir / f["filename"]
        dest.write_bytes(base64.b64decode(f["data"]))
        entry = {k: v for k, v in f.items() if k != "data"}
        entry["path"] = str(dest)
        result.append(entry)
    return result


def _encode_file(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        print(f"File not found: {path}")
        sys.exit(1)
    content_type, _ = mimetypes.guess_type(p.name)
    return {
        "filename":     p.name,
        "content_type": content_type or "application/octet-stream",
        "data":         base64.b64encode(p.read_bytes()).decode(),
    }


def cmd_whoami(args):
    address, _ = load_config()
    print(json.dumps({"address": address}, ensure_ascii=False, indent=2))


def cmd_list(args):
    address, token = load_config()
    qs = f"limit={args.limit}" + ("&unread=1" if args.unread else "")
    result = api("GET", f"/messages?{qs}", address, token)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_read(args):
    address, token = load_config()
    result = api("GET", f"/messages/{args.id}", address, token)
    if result.get("files"):
        result["files"] = _save_files(result["id"], result["files"])
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_send(args):
    address, token = load_config()
    body = sys.stdin.read()
    to   = resolve_address(args.to)
    payload = {
        "to":      to,
        "subject": args.subject,
        "body":    body,
    }
    if args.file:
        payload["files"] = [_encode_file(f) for f in args.file]
    result = api("POST", "/messages/send", address, token, payload)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_delete(args):
    address, token = load_config()
    result = api("DELETE", f"/messages/{args.id}", address, token)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_mark_read(args):
    address, token = load_config()
    result = api("PATCH", f"/messages/{args.id}/read", address, token,
                 {"read": not args.unread})
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_signup(args):
    result = api("POST", "/signup")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("ok"):
        save_config(result["address"], result["token"])


def cmd_add_contact(args):
    save_contact(args.nickname, args.address)
    print(json.dumps({"ok": True, "nickname": args.nickname, "address": args.address}, indent=2))


def cmd_list_contacts(args):
    print(json.dumps(load_contacts(), ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(prog="clawmeets")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("signup", help="Create a new account (no args needed)")

    sub.add_parser("whoami", help="Show your address")

    p = sub.add_parser("add-contact", help="Save a nickname for an address")
    p.add_argument("nickname")
    p.add_argument("address")

    sub.add_parser("list-contacts", help="Show all saved contacts")

    p = sub.add_parser("list", help="List inbox")
    p.add_argument("--unread", action="store_true")
    p.add_argument("--limit", type=int, default=20)

    p = sub.add_parser("read", help="Read a single message")
    p.add_argument("id")

    p = sub.add_parser("send", help="Send a message (body from stdin)")
    p.add_argument("to", help="Nickname or raw address")
    p.add_argument("subject")
    p.add_argument("--file", metavar="PATH", action="append", help="Attach a file (repeatable)")

    p = sub.add_parser("delete", help="Delete a message")
    p.add_argument("id")

    p = sub.add_parser("mark-read", help="Mark message as read/unread")
    p.add_argument("id")
    p.add_argument("--unread", action="store_true", help="Mark as unread instead")

    args = parser.parse_args()
    {
        "signup":        cmd_signup,
        "whoami":        cmd_whoami,
        "add-contact":   cmd_add_contact,
        "list-contacts": cmd_list_contacts,
        "list":          cmd_list,
        "read":          cmd_read,
        "send":          cmd_send,
        "delete":        cmd_delete,
        "mark-read":     cmd_mark_read,
    }[args.command](args)


if __name__ == "__main__":
    main()
