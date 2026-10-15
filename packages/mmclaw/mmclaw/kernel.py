import threading
import traceback
import queue
import hashlib
import json
import re
import uuid

JSON_PARSE_RETRIES = 1
import time
import subprocess
import random
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from .providers import Engine
from .tools import ShellTool, AsyncShellTool, FileTool, TimerTool, SessionTool, UpgradeTool, BrowserTool, FinanceDBTool, SafeCalculatorTool
from .tool_schemas import get_native_tool_schemas
from .tool_policy import is_tool_enabled
from .tool_control import ToolLoopController
from .agentic_loop import ResearchLoop
from .evidence import EvidenceStore, persist_model_call, with_quality_evidence
from .presentation import render_finance_text
from .config import _find_file_icase
from .memory import FileMemory, StatelessMemory
from .watcher import WatcherManager


class StopRequested(Exception):
    """Raised inside the chat worker when the user sends /stop."""
    pass


class HeartbeatManager:
    HEARTBEAT_DIR = None
    CONFIG_FILE   = None
    LOG_FILE      = None
    SKILLS_DIR    = None

    def __init__(self, task_queue: queue.Queue, connector):
        self.task_queue = task_queue
        self.connector  = connector
        self._running   = set()  # skill names with active threads

    def start(self):
        self.HEARTBEAT_DIR.mkdir(parents=True, exist_ok=True)

        cfg = self._load_config()
        for skill_name, opts in cfg.items():
            self._start_skill(skill_name, opts)

        # Queue discovery messages for skills not yet in config
        self._queue_discoveries(cfg)

        # Watch config for new entries written by the AI
        threading.Thread(target=self._watch_config, daemon=True).start()

    def _load_config(self) -> dict:
        if not self.CONFIG_FILE.exists():
            return {}
        try:
            return json.loads(self.CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[!] HeartbeatManager: failed to load config: {e}")
            return {}

    def _start_skill(self, skill_name: str, opts: dict):
        if skill_name in self._running:
            return
        if not opts.get("enabled", True):
            return
        interval_seconds = max(10, int(opts.get("interval_seconds", 1800)))
        heartbeat_file = _find_file_icase(self.SKILLS_DIR / skill_name, "heartbeat.md")
        if not heartbeat_file:
            print(f"[!] HeartbeatManager: no heartbeat.md for '{skill_name}', skipping.")
            return
        is_new = self._last_run(skill_name) is None
        threading.Thread(
            target=self._run,
            args=(skill_name, heartbeat_file, interval_seconds, is_new),
            daemon=True,
        ).start()
        self._running.add(skill_name)
        print(f"[*] HeartbeatManager: '{skill_name}' every {interval_seconds}s")

    def _queue_discoveries(self, existing_cfg: dict):
        if not self.SKILLS_DIR.exists():
            return
        for skill_dir in sorted(self.SKILLS_DIR.iterdir()):
            if not skill_dir.is_dir():
                continue
            skill_name = skill_dir.name
            if skill_name in existing_cfg:
                continue
            heartbeat_file = _find_file_icase(skill_dir, "heartbeat.md")
            if not heartbeat_file:
                continue
            try:
                content = heartbeat_file.read_text(encoding="utf-8")
                msg = (
                    f"[HEARTBEAT_DISCOVER: {skill_name}]\n"
                    f"{content}\n\n"
                    f"The above is the heartbeat.md for skill '{skill_name}'. "
                    f"If the above heartbeat.md explicitly states an interval (e.g. 'every 30s', 'every 1 minute'), "
                    f"use that exact value converted to seconds. "
                    f"Only choose your own value if no interval is specified. Minimum 10 seconds. "
                    f"Then read {self.CONFIG_FILE}, add an entry for '{skill_name}' "
                    f"with {{\"enabled\": true, \"interval_seconds\": <value>}}, "
                    f"and write the updated config back. "
                    f"Do NOT send any message to the user. Stay completely silent."
                )
                self.task_queue.put(msg)
                print(f"[*] HeartbeatManager: queued discovery for '{skill_name}'")
            except Exception as e:
                print(f"[!] HeartbeatManager: discovery failed for '{skill_name}': {e}")

    def _watch_config(self):
        """Poll config file for new entries and start threads for them."""
        last_mtime = 0
        while True:
            time.sleep(5)
            try:
                if not self.CONFIG_FILE.exists():
                    continue
                mtime = self.CONFIG_FILE.stat().st_mtime
                if mtime <= last_mtime:
                    continue
                last_mtime = mtime
                cfg = self._load_config()
                for skill_name, opts in cfg.items():
                    if skill_name not in self._running:
                        self._start_skill(skill_name, opts)
            except Exception as e:
                print(f"[!] HeartbeatManager: watcher error: {e}")

    def _last_run(self, skill_name: str):
        if not self.LOG_FILE.exists():
            return None
        last = None
        try:
            for line in self.LOG_FILE.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                if entry.get("skill") == skill_name:
                    last = entry.get("fired_at")
        except Exception:
            pass
        if last:
            try:
                return datetime.fromisoformat(last)
            except Exception:
                pass
        return None

    def _log(self, skill_name: str):
        entry = {
            "skill":    skill_name,
            "fired_at": datetime.now(timezone.utc).isoformat(),
        }
        with open(self.LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    def _run(self, skill_name: str, heartbeat_file: Path, interval_secs: int, is_new: bool):
        if is_new:
            wait = 0
        else:
            last = self._last_run(skill_name)
            if last:
                elapsed = (datetime.now(timezone.utc) - last).total_seconds()
                wait = max(0, interval_secs - elapsed)
                if wait > 0:
                    print(f"[*] HeartbeatManager: '{skill_name}' resumes in {int(wait)}s")
            else:
                wait = interval_secs

        time.sleep(wait)

        while True:
            try:
                content = heartbeat_file.read_text(encoding="utf-8")
                self.task_queue.put(f"[HEARTBEAT: {skill_name}]\n{content}")
                self._log(skill_name)
                print(f"[*] HeartbeatManager: queued heartbeat for '{skill_name}'")
            except Exception as e:
                print(f"[!] HeartbeatManager: error for '{skill_name}': {e}")
            time.sleep(interval_secs)


class CronManager:
    CRON_DIR  = None
    JOBS_FILE = None

    def __init__(self, cron_queue: queue.Queue):
        self.cron_queue = cron_queue
        self._scheduler = None

    def start(self):
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
        except ImportError:
            print("[!] CronManager: apscheduler not installed. Run: pip install apscheduler")
            return
        self.CRON_DIR.mkdir(parents=True, exist_ok=True)
        self._scheduler = BackgroundScheduler()
        self._scheduler.start()
        jobs = self._load_jobs()
        for job in jobs:
            self._schedule_job(job)
        print(f"[*] CronManager: started with {len(jobs)} job(s)")

    def _load_jobs(self) -> list:
        if not self.JOBS_FILE.exists():
            return []
        jobs = []
        try:
            for line in self.JOBS_FILE.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    jobs.append(json.loads(line))
        except Exception as e:
            print(f"[!] CronManager: failed to load jobs: {e}")
        return jobs

    def _save_jobs(self, jobs: list):
        self.CRON_DIR.mkdir(parents=True, exist_ok=True)
        with open(self.JOBS_FILE, "w", encoding="utf-8") as f:
            for job in jobs:
                f.write(json.dumps(job, ensure_ascii=False) + "\n")

    def _make_trigger(self, cron: str):
        from apscheduler.triggers.cron import CronTrigger
        fields = cron.strip().split()
        if len(fields) == 5:
            return CronTrigger.from_crontab(cron)
        elif len(fields) == 6:
            s, mi, h, d, mo, dow = fields
            return CronTrigger(second=s, minute=mi, hour=h, day=d, month=mo, day_of_week=dow)
        else:
            raise ValueError(f"Expected 5 or 6 fields, got {len(fields)}")

    def _schedule_job(self, job: dict):
        if self._scheduler is None:
            return
        name = job["name"]
        try:
            trigger = self._make_trigger(job["cron"])
            self._scheduler.add_job(
                self._fire,
                trigger=trigger,
                args=[name, job["prompt"]],
                id=name,
                replace_existing=True,
            )
        except Exception as e:
            print(f"[!] CronManager: failed to schedule '{name}': {e}")

    def _fire(self, name: str, prompt: str):
        self.cron_queue.put(f"[CRON: {name}]\n{prompt}")
        print(f"[*] CronManager: fired '{name}'")

    def create(self, name: str, cron: str, prompt: str) -> str:
        if self._scheduler is None:
            return "Error: CronManager not started (apscheduler missing)."
        try:
            self._make_trigger(cron)
        except Exception as e:
            return f"Error: invalid cron expression '{cron}': {e}"
        jobs = self._load_jobs()
        if any(j["name"] == name for j in jobs):
            return f"Error: job '{name}' already exists. Delete it first."
        job = {
            "name": name,
            "cron": cron,
            "prompt": prompt,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        jobs.append(job)
        self._save_jobs(jobs)
        self._schedule_job(job)
        return f"Cron job '{name}' created ({cron})."

    def delete(self, indices) -> str:
        if isinstance(indices, int):
            indices = [indices]
        else:
            indices = [int(i) for i in indices]
        jobs = self._load_jobs()
        invalid = [i for i in indices if i < 0 or i >= len(jobs)]
        if invalid:
            return f"Error: indices {invalid} out of range (0-{len(jobs)-1})."
        to_remove = [jobs[i]["name"] for i in indices]
        new_jobs = [j for i, j in enumerate(jobs) if i not in set(indices)]
        self._save_jobs(new_jobs)
        if self._scheduler:
            for name in to_remove:
                try:
                    self._scheduler.remove_job(name)
                except Exception:
                    pass
        remaining = "\n".join(f"[{i}] {j['name']} | {j['cron']} | {j['prompt']}" for i, j in enumerate(new_jobs))
        return f"Deleted {len(indices)} job(s). Remaining:\n{remaining or '(none)'}"

    def list_jobs(self) -> str:
        jobs = self._load_jobs()
        if not jobs:
            return "No cron jobs."
        lines = [f"[{i}] ({j['name']}) {j['cron']} — {j['prompt']}" for i, j in enumerate(jobs)]
        return "\n".join(lines)


class MMClaw(object):
    def __init__(self, config, connector, system_prompt, use_stateless_arg_connector=False, stateless_use_global_memory=False):
        self.config = config
        self.system_prompt = system_prompt
        if "tool_calling_mode" not in self.config:
            self.config["tool_calling_mode"] = "native"
        self.engine = Engine(config)
        if self.config.get("tool_calling_mode") == "native" and not self.engine.supports_native_tools:
            print(f"[!] Native tool calling is not implemented for {self.config.get('engine_type')}; falling back to JSON tool protocol.")
            self.config["tool_calling_mode"] = "json"
        elif self.config.get("tool_calling_mode") == "native":
            print(f"[*] Tool calling mode: native ({self.config.get('engine_type')})")
        else:
            print("[*] Tool calling mode: JSON protocol")
        self.connector = connector
        self.memory = StatelessMemory(system_prompt, use_global_memory=stateless_use_global_memory) if use_stateless_arg_connector else FileMemory(system_prompt)
        self.default_memory = self.memory
        self._session_memories = {}
        self._session_lock = threading.Lock()
        self.connector.file_saver = self.memory.save_file
        self.chat_queue = queue.Queue()
        self.heartbeat_queue = queue.Queue()
        self.cron_queue = queue.Queue()
        self.debug = config.get("debug", False)
        self.use_stateless_arg_connector = use_stateless_arg_connector

        threading.Thread(target=self._worker, args=(self.chat_queue, "chat"), daemon=True).start()
        threading.Thread(target=self._worker, args=(self.heartbeat_queue, "heartbeat"), daemon=True).start()
        threading.Thread(target=self._worker, args=(self.cron_queue, "cron"), daemon=True).start()

        if not use_stateless_arg_connector:
            self.heartbeat = HeartbeatManager(self.heartbeat_queue, self.connector)
            self.heartbeat.start()

            self.cron = CronManager(self.cron_queue)
            self.cron.start()

            self.watcher = WatcherManager(self.chat_queue)
            self.watcher.start()

        # /stop support
        self._stop_event  = threading.Event()
        self._current_proc = None
        self._proc_lock    = threading.Lock()

    def _safe_session_key(self, key):
        key = str(key or "default")
        return re.sub(r"[^A-Za-z0-9_.:-]+", "_", key)[:180]

    def _memory_for_session(self, session_key):
        if self.use_stateless_arg_connector or not session_key:
            return self.default_memory
        safe_key = self._safe_session_key(session_key)
        with self._session_lock:
            if safe_key not in self._session_memories:
                sessions_dir = Path(FileMemory.SESSIONS_DIR) / "scoped" / safe_key
                self._session_memories[safe_key] = FileMemory(self.system_prompt, sessions_dir=str(sessions_dir))
            return self._session_memories[safe_key]

    def _unpack_queue_item(self, item):
        if isinstance(item, dict) and "__mmclaw_text" in item:
            return item.get("__mmclaw_text", ""), {
                "session_key": item.get("__mmclaw_session_key"),
                "reply_context": item.get("__mmclaw_reply_context"),
            }
        return item, {}

    def _activate_task_context(self, meta):
        if not isinstance(meta, dict):
            return
        reply_context = meta.get("reply_context")
        if reply_context and hasattr(self.connector, "activate_context"):
            self.connector.activate_context(reply_context)
        session_key = meta.get("session_key")
        if session_key:
            self.memory = self._memory_for_session(session_key)
            self.connector.file_saver = self.memory.save_file

    # ------------------------------------------------------------------
    # /stop support
    # ------------------------------------------------------------------

    def stop(self):
        """Cancel the current chat job immediately."""
        self._stop_event.set()
        with self._proc_lock:
            if self._current_proc is not None:
                try:
                    self._current_proc.kill()
                except Exception:
                    pass
        self.connector.stop_typing()
        self.connector.send("✋ Job cancelled.")

    def _check_stop(self):
        if self._stop_event.is_set():
            raise StopRequested()

    @staticmethod
    def _provider_error_message(error, trace_id):
        if isinstance(error, urllib.error.HTTPError):
            if error.code in (401, 403):
                reason = "模型服务认证失败"
            elif error.code == 429:
                reason = "模型服务限流，自动重试后仍未恢复"
            elif error.code == 400:
                reason = "模型请求格式或上下文长度不被服务接受"
            elif error.code >= 500:
                reason = "模型服务暂时不可用"
            else:
                reason = f"模型服务返回 HTTP {error.code}"
        elif isinstance(error, subprocess.TimeoutExpired):
            reason = "数据查询工具超时"
        elif isinstance(error, (TimeoutError, urllib.error.URLError)):
            reason = "模型服务网络连接失败，自动重试后仍未恢复"
        else:
            reason = "模型服务调用失败"
        return f"本次请求未完成：{reason}。请稍后重试。"

    def _ask_with_stop(self, messages, tools=None):
        """Run engine.ask() in a daemon thread, interruptible by the stop event."""
        result_box = [None]
        error_box  = [None]
        done       = threading.Event()

        def _ask():
            try:
                result_box[0] = self.engine.ask(messages, tools=tools)
            except Exception as e:
                error_box[0] = e
            finally:
                done.set()

        threading.Thread(target=_ask, daemon=True).start()
        while not done.wait(timeout=0.2):
            self._check_stop()
        self._check_stop()  # one final check after completion
        if error_box[0]:
            raise error_box[0]
        return result_box[0]

    def _shell_execute_with_stop(self, command):
        """Execute a shell command, killable via the stop event."""
        import locale
        command_text = str(command or "")
        plot_shell_patterns = (
            r"\bmatplotlib\b",
            r"\bseaborn\b",
            r"\bpyplot\b",
            r"\bplt\.",
            r"\bsns\.",
            r"\bsavefig\s*\(",
        )
        if any(re.search(pattern, command_text, re.I) for pattern in plot_shell_patterns):
            return (
                "Error executing command: chart rendering with shell_execute is disabled. "
                "Use finance_plot_chart(code, filename, width, height, dpi) instead; it injects the configured "
                "Chinese font and returns a PNG path for file_upload(path). If finance_plot_chart fails, simplify "
                "the plotting code and retry with that tool."
            )
        try:
            proc = subprocess.Popen(
                command_text, shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except Exception as e:
            return f"Error executing command: {str(e)}"

        with self._proc_lock:
            self._current_proc = proc

        deadline = time.time() + ShellTool.TIMEOUT
        try:
            while proc.poll() is None:
                if self._stop_event.is_set():
                    proc.kill()
                    proc.wait()
                    raise StopRequested()
                if time.time() > deadline:
                    proc.kill()
                    proc.wait()
                    return f"Error executing command: timed out after {ShellTool.TIMEOUT}s"
                time.sleep(0.1)
        finally:
            with self._proc_lock:
                if self._current_proc is proc:
                    self._current_proc = None

        stdout = proc.stdout.read()
        stderr = proc.stderr.read()
        output = stdout if proc.returncode == 0 else stderr
        try:
            output = output.decode('utf-8')
        except UnicodeDecodeError:
            output = output.decode(locale.getpreferredencoding(False), errors='replace')
        return f"Return Code {proc.returncode}:\n{output}"

    def _wait_with_stop(self, seconds):
        """Sleep for N seconds, interruptible by the stop event."""
        try:
            secs = float(seconds)
        except Exception as e:
            return f"Timer error: {str(e)}"
        end = time.time() + secs
        while time.time() < end:
            if self._stop_event.is_set():
                raise StopRequested()
            time.sleep(0.1)
        return f"Waited for {secs} seconds."

    # ------------------------------------------------------------------

    def _extract_json(self, text):
        """Finds and parses the first JSON block from text."""
        # Strip markdown code blocks if present
        text = re.sub(r'```json\s*(.*?)\s*```', r'\1', text, flags=re.DOTALL)

        try:
            start_idx = text.find('{')
            if start_idx != -1:
                # Use JSONDecoder to find the first complete JSON object
                decoder = json.JSONDecoder()
                obj, _ = decoder.raw_decode(text[start_idx:])
                return obj
        except Exception as e:
            print(f"[!] _extract_json failed: {e}\n    text: {repr(text[:200])}")
            return None
        return None

    def _append_model_message(self, message, history, use_local_history):
        if use_local_history:
            history.append(message)
        else:
            self.memory.add_message(message)

    def _append_tool_result_messages(self, messages, history, use_local_history):
        for message in messages:
            if use_local_history:
                history.append(message)
            else:
                self.memory.add_message(message)

    def _append_internal_instruction(self, text, history, use_local_history):
        message = {"role": "user", "content": text}
        if use_local_history:
            history.append(message)
        else:
            if self.config.get("tool_profile") == "finance_chat_readonly":
                message["mmclaw_internal"] = True
            self.memory.add_message(message)

    def _trace(self, event, **fields):
        payload = {"event": event, **fields}
        try:
            self.memory.append_trace(payload)
        except Exception as exc:
            if self.debug:
                print(f"[!] Trace write failed: {exc}")

    @staticmethod
    def _content_hash(value):
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:20]

    def _needs_company_research(self, evidence_store):
        """Status/identity lookups alone cannot establish a company data gap."""
        if self.config.get('tool_profile') != 'finance_chat_readonly':
            return False
        if not re.search(r'分析|研究|综合|基本面|财务|因子|图谱', evidence_store.user_text):
            return False
        calls = evidence_store.tool_calls
        if 'finance_research_bundle' in calls:
            return False  # An empty or failed real lookup must not loop forever.
        company = re.search(r'(?<!\d)[0368]\d{5}(?!\d)', evidence_store.user_text)
        company = company or any(item.get('tool_name') == 'finance_resolve_entities'
                                 for item in evidence_store.items.values())
        substantive = any(item.get('claim_type') != 'data_quality'
                          for item in evidence_store.items.values())
        return bool(company) and not substantive

    def _guard_finance_answer(self, draft, evidence_store, ask_messages, is_background):
        enabled = self.config.get("finance_v3", {}).get(
            "answer_guard_enabled",
            self.config.get("tool_profile") == "finance_chat_readonly",
        )
        if not enabled or is_background or not str(draft or "").strip():
            return draft
        draft = evidence_store.normalize_citations(draft)
        evidence_store.persist()
        first = evidence_store.validate(draft)
        if first["passed"]:
            evidence_store.persist_audit(draft, draft, "passed", first, False)
            return draft
        # Failed/blocked tool output and old turns can contain citation IDs that
        # are not in this turn's evidence store. A repair must work from the
        # accepted ledger, rather than copying those rejected IDs again.
        repair_messages = [message for message in ask_messages if message.get("role") == "system"] + [{
            "role": "user",
            "content": f"Current user request:\n{evidence_store.user_text}\n\n"
                       + evidence_store.repair_prompt(draft, first),
        }]
        try:
            repair_started = time.monotonic()
            repaired_msg = self._ask_with_stop(repair_messages, tools=None)
            persist_model_call(
                evidence_store.turn_id, self.config, "answer_repair",
                prompt_hash=self._content_hash(repair_messages), tool_schema_hash=None,
                duration_ms=round((time.monotonic() - repair_started) * 1000, 1),
                status="ok", response=repaired_msg,
            )
            repaired = str((repaired_msg or {}).get("content") or "").strip()
            # JSON-protocol providers can keep their envelope during repair.
            # Validate and send only the final answer, never internal fields.
            repair_data = self._extract_json(repaired)
            if isinstance(repair_data, dict) and (
                "content" in repair_data or "tools" in repair_data
            ):
                if repair_data.get("tools"):
                    raise ValueError("answer repair requested tool calls")
                repaired = repair_data.get("content")
                if not isinstance(repaired, str) or not repaired.strip():
                    raise ValueError("answer repair returned no final text")
                repaired = repaired.strip()
            repaired = evidence_store.normalize_citations(repaired)
            second = evidence_store.validate(repaired)
            self._trace(
                "answer_repair",
                turn_id=evidence_store.turn_id,
                duration_ms=round((time.monotonic() - repair_started) * 1000, 1),
                first_violations=first["violations"],
                second_violations=second["violations"],
                draft_answer=draft,
                repaired_answer=repaired,
                passed=second["passed"],
            )
            if second["passed"]:
                evidence_store.persist_audit(draft, repaired, "repaired", second, True)
                return repaired
            candidates = [evidence_store.supported_answer(text, fallback_to_facts=False)
                          for text in (draft, repaired)]
            # A repair can introduce a new error. Prefer the candidate retaining
            # more supported analysis, rather than replacing a useful draft with
            # a nearly empty repaired answer.
            fallback = max(candidates, key=len) or evidence_store.supported_answer("")
            evidence_store.persist_audit(draft, fallback, "degraded", second, True)
            return fallback

        except StopRequested:
            raise
        except Exception as exc:
            persist_model_call(
                evidence_store.turn_id, self.config, "answer_repair",
                prompt_hash=self._content_hash(repair_messages), tool_schema_hash=None,
                duration_ms=round((time.monotonic() - repair_started) * 1000, 1),
                status="error", error=exc,
            )
            first["violations"].append({"code": "repair_model_error", "error": f"{type(exc).__name__}: {exc}"[:240]})
            fallback = evidence_store.supported_answer(draft)
            evidence_store.persist_audit(draft, fallback, "degraded", first, True)
            return fallback

    def _goal_model_json(self, messages, purpose, loop, evidence, accept):
        """Bounded protocol retry; planner/reviewer output is never public text."""
        for attempt in range(2):
            self._check_stop()
            started = time.monotonic()
            loop.auxiliary_calls += 1
            try:
                response = self._ask_with_stop(messages, tools=None)
                persist_model_call(evidence.turn_id, self.config, purpose,
                    prompt_hash=self._content_hash(messages), tool_schema_hash=None,
                    duration_ms=round((time.monotonic()-started)*1000, 1), status="ok", response=response)
                data = self._extract_json(str((response or {}).get("content") or ""))
                accept(data)
                return True
            except StopRequested:
                raise
            except (ValueError, TypeError, KeyError) as exc:
                self._trace("goal_protocol_error", turn_id=evidence.turn_id, purpose=purpose,
                            attempt=attempt+1, error_type=type(exc).__name__)
                messages = list(messages)+[{"role":"user", "content":
                    "The control object was invalid: "+str(exc)[:250]+". Return the required JSON object only."}]
            except Exception as exc:
                persist_model_call(evidence.turn_id, self.config, purpose,
                    prompt_hash=self._content_hash(messages), tool_schema_hash=None,
                    duration_ms=round((time.monotonic()-started)*1000, 1), status="error", error=exc)
                self._trace("goal_control_error", turn_id=evidence.turn_id, purpose=purpose,
                            error_type=type(exc).__name__)
                return False
        return False

    def _plan_goal(self, loop, evidence, ask_messages):
        context = [{"role":m["role"], "content":str(m.get("content") or "")[:2000]}
                   for m in ask_messages if m.get("role") in {"user", "assistant"}][-6:]
        if not self._goal_model_json(loop.plan_messages(context), "goal_plan", loop, evidence, loop.accept_plan):
            loop.minimal_plan()
        self._trace("goal_plan", turn_id=evidence.turn_id, goal=loop.goal, requirements=loop.requirements,
                    steps=loop.steps, constraints=loop.constraints, response_mode=loop.response_mode,
                    selected_tools=loop.selected_tools, plan_status=loop.plan_status)

    def _review_goal(self, loop, evidence, candidate):
        if loop.review_count >= loop.max_reviews:
            loop.stop_reason = "review_budget"
            return {"publish":bool(candidate.strip()), "decision":"partial"}
        accepted = self._goal_model_json(loop.review_messages(evidence, candidate), "goal_review", loop, evidence,
                                        lambda data: loop.accept_review(data, evidence, candidate))
        if not accepted:
            loop.review_failed()
        self._trace("goal_review", turn_id=evidence.turn_id, requirements=loop.requirements,
                    review=loop.last_review, selected_tools=loop.selected_tools, review_count=loop.review_count)
        return loop.last_review

    def _execute_goal_tool(self, name, args, silent_tools, is_background, loop):
        if not loop.enabled:
            return self._execute_tool_call(name, args, silent_tools, is_background)
        invalid = loop.argument_error(name, args)
        if invalid:
            return json.dumps({"meta":{"status":"invalid_request", "message":invalid},
                               "rows":[], "evidence":[]}, ensure_ascii=False), False
        try:
            result, reset = self._execute_tool_call(name, args, silent_tools, is_background)
            if name.startswith("finance_"):
                result = with_quality_evidence(name, result)
            return result, reset
        except StopRequested:
            raise
        except Exception as exc:
            # An individual tool failure is an observation for replanning, not
            # a provider failure that aborts the entire user's request.
            return json.dumps({"meta":{"status":"error", "error_type":type(exc).__name__,
                "message":"Tool execution failed. Recheck arguments and use another suitable capability if needed."},
                "rows":[], "evidence":[]}), False

    def _deliver_answer(self, draft, evidence_store, ask_messages, is_background, history, use_local_history,
                        research_loop=None, *, guarded=False):
        final = draft if guarded else self._guard_finance_answer(draft, evidence_store, ask_messages, is_background)
        audited = final
        if research_loop is not None and research_loop.enabled and not research_loop.delivery_allowed(audited):
            self._trace("answer_withheld", turn_id=evidence_store.turn_id, reason="delivery_not_approved")
            return ""
        if self.config.get("tool_profile") == "finance_chat_readonly":
            checked = evidence_store.validate(audited)
            self._trace("answer_validation", turn_id=evidence_store.turn_id,
                        passed=checked["passed"], violations=checked["violations"])
            readable = render_finance_text(final, evidence_store.items)
            final = readable
        if research_loop is not None and not research_loop.record_answer(final, audited):
            research_loop.review_status = "continue"
            research_loop.last_review.update(decision="continue", publish=False)
            research_loop.next_step = "The rendered answer duplicates a delivered result. Deliver the still-unmet requirement."
            self._trace("answer_withheld", turn_id=evidence_store.turn_id, reason="duplicate_rendered_answer")
            return ""
        self._trace("answer_presentation", turn_id=evidence_store.turn_id,
                    audited_answer=audited, presented_answer=final,
                    delivery=dict(research_loop.last_review.get("candidate_delivery") or {}) if research_loop else {})
        # Persist exactly what the user sees, never a rejected pre-guard draft.
        self._append_model_message({"role": "assistant", "content": final}, history, use_local_history)
        self.connector.send(final)
        return final

    def _notify_incomplete_goal(self, loop, turn_id):
        pending = [r["description"] for r in loop.requirements
                   if r["id"] not in loop.delivered_requirement_ids]
        if not pending:
            return
        text = "本次分析尚未完成：" + "；".join(pending) + "。已取得的材料已保留，本次没有发布未通过检查的分析片段。"
        self._trace("incomplete_goal_notice", turn_id=turn_id, pending=pending, reason=loop.stop_reason or loop.review_status)
        self.connector.send(text)

    def _handle_answer_stage(self, payload, evidence_store, ask_messages, is_background,
                             history, use_local_history, research_loop, tool_control, silent_content):
        content = payload.get("content") or ""
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        if not research_loop.enabled:
            if content.strip() and not silent_content:
                self._deliver_answer(content, evidence_store, ask_messages, is_background, history, use_local_history)
            return False
        changed = research_loop.update(payload)
        if not content.strip() and changed and payload.get("task_status") != "complete":
            self._append_internal_instruction("Tool selection updated. Execute the next planned query now.", history, use_local_history)
            return True
        candidate = self._guard_finance_answer(content, evidence_store, ask_messages, is_background)
        review = self._review_goal(research_loop, evidence_store, candidate)
        self._check_stop()
        if review.get("publish") and candidate.strip() and not silent_content:
            self._deliver_answer(candidate, evidence_store, ask_messages, is_background,
                                 history, use_local_history, research_loop, guarded=True)
        elif candidate.strip():
            self._append_internal_instruction("Unpublished draft for revision; this has NOT been sent to the user. "
                                              "Return a complete revised answer including correct sections, not just an addendum:\n"+candidate,
                                              history, use_local_history)
        followup = research_loop.followup(payload, evidence_store, tool_control)
        self._trace("research_stage", turn_id=evidence_store.turn_id,
                    continued=bool(followup), task_status=payload.get("task_status"),
                    **research_loop.summary())
        if followup:
            self._append_internal_instruction(followup, history, use_local_history)
        elif not review.get("publish") and not silent_content:
            self._notify_incomplete_goal(research_loop, evidence_store.turn_id)
        return bool(followup)

    def _execute_tool_call(self, name, args, silent_tools, is_background):
        result = ""
        session_reset = False
        if not is_tool_enabled(name, self.config):
            return f"Error: tool '{name}' is disabled by current tool policy.", False

        if name == "shell_execute":
            if not silent_tools:self.connector.send(f"🐚 Shell: `{args.get('command')}`")
            if is_background:
                result = ShellTool.execute(args.get("command"))
            else:
                result = self._shell_execute_with_stop(args.get("command"))
        elif name == "shell_async":
            if not silent_tools:self.connector.send(f"🚀 Async Shell: `{args.get('command')}`")
            result = AsyncShellTool.execute(args.get("command"))
        elif name == "file_read":
            if not silent_tools:self.connector.send(f"📖 Read: `{args.get('path')}`")
            result = FileTool.read(args.get("path"))
        elif name == "file_write":
            if not silent_tools:self.connector.send(f"💾 Write: `{args.get('path')}`")
            result = FileTool.write(args.get("path"), args.get("content"))
        elif name == "file_upload":
            if not silent_tools:self.connector.send(f"📤 Upload: `{args.get('path')}`")
            self.connector.send_file(args.get("path"))
            result = f"File {args.get('path')} sent."
        elif name == "finance_time_context":
            if not silent_tools:self.connector.send("Finance time context")
            result = FinanceDBTool.time_context(**args)
        elif name == "finance_resolve_entities":
            if not silent_tools:self.connector.send("Finance entity resolution")
            result = FinanceDBTool.resolve_entities(**args)
        elif name == "finance_data_status":
            if not silent_tools:self.connector.send("Finance data status")
            result = FinanceDBTool.data_status(**args)
        elif name == "finance_market_series_batch":
            if not silent_tools:self.connector.send("Finance market series batch")
            result = FinanceDBTool.market_series_batch(**args)
        elif name == "finance_fundamentals_batch":
            if not silent_tools:self.connector.send("Finance fundamentals batch")
            result = FinanceDBTool.fundamentals_batch(**args)
        elif name == "finance_factor_analysis":
            if not silent_tools:self.connector.send("Finance factor analysis")
            result = FinanceDBTool.factor_analysis(**args)
        elif name == "finance_graph_analysis":
            if not silent_tools:self.connector.send("Finance graph analysis")
            result = FinanceDBTool.graph_analysis(**args)
        elif name == "finance_research_intelligence":
            result = FinanceDBTool.research_intelligence(**args)
        elif name == "finance_research_bundle":
            if not silent_tools:self.connector.send("Finance research bundle")
            result = FinanceDBTool.research_bundle(**args)
        elif name == "finance_claim_verify":
            if not silent_tools:self.connector.send("Finance claim verification")
            result = FinanceDBTool.claim_verify(**args)
        elif name == "finance_current_time":
            if not silent_tools:self.connector.send("🕒 Current time")
            result = FinanceDBTool.current_time(args.get("timezone", "Asia/Shanghai"))
        elif name == "finance_calculator":
            if not silent_tools:self.connector.send("🧮 Finance calculator")
            result = SafeCalculatorTool.calculate(
                args.get("expression"),
                args.get("variables"),
                args.get("precision", 6),
                args.get("unit"),
            )
        elif name == "finance_plot_chart":
            if not silent_tools:self.connector.send("📈 Finance plot chart")
            result = FinanceDBTool.plot_chart(
                args.get("code"),
                args.get("filename"),
                args.get("width", 10),
                args.get("height", 6),
                args.get("dpi", 160),
            )
        elif name == "finance_db_query":
            if not silent_tools:self.connector.send("📊 Finance DB query")
            result = FinanceDBTool.query(args.get("sql"), args.get("limit", 200))
        elif name == "finance_db_schema":
            if not silent_tools:self.connector.send("📊 Finance DB schema")
            result = FinanceDBTool.schema(args.get("table"), args.get("schema"))
        elif name == "finance_theme_map":
            if not silent_tools:self.connector.send("📊 Finance theme map")
            result = FinanceDBTool.theme_map(args.get("theme"), args.get("limit", 30))
        elif name == "finance_price_movers":
            if not silent_tools:self.connector.send("📊 Finance price movers")
            result = FinanceDBTool.price_movers(
                args.get("days", 5),
                args.get("threshold", 0.2),
                args.get("direction", "down"),
                args.get("theme"),
                args.get("limit", 50),
            )
        elif name == "finance_research_momentum":
            if not silent_tools:self.connector.send("📊 Finance research momentum")
            result = FinanceDBTool.research_momentum(
                args.get("days", 30),
                args.get("theme"),
                args.get("stock_code"),
                args.get("limit", 50),
            )
        elif name == "finance_screen_stocks":
            if not silent_tools:self.connector.send("📊 Finance stock screen")
            result = FinanceDBTool.screen_stocks(**args)
        elif name == "finance_stock_profile":
            if not silent_tools:self.connector.send("📊 Finance stock profile")
            result = FinanceDBTool.stock_profile(
                args.get("stock_code"),
                args.get("stock_name"),
                args.get("days", 30),
                args.get("limit", 80),
            )
        elif name == "finance_asset_snapshot":
            if not silent_tools:self.connector.send("📊 Finance asset snapshot")
            result = FinanceDBTool.asset_snapshot(
                args.get("as_of_date"),
                args.get("stock_code"),
                args.get("stock_name"),
                args.get("limit", 50),
            )
        elif name == "finance_fundamental_analysis":
            if not silent_tools:self.connector.send("📑 Finance fundamentals")
            result = FinanceDBTool.fundamental_analysis(**args)
        elif name == "finance_peer_compare":
            if not silent_tools:self.connector.send("⚖️ Finance peer comparison")
            result = FinanceDBTool.peer_compare(**args)
        elif name == "finance_relative_valuation":
            if not silent_tools:self.connector.send("📐 Finance relative valuation")
            result = FinanceDBTool.relative_valuation(**args)
        elif name == "finance_capital_flow_analysis":
            if not silent_tools:self.connector.send("💹 Finance capital flow")
            result = FinanceDBTool.capital_flow_analysis(**args)
        elif name == "finance_macro_context":
            if not silent_tools:self.connector.send("🌐 Finance macro context")
            result = FinanceDBTool.macro_context(**args)
        elif name == "finance_factor_catalog":
            if not silent_tools:self.connector.send("Finance factor catalog")
            result = FinanceDBTool.factor_catalog(**args)
        elif name == "finance_factor_profile":
            if not silent_tools:self.connector.send("Finance factor profile")
            result = FinanceDBTool.factor_profile(**args)
        elif name == "finance_factor_validation":
            if not silent_tools:self.connector.send("Finance factor validation")
            result = FinanceDBTool.factor_validation(**args)
        elif name == "finance_factor_readiness":
            if not silent_tools:self.connector.send("Finance factor readiness")
            result = FinanceDBTool.factor_readiness(**args)
        elif name == "finance_factor_attribution":
            if not silent_tools:self.connector.send("Finance factor attribution")
            result = FinanceDBTool.factor_attribution(**args)
        elif name == "finance_research_workflow":
            if not silent_tools:self.connector.send("Finance research workflow")
            result = FinanceDBTool.research_workflow(**args)
        elif name == "finance_knowledge_graph":
            if not silent_tools:self.connector.send("Finance knowledge graph")
            result = FinanceDBTool.knowledge_graph(**args)
        elif name == "finance_graph_paths":
            if not silent_tools:self.connector.send("Finance graph paths")
            result = FinanceDBTool.graph_paths(**args)
        elif name == "finance_event_calendar":
            if not silent_tools:self.connector.send("Finance event calendar")
            result = FinanceDBTool.event_calendar(**args)
        elif name == "finance_portfolio_risk":
            if not silent_tools:self.connector.send("Finance portfolio risk")
            result = FinanceDBTool.portfolio_risk(**args)
        elif name == "finance_watchlist_monitor":
            if not silent_tools:self.connector.send("Finance watchlist monitor")
            result = FinanceDBTool.watchlist_monitor(**args)
        elif name == "finance_strategy_backtest":
            if not silent_tools:self.connector.send("Finance strategy backtest")
            result = FinanceDBTool.strategy_backtest(**args)
        elif name == "finance_entity_resolve":
            if not silent_tools:self.connector.send("🔎 Finance entity resolution")
            result = FinanceDBTool.entity_resolve(
                args.get("query"),
                args.get("asset_type"),
                args.get("limit", 10),
            )
        elif name == "finance_data_context":
            if not silent_tools:self.connector.send("📚 Finance data context")
            result = FinanceDBTool.data_context(
                args.get("intent"),
                args.get("metric_keys"),
                args.get("dataset_keys"),
                args.get("query_keys"),
                args.get("include_health", True),
                args.get("limit", 30),
            )
        elif name == "finance_market_screen":
            if not silent_tools:self.connector.send("📊 Finance full-market screen")
            if self.config.get("tool_profile") == "finance_chat_readonly":
                result = FinanceDBTool.market_screen_v3(**args)
            else:
                result = FinanceDBTool.market_screen(**args)
        elif name == "finance_document_retrieve":
            if not silent_tools:self.connector.send("📄 Finance document retrieval")
            if self.config.get("tool_profile") == "finance_chat_readonly":
                result = FinanceDBTool.document_retrieve_v3(**args)
            else:
                result = FinanceDBTool.document_retrieve(
                    args.get("query"),
                    args.get("keywords"),
                    args.get("source_types"),
                    args.get("days", 30),
                    args.get("stock_code"),
                    args.get("intent", "ranked"),
                    args.get("require_all_terms", False),
                    args.get("limit", 30),
                    args.get("candidate_limit", 500),
                    args.get("snippet_chars", 420),
                    args.get("include_body", False),
                    args.get("backend", "auto"),
                )
        elif name == "finance_document_search":
            if not silent_tools:self.connector.send("📄 Finance document search")
            result = FinanceDBTool.document_search(
                args.get("query"),
                args.get("keywords"),
                args.get("source_types"),
                args.get("days", 30),
                args.get("stock_code"),
                args.get("require_all_terms", False),
                args.get("limit", 30),
                args.get("snippet_chars", 420),
                args.get("include_body", False),
                args.get("backend", "auto"),
            )
        elif name == "finance_document_bm25_search":
            if not silent_tools:self.connector.send("📄 Finance relevance document search")
            result = FinanceDBTool.document_bm25_search(
                args.get("query"),
                args.get("keywords"),
                args.get("source_types"),
                args.get("days", 30),
                args.get("stock_code"),
                args.get("limit", 30),
                args.get("candidate_limit", 500),
                args.get("include_body_candidates", False),
                args.get("snippet_chars", 420),
                args.get("backend", "auto"),
            )
        elif name == "finance_topic_clusters":
            if not silent_tools:self.connector.send("📄 Finance topic clusters")
            result = FinanceDBTool.topic_clusters(
                args.get("query"),
                args.get("keywords"),
                args.get("source_types"),
                args.get("days", 30),
                args.get("limit", 30),
            )
        elif name == "finance_document_evidence":
            if not silent_tools:self.connector.send("📄 Finance document evidence")
            result = FinanceDBTool.document_evidence(
                args.get("query"),
                args.get("keywords"),
                args.get("source_types"),
                args.get("days", 30),
                args.get("limit", 30),
                args.get("snippet_chars", 520),
                args.get("candidate_limit", 500),
                args.get("include_body", False),
                args.get("backend", "auto"),
            )
        elif name == "finance_subindustry_trends":
            if not silent_tools:self.connector.send("📊 Finance subindustry trends")
            result = FinanceDBTool.subindustry_trends(
                args.get("subindustry"),
                args.get("days", 30),
                args.get("limit", 30),
            )
        elif name == "finance_company_exposure":
            if not silent_tools:self.connector.send("📊 Finance company exposure")
            result = FinanceDBTool.company_exposure(
                args.get("query"),
                args.get("term"),
                args.get("subindustry"),
                args.get("stock_code"),
                args.get("stock_name"),
                args.get("limit", 50),
            )
        elif name == "finance_forecast_trends":
            if not silent_tools:self.connector.send("📊 Finance forecast trends")
            result = FinanceDBTool.forecast_trends(
                args.get("stock_code"),
                args.get("stock_name"),
                args.get("subindustry"),
                args.get("days", 180),
                args.get("limit", 50),
            )
        elif name == "finance_event_study":
            if not silent_tools:self.connector.send("📊 Finance event study")
            result = FinanceDBTool.event_study(
                args.get("query"),
                args.get("keywords"),
                args.get("event_type"),
                args.get("stock_code"),
                args.get("days", 90),
                args.get("post_days", 5),
                args.get("limit", 80),
            )
        elif name == "finance_financial_growth_screen":
            if not silent_tools:self.connector.send("📊 Finance actual growth screen")
            result = FinanceDBTool.financial_growth_screen(
                args.get("report_period", "2026Q1"),
                args.get("metric", "net_profit_parent_yoy"),
                args.get("growth_gt", 0.5),
                args.get("growth_lt"),
                args.get("return_lte"),
                args.get("return_gte"),
                args.get("latest_date"),
                args.get("price_adjustment", "qfq"),
                args.get("limit", 50),
            )
        elif name == "finance_event_return":
            if not silent_tools:self.connector.send("📊 Finance event return")
            result = FinanceDBTool.event_return(
                args.get("stock_code"),
                args.get("stock_name"),
                args.get("event_type", "financial_report"),
                args.get("event_date"),
                args.get("price_adjustment", "qfq"),
                args.get("limit", 50),
            )
        elif name == "finance_fact_check_stock":
            if not silent_tools:self.connector.send("📊 Finance fact check")
            result = FinanceDBTool.fact_check_stock(
                args.get("stock_code"),
                args.get("stock_name"),
                args.get("report_period", "2026Q1"),
                args.get("price_adjustment", "qfq"),
                args.get("days", 180),
                args.get("limit", 80),
            )
        elif name == "finance_target_price_history":
            if not silent_tools:self.connector.send("🎯 Finance explicit target prices")
            result = FinanceDBTool.target_price_history(
                args.get("stock_codes"), args.get("stock_code"), args.get("stock_name"),
                args.get("days", 365), args.get("as_of_date"), args.get("limit", 50),
            )
        elif name == "finance_price_series":
            if not silent_tools:self.connector.send("📈 Finance canonical price series")
            result = FinanceDBTool.price_series(
                args.get("stock_codes"), args.get("stock_code"), args.get("stock_name"),
                args.get("start_date"), args.get("end_date"), args.get("adjustment", "qfq"),
                args.get("frequency", "daily"), args.get("limit", 240),
            )
        elif name == "finance_price_chart":
            if not silent_tools:self.connector.send("📈 Finance price chart")
            result = FinanceDBTool.price_chart(
                args.get("stock_codes"), args.get("stock_code"), args.get("stock_name"),
                args.get("start_date"), args.get("end_date"), args.get("adjustment", "qfq"),
                args.get("frequency", "weekly"), args.get("normalize", False), args.get("filename"),
            )
            try:
                chart_path = json.loads(result).get("path")
                if chart_path and not silent_tools:
                    self.connector.send_file(chart_path)
            except Exception:
                pass
        elif name == "finance_limit_up_list":
            if not silent_tools:self.connector.send("📈 Finance limit-up list")
            result = FinanceDBTool.limit_up_list(
                args.get("as_of_date"), args.get("exclude_ipo_window", True), args.get("limit", 200)
            )
        elif name == "finance_data_health":
            if not silent_tools:self.connector.send("📊 Finance data health")
            result = FinanceDBTool.data_health(args.get("limit", 50))
        elif name == "wait":
            if not silent_tools:self.connector.send(f"⏳ Waiting {args.get('seconds')}s...")
            if is_background:
                result = TimerTool.wait(args.get("seconds"))
            else:
                result = self._wait_with_stop(args.get("seconds"))
        elif name == "reset_session":
            self.memory.reset()
            if not silent_tools:self.connector.send("✨ Session reset! Starting fresh.")
            result = "Success: Session history cleared."
            session_reset = True
        elif name == "memory_add":
            if not silent_tools:self.connector.send(f"🧠 Memorize: `{args.get('memory', '')}`")
            result = self.memory.global_memory_add(args.get("memory", ""))
        elif name == "memory_list":
            if not silent_tools:self.connector.send("🧠 Listing global memories...")
            result = self.memory.global_memory_list()
        elif name == "memory_delete":
            indices = args.get("indices", args.get("index", -1))
            if isinstance(indices, list):
                indices = [int(i) for i in indices]
            else:
                indices = int(indices)
            if not silent_tools:self.connector.send(f"🧠 Delete memory {indices}")
            result = self.memory.global_memory_delete(indices)
        elif name == "browser_start":
            if not silent_tools:self.connector.send("🌐 Starting browser...")
            user_data_dir = self.config.get("browser", {}).get("data_dir")
            result = BrowserTool.start(user_data_dir=user_data_dir)
        elif name == "browser_stop":
            if not silent_tools:self.connector.send("🌐 Stopping browser...")
            result = BrowserTool.stop()
        elif name == "browser_navigate":
            if not silent_tools:self.connector.send(f"🌐 Navigate: `{args.get('url')}`")
            result = BrowserTool.navigate(args.get("url"))
        elif name == "browser_click":
            if not silent_tools:self.connector.send(f"🌐 Click: `{args.get('selector')}`")
            result = BrowserTool.click(args.get("selector"))
        elif name == "browser_fill":
            if not silent_tools:self.connector.send(f"🌐 Fill: `{args.get('selector')}`")
            result = BrowserTool.fill(args.get("selector"), args.get("text", ""))
        elif name == "browser_get_text":
            if not silent_tools:self.connector.send(f"🌐 Get text: `{args.get('selector', 'body')}`")
            result = BrowserTool.get_text(args.get("selector"))
        elif name == "browser_screenshot":
            if not silent_tools:self.connector.send("🌐 Screenshot...")
            result = BrowserTool.screenshot(args.get("path"))
            if result.startswith("OK:"):
                if not silent_tools:self.connector.send_file(result[4:].strip())
        elif name == "cron_create":
            if not silent_tools:self.connector.send(f"⏰ Cron create: `{args.get('name')}`")
            result = self.cron.create(args.get("name"), args.get("cron"), args.get("prompt"))
        elif name == "cron_delete":
            indices = args.get("indices", args.get("index", -1))
            if isinstance(indices, list):
                indices = [int(i) for i in indices]
            else:
                indices = int(indices)
            if not silent_tools:self.connector.send(f"⏰ Cron delete: {indices}")
            result = self.cron.delete(indices)
        elif name == "cron_list":
            if not silent_tools:self.connector.send("⏰ Listing cron jobs...")
            result = self.cron.list_jobs()
        elif name == "upgrade":
            if not silent_tools:self.connector.send("⬆️ Upgrading MMClaw... (this is tricky — there's no notification when it's done. Please wait a moment, then ask me for my version number to confirm the upgrade succeeded.)")
            result = UpgradeTool.upgrade()
            if not silent_tools:self.connector.send(f"❌ Upgrade failed: {result}")
        else:
            result = f"Error: unknown tool '{name}'."

        return result, session_reset

    def _worker(self, q: queue.Queue, mode: str):
        while True:
            queue_item = q.get()
            if queue_item is None:
                break
            user_text, item_meta = self._unpack_queue_item(queue_item)

            is_background = mode != "chat"
            history = []
            turn_history_start = None
            turn_clock = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()

            if mode == "heartbeat":
                silent_tools   = True
                silent_content = user_text.startswith("[HEARTBEAT_DISCOVER:")
                history = [{"role": "user", "content": user_text}]
            elif mode == "cron":
                silent_tools   = True
                silent_content = False
                history = [{"role": "user", "content": user_text}]
            else:  # chat
                self._activate_task_context(item_meta)
                self._stop_event.clear()
                silent_tools   = (self.config.get("tool_profile") == "finance_chat_readonly"
                                  or (isinstance(user_text, str) and user_text.startswith("[WATCHER:")))
                silent_content = False
                if self.use_stateless_arg_connector:
                    history = [{"role": "user", "content": user_text}]
                else:
                    turn_history_start = len(self.memory.history)
                    self.memory.add("user", user_text)

            turn_id = uuid.uuid4().hex
            evidence_store = EvidenceStore(turn_id, user_text)
            tool_control = ToolLoopController.from_config(self.config, background=is_background)
            research_loop = ResearchLoop(self.config, user_text, background=is_background)
            research_loop.configure_tools(tool_control)
            if research_loop.enabled:
                research_loop.bind_tools(get_native_tool_schemas(self.config, route=False))
            final_instruction_added = False
            research_followup_added = False
            model_call_count = 0
            self._trace(
                "turn_start",
                turn_id=turn_id,
                mode=mode,
                tool_profile=self.config.get("tool_profile", "default"),
                engine_type=self.config.get("engine_type", "unknown"),
                user_chars=len(str(user_text or "")),
            )
            self.connector.start_typing()
            try:
                json_retries_left = JSON_PARSE_RETRIES
                while True:
                    if not is_background:
                        self._check_stop()
                    research_loop.check_budget(model_call_count, tool_control)
                    if research_loop.enabled and model_call_count+research_loop.auxiliary_calls > research_loop.max_model_calls:
                        if not silent_content:
                            self._notify_incomplete_goal(research_loop, evidence_store.turn_id)
                        break

                    # Refresh system prompt before every call to pick up new skills or context changes
                    from .config import ConfigManager
                    new_prompt = ConfigManager.get_full_prompt(self.config)
                    finance_turn = self.config.get("tool_profile") == "finance_chat_readonly"
                    if finance_turn:
                        new_prompt += (
                            "\n[CURRENT FINANCE TURN]\n"
                            f"Turn started at {turn_clock} (Asia/Shanghai). "
                            "For an undated company analysis, use this turn's current time; "
                            "omit decision_cutoff/as_of_date to query current data, or use a fresh finance_time_context. "
                            "Never copy a cutoff or gate verdict from an earlier tool call. "
                            "If the user explicitly asks for historical analysis, preserve that requested cutoff. "
                            "Only evidence IDs obtained in this turn are citable.\n"
                        )
                    self.memory.update_system_prompt(new_prompt)

                    use_local_history = is_background or self.use_stateless_arg_connector
                    if finance_turn and not use_local_history:
                        ask_messages = self.memory.get_for_finance_turn(turn_history_start)
                    else:
                        ask_messages = [self.memory.get_all()[0]] + history if use_local_history else self.memory.get_all()
                    if research_loop.enabled and research_loop.plan_status == "pending":
                        self._plan_goal(research_loop, evidence_store, ask_messages)
                    progress = research_loop.instruction(evidence_store, tool_control)
                    if progress:
                        ask_messages = list(ask_messages) + [{"role": "system", "content": progress}]

                    native_protocol = (
                        self.config.get("tool_calling_mode", "native") == "native"
                        and self.engine.supports_native_tools
                    )
                    exposed_tools = (research_loop.selected_schemas() if research_loop.enabled
                                     else get_native_tool_schemas(self.config, context_text=user_text))
                    if tool_control.force_final:
                        exposed_tools = []
                    native_tools = exposed_tools if native_protocol else None
                    if not native_protocol:
                        if exposed_tools:
                            tool_contract = (
                                "[TOOLS AVAILABLE THIS ROUND]\n"
                                "Only call tools from these schemas. Put calls in the top-level tools array "
                                "and stop immediately after the JSON object:\n"
                                + json.dumps(exposed_tools, ensure_ascii=False, separators=(",", ":"))
                            )
                        elif research_loop.enabled and not tool_control.force_final:
                            tool_contract = (
                                "[NO TOOLS SELECTED]\nAnswer from supplied material if sufficient, or select_tools "
                                "from the capability catalog and return tools=[] to request their schemas."
                            )
                        else:
                            tool_contract = (
                                "[FINAL ANSWER REQUIRED]\nDo not call tools. Return tools=[] and put the "
                                "evidence-grounded final answer in the content string."
                            )
                        ask_messages = list(ask_messages) + [{"role": "system", "content": tool_contract}]

                    model_call_count += 1
                    model_started = time.monotonic()
                    try:
                        if is_background:
                            response_msg = self.engine.ask(ask_messages, tools=native_tools)
                        else:
                            response_msg = self._ask_with_stop(ask_messages, tools=native_tools)
                    except Exception as model_exc:
                        persist_model_call(
                            turn_id, self.config, "agent_turn",
                            prompt_hash=self._content_hash(new_prompt),
                            tool_schema_hash=self._content_hash(exposed_tools) if exposed_tools else None,
                            duration_ms=round((time.monotonic() - model_started) * 1000, 1),
                            status="error", error=model_exc,
                        )
                        raise
                    model_duration_ms = round((time.monotonic() - model_started) * 1000, 1)
                    persist_model_call(
                        turn_id, self.config, "agent_turn",
                        prompt_hash=self._content_hash(new_prompt),
                        tool_schema_hash=self._content_hash(exposed_tools) if exposed_tools else None,
                        duration_ms=model_duration_ms,status="ok",response=response_msg,
                    )
                    raw_text = response_msg.get("content", "")
                    self._trace(
                        "model_call",
                        turn_id=turn_id,
                        call_index=model_call_count,
                        prompt_hash=self._content_hash(new_prompt),
                        tool_schema_hash=self._content_hash(exposed_tools) if exposed_tools else None,
                        message_count=len(ask_messages),
                        duration_ms=model_duration_ms,
                        returned_tool_calls=len(response_msg.get("tool_calls") or []),
                        response_chars=len(str(raw_text or "")),
                        forced_final=tool_control.force_final,
                    )

                    if native_protocol:
                        tool_calls = response_msg.get("tool_calls") or []

                        if not tool_calls:
                            if (not research_loop.enabled and not research_followup_added and not tool_control.force_final
                                    and self._needs_company_research(evidence_store)):
                                self._append_internal_instruction(
                                    'Only status/entity metadata has been queried. It does not establish that company data is missing. '
                                    'Call finance_research_bundle with workflow_type=company_due_diligence and the resolved stock_code '
                                    'before writing the company analysis. Honor any hard gate or actual tool failure returned.',
                                    history, use_local_history)
                                research_followup_added = True
                                continue
                            stage = self._extract_json(raw_text)
                            if not isinstance(stage, dict) or "content" not in stage:
                                stage = {"content": raw_text}
                            if self._handle_answer_stage(stage, evidence_store, ask_messages, is_background,
                                                         history, use_local_history, research_loop,
                                                         tool_control, silent_content):
                                continue
                            break

                        self._append_model_message(response_msg, history, use_local_history)
                        tool_control.begin_round()
                        results = []
                        session_reset = False
                        for tool_call in tool_calls:
                            if not is_background:
                                self._check_stop()

                            name = tool_call.get("name")
                            args = tool_call.get("args", {}) or {}
                            if name == "finance_claim_verify" and isinstance(args, dict):
                                args["turn_id"] = turn_id
                            decision = tool_control.before_call(name, args)

                            print(f"    [Native Tool Call: {name}]")
                            if self.debug:
                                print(f"    Args: {json.dumps(args)}")

                            tool_started = time.monotonic()
                            if decision.execute:
                                result, reset_requested = self._execute_goal_tool(name, args, silent_tools, is_background, research_loop)
                                error_key = tool_control.after_call(name, result)
                                research_loop.record_tool(name, args, result, error=bool(error_key))
                            else:
                                result = decision.result or tool_control.blocked_result(decision.reason or "budget")
                                reset_requested = False
                                error_key = None
                            duration_ms = round((time.monotonic() - tool_started) * 1000, 1)
                            results.append(result)
                            if decision.execute:
                                evidence_store.add_tool_result(name, result)
                                evidence_store.persist()
                            self._trace(
                                "tool_call",
                                turn_id=turn_id,
                                round=tool_control.rounds,
                                tool=name,
                                args_hash=ToolLoopController.args_hash(args),
                                signature=decision.signature,
                                executed=decision.execute,
                                blocked_reason=decision.reason,
                                duration_ms=duration_ms,
                                result_chars=len(str(result or "")),
                                row_count=ToolLoopController.row_count(result),
                                error=bool(error_key),
                                error_key_hash=self._content_hash(error_key) if error_key else None,
                            )
                            if self.debug:
                                print(f"\n    [Tool Output: {name}]\n    {result}\n")
                            if reset_requested:
                                session_reset = True
                                break

                        if session_reset:
                            break

                        result_messages = self.engine.tool_result_messages(tool_calls, results)
                        self._append_tool_result_messages(result_messages, history, use_local_history)
                        research_loop.observe_round(tool_control)
                        if tool_control.force_final and not final_instruction_added:
                            self._append_internal_instruction(
                                tool_control.final_instruction(), history, use_local_history
                            )
                            final_instruction_added = True
                        continue

                    data = self._extract_json(raw_text)
                    # print(f"[D] data={repr(data)}")
                    if not data:
                        if json_retries_left > 0:
                            json_retries_left -= 1
                            self._append_model_message({"role": "assistant", "content": raw_text}, history, use_local_history)
                            correction = "Your response was not valid JSON. Please respond with valid JSON only."
                            if use_local_history:
                                history.append({"role": "user", "content": correction})
                            else:
                                self.memory.add("user", correction)
                            continue
                        if self._handle_answer_stage({"content": raw_text}, evidence_store, ask_messages,
                                                     is_background, history, use_local_history,
                                                     research_loop, tool_control, silent_content):
                            continue
                        break

                    tools = data.get("tools", [])
                    if research_loop.enabled and tools:
                        research_loop.update(data)
                    if (not tools and not research_loop.enabled and not research_followup_added and not tool_control.force_final
                            and self._needs_company_research(evidence_store)):
                        self._append_internal_instruction(
                            'Only status/entity metadata has been queried. It does not establish that company data is missing. '
                            'Call finance_research_bundle with workflow_type=company_due_diligence and the resolved stock_code '
                            'before writing the company analysis. Honor any hard gate or actual tool failure returned.',
                            history, use_local_history)
                        research_followup_added = True
                        continue
                    if not tools:
                        if self._handle_answer_stage(data, evidence_store, ask_messages, is_background,
                                                     history, use_local_history, research_loop,
                                                     tool_control, silent_content):
                            continue
                        break

                    # A completed research stage may be delivered before querying
                    # the next one. Ordinary tool-planning text remains internal.
                    if (research_loop.enabled and data.get("content")
                            and data.get("task_status") == "continue"
                            and len(research_loop.answers) < research_loop.max_stages - 1
                            and research_loop.publishable_with_tools(data["content"], evidence_store)):
                        continued = self._handle_answer_stage(data, evidence_store, ask_messages, is_background,
                                                             history, use_local_history, research_loop,
                                                             tool_control, silent_content)
                        if not continued:
                            break

                    self._append_model_message({"role": "assistant", "content": raw_text}, history, use_local_history)
                    session_reset = False
                    tool_control.begin_round()
                    for tool in tools:
                        if not is_background:
                            self._check_stop()

                        name = tool.get("name")
                        args = tool.get("args", {})
                        if name == "finance_claim_verify" and isinstance(args, dict):
                            args["turn_id"] = turn_id

                        # JSON-protocol engines use the same centralized executor,
                        # budgets, duplicate detection, and tracing as native tools.
                        decision = tool_control.before_call(name, args)
                        tool_started = time.monotonic()
                        if decision.execute:
                            result, reset_requested = self._execute_goal_tool(
                                name, args, silent_tools, is_background, research_loop
                            )
                            error_key = tool_control.after_call(name, result)
                            research_loop.record_tool(name, args, result, error=bool(error_key))
                            evidence_store.add_tool_result(name, result)
                            evidence_store.persist()
                        else:
                            result = decision.result or tool_control.blocked_result(
                                decision.reason or "budget"
                            )
                            reset_requested = False
                            error_key = None
                        self._trace(
                            "tool_call",
                            turn_id=turn_id,
                            round=tool_control.rounds,
                            tool=name,
                            args_hash=ToolLoopController.args_hash(args),
                            signature=decision.signature,
                            executed=decision.execute,
                            blocked_reason=decision.reason,
                            duration_ms=round((time.monotonic() - tool_started) * 1000, 1),
                            result_chars=len(str(result or "")),
                            row_count=ToolLoopController.row_count(result),
                            error=bool(error_key),
                            error_key_hash=self._content_hash(error_key) if error_key else None,
                        )
                        if self.debug:
                            print(f"\n    [Tool Output: {name}]\n    {result}\n")
                        tool_output = f"Tool Output ({name}):\n{result}"
                        if use_local_history:
                            history.append({"role": "user", "content": tool_output})
                        else:
                            self.memory.add("user", tool_output)
                        if reset_requested:
                            session_reset = True
                            break
                        continue


                    if session_reset:
                        break
                    research_loop.observe_round(tool_control)
                    if tool_control.force_final and not final_instruction_added:
                        self._append_internal_instruction(
                            tool_control.final_instruction(), history, use_local_history
                        )
                        final_instruction_added = True

            except StopRequested:
                if research_loop.enabled:
                    research_loop.stop_reason = "user_stop"
                    research_loop.review_status = "cancelled"
            except Exception as e:
                print(f"[!] Worker error: {e}")
                traceback.print_exc()
                self._trace(
                    "turn_error",
                    turn_id=turn_id,
                    error_type=type(e).__name__,
                    error_hash=self._content_hash(str(e)),
                )
                self.connector.send(self._provider_error_message(e, turn_id))
            finally:
                self._trace(
                    "turn_end",
                    turn_id=turn_id,
                    mode=mode,
                    model_calls=model_call_count,
                    **tool_control.summary(),
                    **research_loop.summary(),
                )
                self.connector.stop_typing()
                q.task_done()

    def handle(self, text):
        if isinstance(text, dict) and "__mmclaw_text" in text:
            self.chat_queue.put(text)
            return
        if isinstance(text, list):
            self.chat_queue.put(text)
            return
        if text.strip() == "/stop":
            self.stop()
            return
        if text.strip() == "/new":
            self.memory.reset()
            self.connector.send("✨ Session reset! Starting fresh.")
            return
        if not self.use_stateless_arg_connector and random.random() < 0.15:
            self.connector.send("💡 Tip: type /stop at any time to cancel the current job.")
        self.chat_queue.put(text)

    def run(self, stop_on_auth=False):
        try:
            self.connector.listen(self.handle, stop_on_auth=stop_on_auth)
        except TypeError:
            self.connector.listen(self.handle)
