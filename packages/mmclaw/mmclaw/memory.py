import os
import json
import glob
import re
import threading
from datetime import datetime

TOTAL_HISTORY_TOKENS = 45_000
MAX_MSG_TOKENS = 16_000
HISTORY_NOTE_TOKENS_UPPER = 100
MAX_MEMORY_ENTRY_CHARS = 500
MAX_TOTAL_MEMORY_CHARS = 10000




def _estimate_tokens(text):
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False)
    chinese = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    return chinese + (len(text) - chinese) // 4


class BaseMemory:
    """Kernel-level abstract base. Defines the session memory interface."""
    def __init__(self, system_prompt):
        self.system_prompt = system_prompt
        self.history = [{"role": "system", "content": system_prompt}]

    def add(self, role, content):
        pass

    def add_message(self, message):
        self.add(message.get("role", "user"), message.get("content", ""))

    def append_trace(self, event):
        pass

    def get_all(self):
        pass

    def reset(self):
        pass

    def update_system_prompt(self, prompt):
        self.system_prompt = prompt
        if self.history and self.history[0]["role"] == "system":
            self.history[0]["content"] = prompt


class GlobalFileMemory(BaseMemory):
    """Adds cross-session global memory backed by a shared file."""
    GLOBAL_MEMORY_FILE = None

    def get_for_finance_turn(self, turn_start):
        """Keep conversation intent, but never carry tool state across turns.

        The on-disk transcript remains intact. In particular, an old clock,
        cutoff, failed gate or evidence ID must not become current evidence.
        Current-turn messages retain the normal native/JSON tool protocol.
        """
        past = []
        for message in self.history[1:turn_start]:
            role = message.get("role")
            content = message.get("content")
            if role not in {"user", "assistant"} or not isinstance(content, str):
                continue
            if message.get("tool_calls") or message.get("mmclaw_internal"):
                continue
            if content.startswith("Tool Output ("):
                continue
            if role == "assistant":
                try:
                    payload, _ = json.JSONDecoder().raw_decode(content.lstrip())
                except (ValueError, TypeError):
                    payload = None
                if isinstance(payload, dict) and ("tools" in payload or "thought" in payload):
                    if payload.get("tools"):
                        continue
                    content = payload.get("content") or ""
                    if not isinstance(content, str):
                        continue
                content = re.sub(r"\[?[0-9a-fA-F-]{32,36}:E\d{4}\]?", "", content)
            if content.strip():
                past.append({"role": role, "content": content[:1800]})

        global_memories = self._load_global_memories()
        context = {
            "notice": "Earlier conversation is context for intent/entity resolution only. "
                      "Re-query all financial facts, time, gate status and citations this turn. "
                      "Do not inherit old tool arguments or decision_cutoff values.",
            "conversation": past[-16:],
            "global_memory": [m.get("memory", "") for m in global_memories],
        }
        system = {"role": "system", "content": self.history[0]["content"]}
        context_message = {"role": "user", "content": "[EARLIER CONVERSATION: NOT CURRENT EVIDENCE]\n"
                           + json.dumps(context, ensure_ascii=False)}
        # Reserve space for this turn first; drop historical context if needed.
        available = TOTAL_HISTORY_TOKENS - _estimate_tokens(system["content"])
        selected, used = [], 0
        for message in reversed(self.history[turn_start:]):
            tokens = _estimate_tokens(message.get("content", ""))
            if used + tokens > available:
                break
            selected.append({k: v for k, v in message.items() if k != "mmclaw_internal"})
            used += tokens
        selected.reverse()
        prefix = [system]
        if used + _estimate_tokens(context_message["content"]) <= available:
            prefix.append(context_message)
        return prefix + self._sanitize_tool_message_sequence(selected)

    def global_memory_add(self, text: str) -> str:
        if len(text) > MAX_MEMORY_ENTRY_CHARS:
            return f"Error: memory too long ({len(text)} chars, max {MAX_MEMORY_ENTRY_CHARS}). Please shorten it."
        memories = self._load_global_memories()
        total = sum(len(m["memory"]) for m in memories) + len(text)
        if total > MAX_TOTAL_MEMORY_CHARS:
            return f"Error: total memory full ({total} chars would exceed {MAX_TOTAL_MEMORY_CHARS}). Ask the user to delete some entries first (use memory_list to show them)."
        os.makedirs(os.path.dirname(self.GLOBAL_MEMORY_FILE), exist_ok=True)
        entry = {"date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "memory": text}
        with open(self.GLOBAL_MEMORY_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return "Memory saved."

    def global_memory_list(self) -> str:
        memories = self._load_global_memories()
        if not memories:
            return "No global memories."
        lines = [f"[{i}] ({m['date']}) {m['memory']}" for i, m in enumerate(memories)]
        return "\n".join(lines)

    def global_memory_delete(self, indices) -> str:
        if isinstance(indices, int):
            indices = [indices]
        memories = self._load_global_memories()
        invalid = [i for i in indices if i < 0 or i >= len(memories)]
        if invalid:
            return f"Error: indices {invalid} out of range (0-{len(memories)-1})."
        for i in sorted(set(indices), reverse=True):
            memories.pop(i)
        with open(self.GLOBAL_MEMORY_FILE, "w", encoding="utf-8") as f:
            for m in memories:
                f.write(json.dumps(m, ensure_ascii=False) + "\n")
        remaining = "\n".join(f"[{i}] ({m['date']}) {m['memory']}" for i, m in enumerate(memories))
        return f"Deleted {len(indices)} memor{'y' if len(indices)==1 else 'ies'}. Remaining:\n{remaining or '(none)'}"

    def _load_global_memories(self):
        if not self.GLOBAL_MEMORY_FILE:
            return []
        if not os.path.exists(self.GLOBAL_MEMORY_FILE):
            return []
        memories = []
        try:
            with open(self.GLOBAL_MEMORY_FILE, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            memories.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
        except Exception:
            pass
        return memories

    def _get_truncation_note(self):
        return "\n... [truncated due to length limit]"

    def _get_history_note(self, dropped):
        return ""

    def _sanitize_tool_message_sequence(self, messages):
        sanitized = []
        i = 0
        while i < len(messages):
            msg = messages[i]
            role = msg.get("role")
            if role == "tool":
                i += 1
                continue

            tool_calls = msg.get("tool_calls") or []
            if role == "assistant" and tool_calls:
                required = [call.get("id") or call.get("name", "") for call in tool_calls]
                required = [call_id for call_id in required if call_id]
                block_tools = []
                seen = set()
                j = i + 1
                while j < len(messages) and messages[j].get("role") == "tool":
                    tool_msg = messages[j]
                    tool_call_id = tool_msg.get("tool_call_id") or tool_msg.get("id") or tool_msg.get("name", "")
                    if tool_call_id in required and tool_call_id not in seen:
                        block_tools.append(tool_msg)
                        seen.add(tool_call_id)
                    j += 1
                if required and set(required).issubset(seen):
                    sanitized.append(msg)
                    sanitized.extend(block_tools)
                i = j
                continue

            sanitized.append(msg)
            i += 1
        return sanitized

    def get_all(self):
        messages = self.history[1:]

        global_memories = self._load_global_memories()
        if global_memories:
            mem_lines = "\n".join(f"[{m['date']}] {m['memory']}" for m in global_memories)
            global_note = f"\n\n## Global Memory\n{mem_lines}"
        else:
            global_note = ""

        system_tokens = _estimate_tokens(self.history[0]["content"] + global_note) + HISTORY_NOTE_TOKENS_UPPER
        available = TOTAL_HISTORY_TOKENS - system_tokens
        selected = []
        used = 0
        for msg in reversed(messages):
            content = msg.get("content", "")
            if isinstance(content, str) and _estimate_tokens(content) > MAX_MSG_TOKENS:
                content = content[:MAX_MSG_TOKENS * 3] + self._get_truncation_note()
            tokens = _estimate_tokens(content)
            if used + tokens > available:
                break
            selected.append({**msg, "content": content})
            used += tokens
        selected.reverse()

        dropped = len(messages) - len(selected)
        history_note = self._get_history_note(dropped)
        system = {"role": "system", "content": self.history[0]["content"] + global_note + history_note}
        return [system] + self._sanitize_tool_message_sequence(selected)


class StatelessMemory(GlobalFileMemory):
    """In-memory session only. No session files, no disk I/O. Used for stateless arg mode (-p).
    Global memory is disabled by default; pass use_global_memory=True to enable it."""
    def __init__(self, system_prompt, use_global_memory=False):
        super().__init__(system_prompt)
        self._use_global_memory = use_global_memory
        if use_global_memory:
            print(f"[*] Global memory: {self.GLOBAL_MEMORY_FILE}")
        else:
            print("[*] Global memory: disabled (use --global-memory to enable)")

    def _get_truncation_note(self):
        return "\n... [truncated due to length limit]"

    def _load_global_memories(self):
        if not self._use_global_memory:
            return []
        return super()._load_global_memories()

    def global_memory_add(self, text):
        if not self._use_global_memory:
            return "Global memory is disabled in stateless mode. Use --global-memory to enable it."
        return super().global_memory_add(text)

    def global_memory_list(self):
        if not self._use_global_memory:
            return "Global memory is disabled in stateless mode. Use --global-memory to enable it."
        return super().global_memory_list()

    def global_memory_delete(self, indices):
        if not self._use_global_memory:
            return "Global memory is disabled in stateless mode. Use --global-memory to enable it."
        return super().global_memory_delete(indices)

    def add(self, role, content):
        self.history.append({"role": role, "content": content})

    def add_message(self, message):
        self.history.append(message)

    def save_file(self, filename: str, data: bytes) -> str:
        import tempfile
        path = os.path.join(tempfile.gettempdir(), filename)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def reset(self):
        self.history = [{"role": "system", "content": self.system_prompt}]


class FileMemory(GlobalFileMemory):
    SESSIONS_DIR = None

    def __init__(self, system_prompt, sessions_dir=None):
        self._trace_lock = threading.Lock()
        self.sessions_dir = str(sessions_dir or self.SESSIONS_DIR)
        os.makedirs(self.sessions_dir, exist_ok=True)
        latest_dir = self._find_latest_dir()
        if latest_dir:
            try:
                self._load(latest_dir, system_prompt)
                print(f"[*] Resumed session: {os.path.basename(latest_dir)}")
            except Exception as e:
                print(f"[!] Failed to load session {os.path.basename(latest_dir)}: {e}")
                self._start_new(system_prompt)
                print(f"[*] Using new session: {os.path.basename(self.session_dir)}")
        else:
            self._start_new(system_prompt)
            print(f"[*] New session: {os.path.basename(self.session_dir)}")
        print(f"[*] Global memory: {self.GLOBAL_MEMORY_FILE}")

    def _start_new(self, system_prompt):
        super().__init__(system_prompt)
        self.session_dir = self._new_dir()
        os.makedirs(self.session_dir, exist_ok=True)
        os.makedirs(os.path.join(self.session_dir, "files"), exist_ok=True)
        self.session_file = os.path.join(self.session_dir, "messages.jsonl")
        self.summary_file = os.path.join(self.session_dir, "summary.md")
        self._append({"role": "system", "content": system_prompt})

    def _new_dir(self):
        now = datetime.now()
        ts = now.strftime("%Y-%m-%d_%H-%M-%S")
        ms = now.microsecond // 1000
        return os.path.join(self.sessions_dir, f"session_{ts}-{ms:03d}")

    def _find_latest_dir(self):
        dirs = [d for d in glob.glob(os.path.join(self.sessions_dir, "session_*")) if os.path.isdir(d)]
        return max(dirs, key=os.path.basename) if dirs else None

    def _load(self, session_dir, system_prompt):
        self.session_dir = session_dir
        self.session_file = os.path.join(session_dir, "messages.jsonl")
        self.summary_file = os.path.join(session_dir, "summary.md")
        self.system_prompt = system_prompt
        self.history = []
        with open(self.session_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    self.history.append(json.loads(line))
        if self.history and self.history[0]["role"] == "system":
            self.history[0]["content"] = system_prompt

    def _append(self, entry):
        with open(self.session_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def add(self, role, content):
        entry = {"role": role, "content": content}
        self.history.append(entry)
        self._append(entry)

    def add_message(self, message):
        self.history.append(message)
        self._append(message)

    @property
    def trace_file(self):
        return os.path.join(self.session_dir, "trace.jsonl")

    def append_trace(self, event):
        payload = dict(event or {})
        payload.setdefault("timestamp", datetime.now().astimezone().isoformat())
        with self._trace_lock:
            with open(self.trace_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")

    @property
    def files_dir(self):
        return os.path.join(self.session_dir, "files")

    def _get_truncation_note(self):
        return "\n... [truncated, full content in session file]"

    def _compact_text(self, content, limit=700):
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        content = " ".join(content.split())
        if len(content) > limit:
            content = content[:limit] + "..."
        return content

    def _compact_tool_evidence(self, message):
        content = str(message.get("content") or "")
        name = str(message.get("name") or "tool")
        start = content.find("{")
        payload = None
        if start >= 0:
            try:
                payload = json.loads(content[start:])
            except Exception:
                payload = None
        if not isinstance(payload, dict):
            return f"{name}: {self._compact_text(content, 320)}"

        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
        parts = [name]
        for key in ("search_backend", "row_count", "days", "as_of_date", "latest_date", "read_only"):
            if key in meta and meta[key] not in (None, "", []):
                parts.append(f"{key}={meta[key]}")
        rows = payload.get("rows") if isinstance(payload.get("rows"), list) else []
        evidence = []
        for row in rows[:5]:
            if not isinstance(row, dict):
                continue
            fields = []
            for key in (
                "document_id", "id", "stock_code", "stock_name", "title", "source_name",
                "source_type", "published_date", "report_date", "event_date", "base_trade_date",
                "latest_trade_date", "price_source",
            ):
                value = row.get(key)
                if value not in (None, "", []):
                    fields.append(f"{key}={self._compact_text(value, 100)}")
            if fields:
                evidence.append("{" + ", ".join(fields) + "}")
        if evidence:
            parts.append("evidence=" + "; ".join(evidence))
        return " | ".join(parts)[:1400]

    def _load_summary(self):
        if not getattr(self, "summary_file", None) or not os.path.exists(self.summary_file):
            return ""
        try:
            with open(self.summary_file, encoding="utf-8") as handle:
                return handle.read().strip()
        except Exception:
            return ""

    def _refresh_summary(self, dropped_messages):
        if not dropped_messages or not getattr(self, "summary_file", None):
            return ""
        try:
            os.makedirs(os.path.dirname(self.summary_file), exist_ok=True)
            conversational = [
                msg for msg in dropped_messages
                if msg.get("role") in {"user", "assistant"} and not msg.get("tool_calls")
            ][-24:]
            tool_evidence = [
                msg for msg in dropped_messages if msg.get("role") == "tool"
            ][-20:]
            lines = [
                "# Rolling Conversation Summary",
                "",
                f"Compacted at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                f"Messages summarized: {len(dropped_messages)}",
                "",
            ]
            for msg in conversational:
                role = msg.get("role", "user")
                content = self._compact_text(msg.get("content", ""), 500 if role == "user" else 360)
                if content:
                    lines.append(f"- {role}: {content}")
            if tool_evidence:
                lines.extend(["", "## Retained Tool Evidence"])
                for msg in tool_evidence:
                    lines.append(f"- {self._compact_tool_evidence(msg)}")
            summary = "\n".join(lines).strip()
            with open(self.summary_file, "w", encoding="utf-8") as f:
                f.write(summary + "\n")
            return summary
        except Exception:
            return self._load_summary()

    def _get_history_note(self, dropped):
        summary = self._load_summary()
        summary_note = f"\n\nRolling summary of earlier conversation:\n{summary}" if summary else ""
        if dropped > 0:
            return (
                f"\n\n{dropped} earlier messages were compacted. "
                "Use the rolling summary and retained evidence below. Do not claim a historical fact "
                "that is absent from the summary; retrieve the underlying finance evidence again when needed."
            ) + summary_note
        return "\n\nFull active-session history is in context."

    def get_all(self):
        messages = self.history[1:]

        global_memories = self._load_global_memories()
        if global_memories:
            mem_lines = "\n".join(f"[{m['date']}] {m['memory']}" for m in global_memories)
            global_note = f"\n\n## Global Memory\n{mem_lines}"
        else:
            global_note = ""

        system_tokens = _estimate_tokens(self.history[0]["content"] + global_note) + HISTORY_NOTE_TOKENS_UPPER
        available = TOTAL_HISTORY_TOKENS - system_tokens
        selected = []
        used = 0
        for msg in reversed(messages):
            content = msg.get("content", "")
            if isinstance(content, str) and _estimate_tokens(content) > MAX_MSG_TOKENS:
                content = content[:MAX_MSG_TOKENS * 3] + self._get_truncation_note()
            tokens = _estimate_tokens(content)
            if used + tokens > available:
                break
            selected.append({**msg, "content": content})
            used += tokens
        selected.reverse()

        dropped = len(messages) - len(selected)
        if dropped > 0:
            self._refresh_summary(messages[:dropped])
        history_note = self._get_history_note(dropped)
        system = {"role": "system", "content": self.history[0]["content"] + global_note + history_note}
        return [system] + self._sanitize_tool_message_sequence(selected)

    def save_file(self, filename: str, data: bytes) -> str:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(self.files_dir, f"{ts}_{filename}")
        with open(path, "wb") as f:
            f.write(data)
        return path

    def reset(self):
        self._start_new(self.system_prompt)
        print(f"[*] New session: {os.path.basename(self.session_dir)}")
