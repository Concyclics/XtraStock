
from xtrastock.config import bootstrap_environment
bootstrap_environment()

import os
import json
import subprocess
import threading
import time
import asyncio
import concurrent.futures
import re
import telebot
import shutil
import random
import base64
import io
import tempfile
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path
from .config import ConfigManager
from .providers import Engine, prepare_image_content
from .outbox import OutboxWorker
from .access_control import AccessController
from .presentation import render_finance_text, render_report_file


class TerminalConnector(object):
    def __init__(self):
        self._typing = False
        self._input_ready = threading.Event()
        self._input_ready.set()
        self._print_lock = threading.Lock()

    def listen(self, callback, stop_on_auth=False):
        print("\n--- MMClaw Kernel Active (Terminal Mode) ---")
        while True:
            try:
                self._input_ready.wait()
                text = input("👤 You: ").strip()
                if text.lower() in ["exit", "quit"]: break
                if text:
                    self._input_ready.clear()
                    callback(text)
            except KeyboardInterrupt: break

    def start_typing(self):
        self._typing = True
        def _animate():
            chars = ["|", "/", "-", "\\"]
            i = 0
            while self._typing:
                with self._print_lock:
                    print(f"\r    ⚡ MMClaw: {chars[i % len(chars)]} thinking...", end="", flush=True)
                i += 1
                time.sleep(0.15)
        threading.Thread(target=_animate, daemon=True).start()

    def stop_typing(self):
        self._typing = False
        time.sleep(0.2)  # Let animation thread finish its current iteration
        with self._print_lock:
            print("\r\033[K", end="", flush=True)
        self._input_ready.set()

    def send(self, message):
        with self._print_lock:
            print(f"\r\033[K    ⚡ MMClaw: {message}", flush=True)

    def send_file(self, path):
        full_path = os.path.expanduser(path)
        with self._print_lock:
            print(f"\r\033[K    ⚡ MMClaw: [FILE SENT] {os.path.abspath(full_path)}", flush=True)


class StatelessArgConnector(object):
    """Delivers a single CLI prompt (-p), runs the full agent loop without history, then exits."""
    def __init__(self, prompt):
        self._prompt = prompt
        self._done = threading.Event()
        self.file_saver = None

    def listen(self, callback, stop_on_auth=False):
        callback(self._prompt)
        self._done.wait(timeout=600)

    def start_typing(self): pass

    def stop_typing(self):
        self._done.set()

    def send(self, message):
        print(message, flush=True)

    def send_file(self, path):
        full_path = os.path.abspath(os.path.expanduser(path))
        print(f"[FILE] {full_path}", flush=True)


class FeishuConnector(object):
    def __init__(self, app_id, app_secret, config=None):
        try:
            import lark_oapi as lark
        except ImportError:
            raise ImportError("lark-oapi is required for Feishu mode. Install it with: pip install lark-oapi")

        self.lark = lark
        self.app_id = app_id
        self.app_secret = app_secret
        self.callback = None
        self.last_message_id = None
        self.config = config if config else ConfigManager.load()
        # Nested connector config
        self.fs_config = self.config.get("connectors", {}).get("feishu", {})
        self.authorized_id = self.fs_config.get("authorized_id")

        self.verify_code = str(random.randint(100000, 999999))
        self.client = lark.Client.builder() \
            .app_id(app_id) \
            .app_secret(app_secret) \
            .log_level(lark.LogLevel.INFO) \
            .build()
        self.ws_client = None
        self.stop_on_auth = False


    def _handle_message(self, data) -> None:
        from lark_oapi.api.im.v1 import GetMessageResourceRequest
        try:
            sender_id = data.event.sender.sender_id.open_id
            msg_type = data.event.message.message_type
            msg_dict = json.loads(data.event.message.content)

            # Always store last message_id for reply attempt
            self.last_message_id = data.event.message.message_id


            if not self.authorized_id:
                text = msg_dict.get("text", "").strip()
                if text == self.verify_code:
                    self.authorized_id = sender_id
                    # Save to nested config
                    if "connectors" not in self.config: self.config["connectors"] = {}
                    if "feishu" not in self.config["connectors"]: self.config["connectors"]["feishu"] = {}
                    self.config["connectors"]["feishu"]["authorized_id"] = sender_id

                    ConfigManager.save(self.config)
                    print(f"\n[⭐] AUTH SUCCESS! MMClaw is now locked to Feishu User: {sender_id}")
                    self.send("⚡ Verification Successful! I am now your personal agent.")

                    if self.stop_on_auth:
                        os._exit(0) # Brutal but effective for a CLI wizard setup
                    return
                else:
                    return

            if sender_id != self.authorized_id:
                return

            if msg_type == "text":
                text = msg_dict.get("text", "").strip()
                if text and self.callback:
                    print(f"📩 Feishu: {text}")
                    self.callback(text)
            elif msg_type == "image":
                image_key = msg_dict.get("image_key")
                try:
                    request = GetMessageResourceRequest.builder() \
                        .message_id(self.last_message_id) \
                        .file_key(image_key) \
                        .type("image") \
                        .build()
                    response = self.client.im.v1.message_resource.get(request)
                    if not response.success():
                        print(f"[!] Feishu Image Download Error: {response.code}, {response.msg}")
                        return

                    downloaded_file = response.file.read()
                    content = prepare_image_content(downloaded_file, "这张图片里有什么？")
                    print(f"📩 Feishu: [Photo] (Compressed)")
                    if self.callback:
                        self.callback(content)
                except Exception as e:
                    print(f"[!] Feishu Photo Error: {e}")
                    self.send(f"Error processing image: {e}")
            elif msg_type == "file":
                file_key = msg_dict.get("file_key")
                file_name = msg_dict.get("file_name", "file")
                try:
                    request = GetMessageResourceRequest.builder() \
                        .message_id(self.last_message_id) \
                        .file_key(file_key) \
                        .type("file") \
                        .build()
                    response = self.client.im.v1.message_resource.get(request)
                    if not response.success():
                        print(f"[!] Feishu File Download Error: {response.code}, {response.msg}")
                        return

                    file_bytes = response.file.read()
                    file_path = self.file_saver(file_name, file_bytes)

                    content = f"[Uploaded file: {file_path}]"
                    print(f"📩 Feishu: [File] {file_name}")
                    if self.callback:
                        self.callback(content)
                except Exception as e:
                    print(f"[!] Feishu File Error: {e}")
                    self.send(f"Error processing file: {e}")
        except Exception as e:
            print(f"[!] Feishu Parse Error: {e}")
        return None

    def listen(self, callback, stop_on_auth=False):
        self.callback = callback
        self.stop_on_auth = stop_on_auth
        print(f"\n--- MMClaw Kernel Active (Feishu Mode) ---")

        if not self.authorized_id:
            print(f"[🔐] 需要进行飞书身份验证")
            print(f"[*] 请将以下 6 位验证码发送给飞书机器人: {self.verify_code}")
        elif stop_on_auth:
            print(f"\n[✅] 飞书身份已验证: {self.authorized_id}")
            return

        event_handler = self.lark.EventDispatcherHandler.builder("", "") \
            .register_p2_im_message_receive_v1(self._handle_message) \
            .build()

        self.ws_client = self.lark.ws.Client(
            app_id=self.app_id,
            app_secret=self.app_secret,
            event_handler=event_handler,
            log_level=self.lark.LogLevel.INFO
        )
        self.ws_client.start()

    def start_typing(self):
        self.send("⏳")

    def stop_typing(self):
        self.send("✅")

    def send(self, message):
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
        if not self.authorized_id:
            return

        limit = 4000
        chunks = [message[i:i+limit] for i in range(0, len(message), limit)]
        for chunk in chunks:
            content = json.dumps({"text": f"⚡ {chunk}"})
            request = CreateMessageRequest.builder() \
                .receive_id_type("open_id") \
                .request_body(CreateMessageRequestBody.builder() \
                    .receive_id(self.authorized_id) \
                    .content(content) \
                    .msg_type("text") \
                    .build()) \
                .build()
            response = self.client.im.v1.message.create(request)
            if not response.success():
                print(f"[!] Feishu Send Error: {response.code}, {response.msg}")
                break

    def send_file(self, path):
        from lark_oapi.api.im.v1 import CreateFileRequest, CreateFileRequestBody, CreateMessageRequest, CreateMessageRequestBody
        if not self.authorized_id:
            return

        full_path = os.path.expanduser(path)
        if not os.path.exists(full_path):
            self.send(f"❌ File not found: {path}")
            return

        file_name = os.path.basename(full_path)

        try:
            # 1. Upload file
            with open(full_path, "rb") as f:
                request = CreateFileRequest.builder() \
                    .request_body(CreateFileRequestBody.builder() \
                        .file_type("stream") \
                        .file_name(file_name) \
                        .file(f) \
                        .build()) \
                    .build()
                response = self.client.im.v1.file.create(request)

            if not response.success():
                print(f"[!] Feishu Upload Error: {response.code}, {response.msg}")
                self.send(f"❌ Error uploading file: {response.msg}")
                return

            file_key = response.data.file_key

            # 2. Send file message (direct message)
            content = json.dumps({"file_key": file_key})
            request = CreateMessageRequest.builder() \
                .receive_id_type("open_id") \
                .request_body(CreateMessageRequestBody.builder() \
                    .receive_id(self.authorized_id) \
                    .content(content) \
                    .msg_type("file") \
                    .build()) \
                .build()

            response = self.client.im.v1.message.create(request)
            if not response.success():
                print(f"[!] Feishu Send File Error: {response.code}, {response.msg}")
        except Exception as e:
            print(f"[!] Feishu File Process Error: {e}")
            self.send(f"❌ Error processing file: {str(e)}")

class TelegramConnector(object):
    def __init__(self, token, telegram_authorized_user_id):
        self.bot = telebot.TeleBot(token)
        self.telegram_authorized_user_id = int(telegram_authorized_user_id)
        self.chat_id = None
        self._typing = False

    def start_typing(self):
        self._typing = True
        def _type_loop():
            while self._typing:
                try:
                    self.bot.send_chat_action(self.chat_id, 'typing')
                except Exception:
                    pass
                threading.Event().wait(1)
        threading.Thread(target=_type_loop, daemon=True).start()

    def stop_typing(self):
        self._typing = False

    def listen(self, callback):
        print(f"\n--- MMClaw Kernel Active (Telegram Mode) ---")
        print(f"[*] Listening for messages from User ID: {self.telegram_authorized_user_id}")

        @self.bot.message_handler(func=lambda message: message.from_user.id == self.telegram_authorized_user_id,
                                  content_types=['text', 'photo', 'document'])
        def handle_message(message):
            self.chat_id = message.chat.id
            text = message.text or message.caption or ""

            if message.content_type == 'photo':
                try:
                    file_id = message.photo[-1].file_id
                    file_info = self.bot.get_file(file_id)
                    downloaded_file = self.bot.download_file(file_info.file_path)

                    content = prepare_image_content(downloaded_file, text if text else "What is in this image?")
                    print(f"📩 Telegram: [Photo] {text} (Compressed)")
                    callback(content)
                except Exception as e:
                    print(f"[!] Telegram Photo Error: {e}")
                    self.send(f"Error processing image: {e}")
            elif message.content_type == 'document':
                try:
                    doc = message.document
                    file_info = self.bot.get_file(doc.file_id)
                    downloaded_file = self.bot.download_file(file_info.file_path)

                    file_path = self.file_saver(doc.file_name, downloaded_file)

                    content = f"[Uploaded file: {file_path}]"
                    if text:
                        content += f"\n{text}"
                    print(f"📩 Telegram: [Document] {doc.file_name}{' | ' + text if text else ''}")
                    callback(content)
                except Exception as e:
                    print(f"[!] Telegram Document Error: {e}")
                    self.send(f"Error processing file: {e}")
            else:
                if text:
                    print(f"📩 Telegram: {text}")
                    callback(text)

        @self.bot.message_handler(func=lambda message: message.from_user.id != self.telegram_authorized_user_id,
                                  content_types=['text', 'photo', 'audio', 'video', 'document', 'sticker', 'voice'])
        def unauthorized(message):
            self.bot.reply_to(message, "🚫 Unauthorized. I only respond to my master.")

        try:
            self.bot.set_my_commands([
                telebot.types.BotCommand("/new",  "Start a new session"),
                telebot.types.BotCommand("/stop", "Cancel the current job"),
            ])
        except Exception as e:
            print(f"[!] Telegram: failed to register commands: {e}")

        self.bot.infinity_polling()

    def send(self, message):
        limit = 4000
        chunks = [message[i:i+limit] for i in range(0, len(message), limit)]
        for chunk in chunks:
            try:
                self.bot.send_message(self.telegram_authorized_user_id, f"⚡ {chunk}")
            except Exception as e:
                print(f"[!] Telegram Send Error: {e}")
                break

    def send_file(self, path):
        path = os.path.expanduser(path)
        try:
            with open(path, 'rb') as f:
                self.bot.send_document(self.telegram_authorized_user_id, f)
        except Exception as e:
            self.send(f"Error sending file: {str(e)}")

class WhatsAppConnector(object):
    def __init__(self, config=None):
        self.process = None
        self.callback = None
        self.active_recipient = None
        self.config = config if config else ConfigManager.load()
        # Nested connector config
        self.wa_config = self.config.get("connectors", {}).get("whatsapp", {})
        self.authorized_id = self.wa_config.get("authorized_id")

        self.verify_code = str(random.randint(100000, 999999))
        self.last_sent_text = None
        self.bridge_path = os.path.join(os.path.dirname(__file__), "bridge.js")
        self.is_windows = os.name == 'nt'
        self._deps_checked = False
        self._typing = False
        self._stdin_lock = threading.Lock()
        self._stop_typing_event = threading.Event()
        self._ack_event = threading.Event()
        self._ack_error = None

    def _ensure_node(self):
        if not shutil.which("node"):
            print("[❌] Node.js not found. Please install Node.js to use WhatsApp mode.")
            return False
        return True

    def _get_node_env(self):
        """Prepare environment to find global node_modules."""
        env = os.environ.copy()
        try:
            npm_root = subprocess.check_output(["npm", "root", "-g"], encoding='utf-8', stderr=subprocess.DEVNULL, shell=self.is_windows).strip()
            existing_path = env.get("NODE_PATH", "")
            env["NODE_PATH"] = f"{npm_root}{os.pathsep}{existing_path}" if existing_path else npm_root
        except:
            pass
        return env

    def _ensure_deps(self):
        if self._deps_checked:
            return

        deps = ["@whiskeysockets/baileys", "qrcode-terminal", "pino"]
        missing = []

        print("[*] Verifying WhatsApp bridge dependencies...")
        env = self._get_node_env()

        for dep in deps:
            # Check if we can require the dependency using node
            # This is much faster than 'npm list'
            check = subprocess.run(
                ["node", "-e", f"require('{dep}')"],
                env=env,
                capture_output=True,
                shell=self.is_windows
            )

            if check.returncode != 0:
                missing.append(dep)

        if not missing:
            self._deps_checked = True
            return

        print(f"[!] Missing: {', '.join(missing)}")
        print(f"[*] Installing dependencies: {', '.join(missing)}...")

        try:
            # Attempt global install
            print("[*] Running: npm install -g " + " ".join(missing))
            subprocess.run(["npm", "install", "-g"] + missing, check=True, shell=self.is_windows)
        except subprocess.CalledProcessError:
            print("[!] Global install failed. Attempting local install...")
            print("[*] Running: npm install " + " ".join(missing))
            subprocess.run(["npm", "install"] + missing, check=True, shell=self.is_windows)

        self._deps_checked = True

    def listen(self, callback, stop_on_auth=False):
        if not self._ensure_node(): return
        self._ensure_deps()
        self.callback = callback
        self.stop_on_auth = stop_on_auth

        if stop_on_auth and self.authorized_id:
            print(f"\n[✅] WhatsApp identity already verified: {self.authorized_id}")
            return

        env = self._get_node_env()
        self.process = subprocess.Popen(
            ["node", self.bridge_path],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None,
            env=env, encoding='utf-8', bufsize=1, shell=self.is_windows
        )

        def output_reader():
            for line in iter(self.process.stdout.readline, ""):
                if line.startswith("JSON_EVENT:"):
                    try:
                        event = json.loads(line[11:])
                        if event["type"] == "message":
                            sender = event["from"]
                            text = event["text"].strip()
                            from_me = event.get("fromMe", False)

                            if not self.authorized_id:
                                if text == self.verify_code:
                                    self.authorized_id = sender
                                    # Save to nested config
                                    if "connectors" not in self.config: self.config["connectors"] = {}
                                    if "whatsapp" not in self.config["connectors"]: self.config["connectors"]["whatsapp"] = {}
                                    self.config["connectors"]["whatsapp"]["authorized_id"] = sender

                                    ConfigManager.save(self.config)
                                    print(f"\n[⭐] AUTH SUCCESS! MMClaw is now locked to: {sender}")

                                    if stop_on_auth:
                                        # Fire-and-forget: bypass ACK wait since we're exiting immediately
                                        try:
                                            payload = {"to": sender, "text": "⚡ Verification Successful! I am now your personal agent."}
                                            self._write_stdin(f"SEND:{json.dumps(payload)}\n")
                                        except Exception:
                                            pass
                                        self.process.terminate()
                                        os._exit(0)
                                    else:
                                        # Dispatch to thread — output_reader must keep running to handle ACKs
                                        threading.Thread(target=self.send, args=("⚡ Verification Successful! I am now your personal agent.",), daemon=True).start()
                                    continue
                                else:
                                    continue

                            if sender != self.authorized_id:
                                continue

                            if from_me and text == self.last_sent_text:
                                continue

                            print(f"📩 WhatsApp: {text}")
                            self.active_recipient = sender
                            if self.callback:
                                threading.Thread(target=self.callback, args=(text,), daemon=True).start()

                        elif event["type"] == "image":
                            sender = event["from"]
                            b64_data = event["base64"]
                            caption = event.get("caption", "").strip()
                            from_me = event.get("fromMe", False)

                            if not self.authorized_id or sender != self.authorized_id:
                                continue

                            # We allow from_me for images because the bot currently only sends
                            # documents (not imageMessages), so there is no risk of a loop.
                            # This allows users to talk to the bot from the same account.

                            try:
                                # Decode base64 to bytes
                                image_bytes = base64.b64decode(b64_data)
                                content = prepare_image_content(image_bytes, caption if caption else "What is in this image?")
                                print(f"📩 WhatsApp: [Photo] {caption} (Compressed)")
                                self.active_recipient = sender
                                if self.callback:
                                    threading.Thread(target=self.callback, args=(content,), daemon=True).start()
                            except Exception as e:
                                print(f"[!] WhatsApp Image Error: {e}")

                        elif event["type"] == "file":
                            sender = event["from"]
                            b64_data = event["base64"]
                            filename = event.get("filename", "file")
                            caption = event.get("caption", "").strip()
                            from_me = event.get("fromMe", False)

                            if not self.authorized_id or sender != self.authorized_id:
                                continue

                            try:
                                file_bytes = base64.b64decode(b64_data)
                                file_path = self.file_saver(filename, file_bytes)

                                content = f"[Uploaded file: {file_path}]"
                                if caption:
                                    content += f"\n{caption}"
                                print(f"📩 WhatsApp: [Document] {filename}{' | ' + caption if caption else ''}")
                                self.active_recipient = sender
                                if self.callback:
                                    threading.Thread(target=self.callback, args=(content,), daemon=True).start()
                            except Exception as e:
                                print(f"[!] WhatsApp Document Error: {e}")

                        elif event["type"] == "connected":
                            if not self.authorized_id:
                                print(f"\n[✅] WhatsApp Bridge Connected!")
                                print(f"[🔐] WHATSAPP VERIFICATION REQUIRED")
                                print(f"[*] PLEASE SEND THIS CODE TO YOURSELF ON WHATSAPP: {self.verify_code}")
                            else:
                                print(f"\n[✅] WhatsApp Active")
                                if stop_on_auth:
                                    self.process.terminate()
                                    return

                        elif event["type"] in ("msg_sent", "file_sent"):
                            self._ack_error = None
                            self._ack_event.set()

                        elif event["type"] in ("msg_error", "file_error"):
                            self._ack_error = event.get("error", "unknown error")
                            self._ack_event.set()

                    except Exception as e:
                        print(f"[!] Bridge Parse Error: {e}")
                else:
                    print(line, end="")

        threading.Thread(target=output_reader, daemon=True).start()
        self.process.wait()

    def _write_stdin(self, data):
        with self._stdin_lock:
            self.process.stdin.write(data)
            self.process.stdin.flush()

    def _send_presence(self, action):
        recipient = self.active_recipient or self.authorized_id
        if not self.process or not recipient: return
        try:
            payload = {"to": recipient, "action": action}
            self._write_stdin(f"TYPING:{json.dumps(payload)}\n")
        except Exception:
            pass

    def start_typing(self):
        self._typing = True
        self._stop_typing_event.clear()
        def _type_loop():
            while self._typing:
                self._send_presence("composing")
                self._stop_typing_event.wait(1)
        threading.Thread(target=_type_loop, daemon=True).start()

    def stop_typing(self):
        self._typing = False
        self._stop_typing_event.set()
        self._send_presence("paused")

    @contextmanager
    def _wa_send(self, timeout=60):
        self._ack_event.clear()
        self._ack_error = None
        yield
        if not self._ack_event.wait(timeout=timeout):
            raise TimeoutError("WhatsApp send timed out")
        if self._ack_error:
            raise RuntimeError(f"WhatsApp send failed: {self._ack_error}")

    def send(self, message):
        if not self.process or not (self.active_recipient or self.authorized_id): return
        recipient = self.active_recipient or self.authorized_id
        limit = 4000
        chunks = [message[i:i+limit] for i in range(0, len(message), limit)]
        for chunk in chunks:
            self.last_sent_text = chunk
            payload = {"to": recipient, "text": f"⚡ {chunk}"}
            try:
                with self._wa_send():
                    self._write_stdin(f"SEND:{json.dumps(payload)}\n")
            except Exception as e:
                print(f"[!] WhatsApp send error: {e}")
                break

    def send_file(self, path):
        if not self.process or not (self.active_recipient or self.authorized_id): return
        recipient = self.active_recipient or self.authorized_id
        full_path = os.path.abspath(os.path.expanduser(path))
        payload = {"to": recipient, "path": full_path}
        try:
            with self._wa_send(timeout=120):
                self._write_stdin(f"SEND_FILE:{json.dumps(payload)}\n")
        except Exception as e:
            print(f"[!] WhatsApp send_file error: {e}")

class WeChatConnector(object):
    """WeChat (Weixin iLink Bot) connector via QR login + long-poll getUpdates."""

    DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
    CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"
    BOT_TYPE = "3"
    LONG_POLL_TIMEOUT_S = 38   # slightly longer than server's 35 s hold
    MAX_CONSECUTIVE_FAILURES = 3
    CHANNEL_VERSION = "1.0.3"
    # UploadMediaType
    _MEDIA_IMAGE = 1
    _MEDIA_VIDEO = 2
    _MEDIA_FILE  = 3
    # MessageItemType
    _ITEM_TEXT  = 1
    _ITEM_IMAGE = 2
    _ITEM_FILE  = 4
    _ITEM_VIDEO = 5

    def __init__(self, config=None):
        self.config = config if config else ConfigManager.load()
        self.wc_config = self.config.get("connectors", {}).get("wechat", {})
        token_env = str(self.wc_config.get("token_env") or "").strip()
        self.token = os.getenv(token_env, "") if token_env else self.wc_config.get("token")
        self.base_url = self.wc_config.get("base_url", self.DEFAULT_BASE_URL).rstrip("/")
        self.authorized_id = self.wc_config.get("authorized_id")
        self._get_updates_buf = self.wc_config.get("get_updates_buf", "")
        self.callback = None
        self._typing = False
        self._stop_event = threading.Event()
        self._context_tokens = {}   # from_user_id -> context_token (in-memory)
        self.file_saver = None
        self._daily_report_thread_started = False
        self._outbox_worker = None
        try:
            self._access = AccessController("wechat", self.wc_config)
        except Exception as exc:
            self._access = None
            print(f"[!] WeChat database access policy fallback: {type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _random_wechat_uin(self):
        import struct
        uint32 = struct.unpack(">I", os.urandom(4))[0]
        return base64.b64encode(str(uint32).encode()).decode()

    def _build_headers(self):
        headers = {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "X-WECHAT-UIN": self._random_wechat_uin(),
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _api_post(self, endpoint, body_dict, timeout=15):
        import requests
        url = f"{self.base_url}/{endpoint}"
        resp = requests.post(url, json=body_dict, headers=self._build_headers(), timeout=timeout)
        resp.raise_for_status()
        return resp.json()

    def _save_wc_config(self):
        if "connectors" not in self.config:
            self.config["connectors"] = {}
        if "wechat" not in self.config["connectors"]:
            self.config["connectors"]["wechat"] = {}
        self.config["connectors"]["wechat"].update(self.wc_config)
        ConfigManager.save(self.config)

    def _list_config(self, key):
        value = self.wc_config.get(key, [])
        if isinstance(value, str):
            value = [value]
        return {str(x) for x in value if str(x)}

    def _bool_config(self, key, default=False):
        return bool(self.wc_config.get(key, default))

    def _int_config(self, key, default=0):
        try:
            return int(self.wc_config.get(key, default) or 0)
        except Exception:
            return default

    def _now_iso(self):
        return datetime.now(timezone.utc).isoformat()

    def _today_key(self):
        return datetime.now().strftime("%Y-%m-%d")

    def _log_event(self, event_type, payload):
        if not self._bool_config("log_messages", True):
            return
        try:
            log_dir = self.wc_config.get("log_dir") or str(ConfigManager.CONFIG_DIR / "wechat_logs")
            os.makedirs(os.path.expanduser(log_dir), exist_ok=True)
            path = os.path.join(os.path.expanduser(log_dir), f"{self._today_key()}.jsonl")
            record = {"ts": self._now_iso(), "event_type": event_type, **payload}
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            print(f"[!] WeChat Log Error: {e}")

    def _usage_path(self):
        usage_dir = self.wc_config.get("usage_dir") or str(ConfigManager.CONFIG_DIR / "wechat_usage")
        os.makedirs(os.path.expanduser(usage_dir), exist_ok=True)
        return os.path.join(os.path.expanduser(usage_dir), f"{self._today_key()}.json")

    def _load_usage(self):
        path = self._usage_path()
        if not os.path.exists(path):
            return {"date": self._today_key(), "users": {}}
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"date": self._today_key(), "users": {}}

    def _save_usage(self, usage):
        path = self._usage_path()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(usage, f, ensure_ascii=False, indent=2)

    def _is_admin_user(self, user_id):
        if self._access is not None:
            try:
                return self._access.is_admin(user_id)
            except Exception:
                pass
        return bool(user_id and user_id in self._list_config("admin_users"))

    def _ensure_authorized_admin(self):
        if not self.authorized_id or not self._bool_config("auto_admin_authorized_user", True):
            return
        admins = self.wc_config.setdefault("admin_users", [])
        if isinstance(admins, str):
            admins = [admins]
            self.wc_config["admin_users"] = admins
        if self.authorized_id not in admins:
            admins.append(self.authorized_id)
            self._save_wc_config()

    def _member_limit(self, user_id):
        if self._access is not None:
            try:
                policy = self._access.policy("user", user_id)
                return {
                    "daily_budget": policy["daily_budget"],
                    "min_interval_seconds": policy["min_interval_seconds"],
                }
            except Exception:
                pass
        if self._is_admin_user(user_id):
            return {
                "daily_budget": 0,
                "min_interval_seconds": 0,
            }
        limits = self.wc_config.get("member_limits", {}) or {}
        cfg = limits.get(user_id, {}) if isinstance(limits, dict) else {}
        return {
            "daily_budget": int(cfg.get("daily_budget", self._int_config("default_daily_budget_per_user", 10)) or 0),
            "min_interval_seconds": int(cfg.get("min_interval_seconds", self._int_config("default_min_interval_seconds", 0)) or 0),
        }

    def _is_allowed(self, user_id):
        if self._access is not None:
            try:
                return self._access.allowed(user_id)
            except Exception:
                pass
        allowed_users = self._list_config("allowed_users")
        blocked_users = self._list_config("blocked_users")
        if user_id and user_id in blocked_users:
            return False, "blocked_user"
        if allowed_users and user_id not in allowed_users:
            return False, "user_not_allowed"
        return True, ""

    def _check_and_record_budget(self, user_id):
        if self._access is not None:
            try:
                return self._access.consume(user_id)
            except Exception as exc:
                self._log_event("access_database_fallback", {"operation": "consume", "error": str(exc)})
        now = time.time()
        usage = self._load_usage()
        user = usage.setdefault("users", {}).setdefault(user_id or "unknown", {"requests": 0, "last_at": 0})
        limit = self._member_limit(user_id)
        if limit["daily_budget"] > 0 and user.get("requests", 0) >= limit["daily_budget"]:
            return False, "member_daily_budget_exceeded"
        if limit["min_interval_seconds"] > 0 and now - float(user.get("last_at", 0) or 0) < limit["min_interval_seconds"]:
            return False, "member_rate_limited"
        user["requests"] = user.get("requests", 0) + 1
        user["last_at"] = now
        self._save_usage(usage)
        return True, ""

    def _command_text(self, text):
        return (text or "").strip()

    def _is_usage_command(self, text):
        cmd = self._command_text(text).lower()
        return cmd in {"/usage", "usage", "/quota", "quota", "额度", "查询额度"} or cmd.startswith("/usage ")

    def _is_daily_report_command(self, text):
        cmd = self._command_text(text).lower()
        return cmd in {"/daily", "/日报"}

    def _fmt_limit(self, value):
        return "不限" if int(value or 0) <= 0 else str(int(value))

    def _fmt_remaining(self, used, limit):
        limit = int(limit or 0)
        if limit <= 0:
            return "不限"
        return str(max(0, limit - int(used or 0)))

    def _fmt_time(self, ts):
        try:
            if not ts:
                return "无"
            if isinstance(ts, datetime):
                return ts.astimezone().strftime("%Y-%m-%d %H:%M:%S")
            return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return "无"

    def _usage_report(self, user_id):
        if self._access is not None:
            try:
                db_usage = self._access.usage("user", user_id)
                return "\n".join([
                    f"今日额度 {self._today_key()}",
                    f"用户ID: {user_id or 'unknown'}",
                    f"用户请求: {db_usage['request_count']}/{self._fmt_limit(db_usage['daily_budget'])}",
                    f"用户剩余: {self._fmt_remaining(db_usage['request_count'], db_usage['daily_budget'])}",
                    f"最小间隔: {self._fmt_limit(db_usage['min_interval_seconds'])} 秒",
                    f"上次请求: {self._fmt_time(db_usage['last_request_at'])}",
                ])
            except Exception:
                pass
        usage = self._load_usage()
        user = usage.get("users", {}).get(user_id or "unknown", {})
        limit = self._member_limit(user_id)
        lines = [
            f"今日额度 {self._today_key()}",
            f"用户ID: {user_id or 'unknown'}",
            f"用户请求: {int(user.get('requests', 0) or 0)}/{self._fmt_limit(limit['daily_budget'])}",
            f"用户剩余: {self._fmt_remaining(user.get('requests', 0), limit['daily_budget'])}",
            f"最小间隔: {self._fmt_limit(limit['min_interval_seconds'])} 秒",
            f"上次请求: {self._fmt_time(user.get('last_at', 0))}",
        ]
        return "\n".join(lines)

    def _handle_usage_command(self, user_id, text):
        parts = self._command_text(text).split()
        if len(parts) == 1:
            self.send(self._usage_report(user_id))
            return True
        action = parts[1].lower()
        if action == "id":
            self.send(f"用户ID: {user_id or 'unknown'}")
            return True
        if action in {"set", "unset"}:
            if not self._is_admin_user(user_id):
                self.send("无权限：只有 admin_users 可以修改额度。")
                return True
            if len(parts) < 3:
                self.send("用法：/usage set <user_id> <daily_budget> [min_interval_seconds]\n或：/usage unset <user_id>")
                return True
            target_user = parts[2]
            limits = self.wc_config.setdefault("member_limits", {})
            if action == "unset":
                limits.pop(target_user, None)
                self._save_wc_config()
                if self._access is not None:
                    self._access.unset_user_limit(target_user)
                self.send(f"已移除用户额度覆盖：{target_user}")
                return True
            if len(parts) < 4:
                self.send("用法：/usage set <user_id> <daily_budget> [min_interval_seconds]")
                return True
            try:
                daily_budget = max(0, int(parts[3]))
                min_interval = max(0, int(parts[4])) if len(parts) >= 5 else self._int_config("default_min_interval_seconds", 0)
            except Exception:
                self.send("额度必须是整数：/usage set <user_id> <daily_budget> [min_interval_seconds]")
                return True
            limits[target_user] = {
                "daily_budget": daily_budget,
                "min_interval_seconds": min_interval,
            }
            self._save_wc_config()
            if self._access is not None:
                self._access.set_user_limit(target_user, daily_budget, min_interval)
            self.send(f"已设置用户额度：{target_user}\n每日 {self._fmt_limit(daily_budget)} 次，间隔 {self._fmt_limit(min_interval)} 秒")
            return True
        self.send("用法：/usage | /usage id | /usage set <user_id> <daily_budget> [min_interval_seconds] | /usage unset <user_id>")
        return True

    def _is_progress_message(self, message):
        text = (message or "").strip()
        if text in {"⏳", "✅"}:
            return True
        prefixes = (
            "📊 Finance ",
            "📈 Finance ",
            "📄 Finance ",
            "🐚 Shell:",
            "🚀 Async Shell:",
            "📖 Read:",
            "💾 Write:",
            "📤 Upload:",
            "🕒 Current time",
            "⏳ Waiting ",
        )
        return text.startswith(prefixes)

    def _daily_report_dir(self):
        configured = self.wc_config.get("daily_report_dir")
        if configured:
            return os.path.expanduser(str(configured))
        from xtrastock.config import Settings
        return str(Settings().path('paths.app') / "analysis" / "AI_Hardware" / "daily")

    def _daily_report_state_path(self):
        return str(ConfigManager.CONFIG_DIR / "wechat_daily_report_push_state.json")

    def _load_daily_report_state(self):
        path = self._daily_report_state_path()
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_daily_report_state(self, state):
        path = self._daily_report_state_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)

    def _daily_report_user_ids(self):
        raw_values = [
            self.wc_config.get("daily_report_user_ids"),
            self.wc_config.get("daily_report_user_id"),
            self.wc_config.get("default_user_id"),
        ]
        if self._bool_config("daily_report_to_authorized_user", True):
            raw_values.append(self.authorized_id)
        user_ids = []
        seen = set()
        for raw in raw_values:
            if not raw:
                continue
            values = raw if isinstance(raw, (list, tuple, set)) else [raw]
            for value in values:
                user_id = str(value or "").strip()
                if user_id and user_id not in seen:
                    user_ids.append(user_id)
                    seen.add(user_id)
        return user_ids

    def _daily_report_due(self):
        if not self._bool_config("daily_report_push_enabled", False):
            return False
        report_time = str(self.wc_config.get("daily_report_time", "09:05"))
        try:
            hour, minute = [int(x) for x in report_time.split(":", 1)]
        except Exception:
            hour, minute = 9, 5
        now = datetime.now()
        return (now.hour, now.minute) >= (hour, minute)

    def _find_daily_report(self):
        today = self._today_key()
        report_dir = self._daily_report_dir()
        exact = os.path.join(report_dir, f"{today}.md")
        if os.path.exists(exact):
            return exact
        try:
            candidates = [
                os.path.join(report_dir, name)
                for name in os.listdir(report_dir)
                if name.startswith(today) and name.endswith(".md")
            ]
        except Exception:
            return None
        return sorted(candidates)[0] if candidates else None

    def _split_text_chunks(self, text, limit):
        """Split outbound text without dropping content at the platform limit."""
        limit = max(300, int(limit or 900))
        chunks = []
        current = []
        current_len = 0
        for line in str(text or "").splitlines(keepends=True):
            pieces = [line]
            if len(line) > limit:
                pieces = [line[i : i + limit] for i in range(0, len(line), limit)]
            for piece in pieces:
                if current and current_len + len(piece) > limit:
                    chunks.append("".join(current))
                    current = [piece]
                    current_len = len(piece)
                else:
                    current.append(piece)
                    current_len += len(piece)
        if current:
            chunks.append("".join(current))
        return chunks or [""]

    def _daily_report_messages(self, path):
        max_chars = max(500, min(self._int_config("daily_report_max_chars", 3500), 3800))
        body_limit = max_chars - 120
        try:
            content = render_report_file(path)
        except Exception as e:
            self._log_event("daily_report_read_failed", {"path": str(path), "error": str(e)})
            return ["全市场日报暂时无法读取，请稍后重试。"]
        chunks = self._split_text_chunks(content, body_limit)
        total = len(chunks)
        messages = []
        for index, chunk in enumerate(chunks, start=1):
            if total == 1:
                title = f"全市场日报 {self._today_key()}"
            else:
                title = f"全市场日报 {self._today_key()} ({index}/{total})"
            messages.append(f"{title}\n\n{chunk}")
        return messages


    def _handle_daily_report_command(self, user_id):
        today = self._today_key()
        path = self._find_daily_report()
        if not path:
            self._log_event("daily_report_command_missing", {"date": today, "user_id": user_id, "dir": self._daily_report_dir()})
            self.send_messages_to_user(
                user_id,
                [f"今天的全市场日报还没有生成：{today}"],
            )
            return True

        messages = self._daily_report_messages(path)
        ok = self.send_messages_to_user(user_id, messages)
        self._log_event(
            "daily_report_command_sent" if ok else "daily_report_command_failed",
            {
                "date": today,
                "path": path,
                "user_id": user_id,
                "message_count": len(messages),
                "brief_sent": False,
            },
        )
        return True

    def _wechat_text_chunks(self, message):
        max_chars = max(300, min(self._int_config("max_text_chars", 900), 1800))
        return self._split_text_chunks(str(message or ""), max_chars)

    def _wechat_response_ok(self, response):
        if not isinstance(response, dict):
            return True
        ret = response.get("ret", 0)
        errcode = response.get("errcode", 0)
        return str(ret) in {"0", "None"} and str(errcode) in {"0", "None"}

    def _send_text_chunk_to_user(self, user_id, message):
        if not user_id or not self.token:
            return False
        import uuid
        try:
            msg_obj = {
                "from_user_id": "",
                "to_user_id": user_id,
                "client_id": str(uuid.uuid4()),
                "message_type": 2,
                "message_state": 2,
                "item_list": [{"type": 1, "text_item": {"text": f"⚡ {message}"}}],
            }
            context_token = self._context_tokens.get(user_id)
            if context_token:
                msg_obj["context_token"] = context_token
            response = self._api_post(
                "ilink/bot/sendmessage",
                {"msg": msg_obj, "base_info": {"channel_version": self.CHANNEL_VERSION}},
                timeout=15,
            )
            if not self._wechat_response_ok(response):
                raise RuntimeError(f"sendmessage returned error: {response}")
            self._log_event(
                "outbound",
                {
                    "user_id": user_id,
                    "text": message,
                    "chars": len(str(message or "")),
                    "response": response,
                },
            )
            return True
        except Exception as e:
            print(f"[!] WeChat send error: {e}")
            self._log_event("outbound_failed", {"user_id": user_id, "text": message, "error": str(e)})
            return False

    def send_messages_to_user(self, user_id, messages, delay_seconds=0.8):
        for message in messages:
            if self.config.get("tool_profile") == "finance_chat_readonly":
                message = render_finance_text(message)
            for chunk in self._wechat_text_chunks(message):
                if not self._send_text_chunk_to_user(user_id, chunk):
                    return False
                time.sleep(max(0, float(delay_seconds or 0)))
        return True

    def _start_outbox_worker(self):
        if self._outbox_worker is not None:
            return
        self._outbox_worker = OutboxWorker(
            "wechat",
            lambda target_type, target_id, payload: (
                target_type == "user"
                and self.send_messages_to_user(target_id, [str(payload.get("text") or "")])
            ),
            log=self._log_event,
        )
        self._outbox_worker.start()
        print("[*] WeChat finance outbox worker started.")

    def _start_daily_report_scheduler(self):
        if getattr(self, "_daily_report_thread_started", False):
            return
        if not self._bool_config("daily_report_push_enabled", False):
            return
        self._daily_report_thread_started = True
        threading.Thread(target=self._daily_report_loop, daemon=True).start()
        print("[*] WeChat daily report scheduler started.")

    def _daily_report_loop(self):
        while not self._stop_event.is_set():
            try:
                self._maybe_push_daily_report()
            except Exception as e:
                print(f"[!] WeChat daily report scheduler error: {e}")
            self._stop_event.wait(60)

    def _maybe_push_daily_report(self):
        if not self._daily_report_due():
            return
        today = self._today_key()
        state = self._load_daily_report_state()
        user_ids = self._daily_report_user_ids()
        if not user_ids:
            self._log_event("daily_report_skipped", {"reason": "missing_user_id", "date": today})
            return
        sent_users = state.setdefault("sent_users", {})
        pending_user_ids = [
            user_id
            for user_id in user_ids
            if sent_users.get(user_id, {}).get("last_sent_date") != today
            and sent_users.get(user_id, {}).get("last_failed_date") != today
        ]
        if not pending_user_ids:
            return
        path = self._find_daily_report()
        if not path:
            self._log_event("daily_report_waiting", {"reason": "missing_report", "date": today, "dir": self._daily_report_dir()})
            return
        messages = self._daily_report_messages(path)
        for user_id in pending_user_ids:
            ok = self.send_messages_to_user(user_id, messages)
            if ok:
                sent_users[user_id] = {
                    "last_sent_date": today,
                    "last_sent_path": path,
                    "sent_at": self._now_iso(),
                    "message_count": len(messages),
                    "brief_sent": False,
                }
                state.update(
                    {
                        "last_sent_date": today,
                        "last_sent_path": path,
                        "sent_at": self._now_iso(),
                        "user_id": user_id,
                        "message_count": len(messages),
                        "brief_sent": False,
                    }
                )
                self._save_daily_report_state(state)
                self._log_event("daily_report_pushed", {"date": today, "path": path, "user_id": user_id, "message_count": len(messages), "brief_sent": False})
            else:
                sent_users[user_id] = {
                    "last_failed_date": today,
                    "last_failed_path": path,
                    "failed_at": self._now_iso(),
                    "message_count": len(messages),
                    "brief_sent": False,
                }
                self._save_daily_report_state(state)
                self._log_event(
                    "daily_report_failed",
                    {
                        "date": today,
                        "path": path,
                        "user_id": user_id,
                        "message_count": len(messages),
                        "brief_sent": False,
                    },
                )

    # ------------------------------------------------------------------
    # QR login
    # ------------------------------------------------------------------

    def _login_qr(self):
        """Interactive QR login. Blocks until confirmed, expired limit, or timeout."""
        import requests as req
        base = self.base_url
        qr_url_endpoint = f"{base}/ilink/bot/get_bot_qrcode?bot_type={self.BOT_TYPE}"

        def _fetch_qr():
            r = req.get(qr_url_endpoint, timeout=15)
            r.raise_for_status()
            d = r.json()
            return d["qrcode"], d["qrcode_img_content"]

        print("\n[🔐] WeChat QR login required")
        try:
            qrcode, qrcode_url = _fetch_qr()
        except Exception as e:
            print(f"[❌] Failed to fetch WeChat QR code: {e}")
            return False

        def _display_qr(url):
            import qrcode as qr_lib
            qr = qr_lib.QRCode(border=1)
            qr.add_data(url)
            qr.make(fit=True)
            print()
            qr.print_ascii(invert=True)
            print(f"[*] 如二维码显示异常，请用浏览器打开链接扫码: {url}")

        _display_qr(qrcode_url)

        deadline = time.time() + 480
        refresh_count = 0
        max_refreshes = 3
        scanned_printed = False

        while time.time() < deadline:
            try:
                from urllib.parse import quote as _quote
                status_url = f"{base}/ilink/bot/get_qrcode_status?qrcode={_quote(qrcode)}"
                r = req.get(status_url, headers={"iLink-App-ClientVersion": "1"},
                            timeout=self.LONG_POLL_TIMEOUT_S)
                r.raise_for_status()
                d = r.json()
                status = d.get("status", "wait")

                if status == "wait":
                    pass
                elif status == "scaned":
                    if not scanned_printed:
                        print("\n[👀] QR scanned — please confirm in WeChat...")
                        scanned_printed = True
                elif status == "expired":
                    refresh_count += 1
                    if refresh_count > max_refreshes:
                        print("\n[❌] QR code expired too many times. Login aborted.")
                        return False
                    print(f"\n[⏳] QR expired, refreshing ({refresh_count}/{max_refreshes})...")
                    try:
                        qrcode, qrcode_url = _fetch_qr()
                        scanned_printed = False
                        _display_qr(qrcode_url)
                    except Exception as e:
                        print(f"[❌] Failed to refresh QR code: {e}")
                        return False
                elif status == "confirmed":
                    if not d.get("ilink_bot_id"):
                        print("[❌] Login confirmed but ilink_bot_id missing.")
                        return False
                    self.token = d.get("bot_token", "")
                    self.base_url = (d.get("baseurl") or base).rstrip("/")
                    self.authorized_id = d.get("ilink_user_id", "")
                    account_id = d.get("ilink_bot_id", "")
                    self.wc_config["token"] = self.token
                    self.wc_config["base_url"] = self.base_url
                    self.wc_config["authorized_id"] = self.authorized_id
                    self.wc_config["account_id"] = account_id
                    self._save_wc_config()
                    self._ensure_authorized_admin()
                    print(f"\n[✅] WeChat login successful! Bot: {account_id}, User: {self.authorized_id}")
                    return True
            except req.exceptions.Timeout:
                pass   # normal for long-poll
            except Exception as e:
                print(f"[!] WeChat QR poll error: {e}")
                time.sleep(2)

        print("\n[❌] WeChat login timed out.")
        return False

    # ------------------------------------------------------------------
    # CDN download helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_aes_key(aes_key_b64):
        """Mirror of parseAesKey in pic-decrypt.ts.
        Accepts base64(raw 16 bytes) or base64(hex string of 16 bytes)."""
        import re
        decoded = base64.b64decode(aes_key_b64)
        if len(decoded) == 16:
            return decoded
        if len(decoded) == 32 and re.fullmatch(b"[0-9a-fA-F]{32}", decoded):
            return bytes.fromhex(decoded.decode("ascii"))
        raise ValueError(f"aes_key must decode to 16 raw bytes or 32-char hex string, "
                         f"got {len(decoded)} bytes")

    @staticmethod
    def _decrypt_aes_ecb(ciphertext, key):
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives import padding as crypto_padding
        dec = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
        padded = dec.update(ciphertext) + dec.finalize()
        unpadder = crypto_padding.PKCS7(128).unpadder()
        return unpadder.update(padded) + unpadder.finalize()

    def _download_and_decrypt_cdn(self, encrypt_query_param, aes_key_b64):
        import requests as req
        from urllib.parse import quote
        cdn_base = self.wc_config.get("cdn_base_url", self.CDN_BASE_URL)
        url = f"{cdn_base}/download?encrypted_query_param={quote(encrypt_query_param)}"
        r = req.get(url, timeout=60)
        r.raise_for_status()
        key = self._parse_aes_key(aes_key_b64)
        return self._decrypt_aes_ecb(r.content, key)

    # ------------------------------------------------------------------
    # Inbound message handling
    # ------------------------------------------------------------------

    def _handle_message(self, msg):
        from_user_id = msg.get("from_user_id", "")
        context_token = msg.get("context_token")
        if context_token:
            self._context_tokens[from_user_id] = context_token

        if self.authorized_id and from_user_id != self.authorized_id:
            return

        for item in msg.get("item_list", []):
            item_type = item.get("type", 0)
            if item_type == 1:   # TEXT
                text = item.get("text_item", {}).get("text", "").strip()
                ref_msg = item.get("ref_msg")
                if ref_msg:
                    parts = []
                    if ref_msg.get("title"):
                        parts.append(ref_msg["title"])
                    ref_item = ref_msg.get("message_item")
                    if ref_item and ref_item.get("type") == 1:
                        ref_text = ref_item.get("text_item", {}).get("text", "").strip()
                        if ref_text:
                            parts.append(ref_text)
                    if parts:
                        text = f"[引用: {' | '.join(parts)}]\n{text}"
                if text:
                    self._log_event("inbound", {"user_id": from_user_id, "text": text})
                    allowed, reason = self._is_allowed(from_user_id)
                    if not allowed:
                        self._log_event("blocked", {"user_id": from_user_id, "reason": reason})
                        return
                    if self._is_usage_command(text):
                        self._log_event("usage_command", {"user_id": from_user_id})
                        self._handle_usage_command(from_user_id, text)
                        return
                    if self._is_daily_report_command(text):
                        self._log_event("daily_report_command", {"user_id": from_user_id, "text": text})
                        threading.Thread(
                            target=self._handle_daily_report_command,
                            args=(from_user_id,),
                            daemon=True,
                        ).start()
                        return
                    ok, reason = self._check_and_record_budget(from_user_id)
                    if not ok:
                        self._log_event("rate_limited", {"user_id": from_user_id, "reason": reason})
                        if self._bool_config("notify_on_limit", True):
                            self.send(f"请求已被限制：{reason}")
                        return
                    if self.callback:
                        print(f"📩 WeChat: {text}")
                        payload = {
                            "__mmclaw_text": text,
                            "__mmclaw_session_key": f"wechat:{from_user_id or 'unknown'}",
                        }
                        threading.Thread(target=self.callback, args=(payload,), daemon=True).start()
                return
            elif item_type == 3:  # VOICE — use transcribed text if available
                voice_text = item.get("voice_item", {}).get("text", "").strip()
                if voice_text and self.callback:
                    print(f"📩 WeChat: [Voice] {voice_text}")
                    threading.Thread(target=self.callback, args=(voice_text,), daemon=True).start()
                return
            elif item_type == 2:  # IMAGE
                media = item.get("image_item", {}).get("media", {})
                eqp = media.get("encrypt_query_param")
                aes_key = media.get("aes_key") or item.get("image_item", {}).get("aeskey")
                print("📩 WeChat: [Image]")
                if eqp and aes_key and self.callback:
                    def _send_image(eqp=eqp, aes_key=aes_key):
                        try:
                            image_bytes = self._download_and_decrypt_cdn(eqp, aes_key)
                            content = prepare_image_content(image_bytes, "这张图片里有什么？")
                            self.callback(content)
                        except Exception as e:
                            print(f"[!] WeChat image download error: {e}")
                            self.callback("[Image received]")
                    threading.Thread(target=_send_image, daemon=True).start()
                elif self.callback:
                    threading.Thread(target=self.callback,
                                     args=("[Image received]",), daemon=True).start()
                return
            elif item_type == 4:  # FILE
                file_item = item.get("file_item", {})
                file_name = file_item.get("file_name", "file")
                media = file_item.get("media", {})
                eqp = media.get("encrypt_query_param")
                aes_key = media.get("aes_key")
                print(f"📩 WeChat: [File: {file_name}]")
                if eqp and aes_key and self.file_saver and self.callback:
                    def _send_file(file_name=file_name, eqp=eqp, aes_key=aes_key):
                        try:
                            file_bytes = self._download_and_decrypt_cdn(eqp, aes_key)
                            file_path = self.file_saver(file_name, file_bytes)
                            self.callback(f"[Uploaded file: {file_path}]")
                        except Exception as e:
                            print(f"[!] WeChat file download error: {e}")
                            self.callback(f"[File received: {file_name}]")
                    threading.Thread(target=_send_file, daemon=True).start()
                elif self.callback:
                    threading.Thread(target=self.callback,
                                     args=(f"[File received: {file_name}]",), daemon=True).start()
                return

    # ------------------------------------------------------------------
    # Connector interface
    # ------------------------------------------------------------------

    def listen(self, callback, stop_on_auth=False):
        self.callback = callback
        print("\n--- MMClaw Kernel Active (WeChat Mode) ---")

        if not self.token:
            if not self._login_qr():
                return
        elif stop_on_auth:
            print(f"\n[✅] WeChat identity already verified: {self.authorized_id}")
            return

        if stop_on_auth:
            return

        self._ensure_authorized_admin()
        self._start_outbox_worker()
        self._start_daily_report_scheduler()
        print(f"[*] Listening for WeChat messages from: {self.authorized_id or '(any)'}")

        def _poll_loop():
            consecutive_failures = 0
            next_timeout_s = self.LONG_POLL_TIMEOUT_S
            while not self._stop_event.is_set():
                try:
                    body = {
                        "get_updates_buf": self._get_updates_buf,
                        "base_info": {"channel_version": self.CHANNEL_VERSION},
                    }
                    data = self._api_post("ilink/bot/getupdates", body,
                                          timeout=next_timeout_s + 5)

                    ret = data.get("ret", 0)
                    errcode = data.get("errcode", 0)
                    if ret != 0 or errcode != 0:
                        if errcode == -14 or ret == -14:
                            print("[!] WeChat session expired (errcode -14), pausing 1 hour...")
                            consecutive_failures = 0
                            self._stop_event.wait(3600)
                            continue
                        consecutive_failures += 1
                        print(f"[!] WeChat getUpdates error: ret={ret} errcode={errcode} "
                              f"errmsg={data.get('errmsg', '')}")
                        delay = 30 if consecutive_failures >= self.MAX_CONSECUTIVE_FAILURES else 2
                        if consecutive_failures >= self.MAX_CONSECUTIVE_FAILURES:
                            consecutive_failures = 0
                        self._stop_event.wait(delay)
                        continue

                    consecutive_failures = 0
                    server_timeout_ms = data.get("longpolling_timeout_ms", 0)
                    if server_timeout_ms and server_timeout_ms > 0:
                        next_timeout_s = server_timeout_ms / 1000
                    new_buf = data.get("get_updates_buf", "")
                    if new_buf and new_buf != self._get_updates_buf:
                        self._get_updates_buf = new_buf
                        self.wc_config["get_updates_buf"] = new_buf
                        self._save_wc_config()

                    for msg in data.get("msgs", []):
                        self._handle_message(msg)

                except Exception as e:
                    # requests.Timeout is also caught here — that's normal for long-poll
                    import requests
                    if isinstance(e, requests.exceptions.Timeout):
                        continue
                    consecutive_failures += 1
                    print(f"[!] WeChat poll error: {e}")
                    delay = 30 if consecutive_failures >= self.MAX_CONSECUTIVE_FAILURES else 2
                    if consecutive_failures >= self.MAX_CONSECUTIVE_FAILURES:
                        consecutive_failures = 0
                    self._stop_event.wait(delay)

        threading.Thread(target=_poll_loop, daemon=True).start()
        self._stop_event.wait()

    def start_typing(self):
        self._typing = True
        user_id = self.authorized_id
        if not user_id:
            return

        def _type_loop():
            ticket = None
            while self._typing:
                try:
                    if not ticket:
                        cfg_data = self._api_post(
                            "ilink/bot/getconfig",
                            {"ilink_user_id": user_id,
                             "context_token": self._context_tokens.get(user_id, ""),
                             "base_info": {"channel_version": self.CHANNEL_VERSION}},
                            timeout=10,
                        )
                        ticket = cfg_data.get("typing_ticket")
                    if ticket:
                        self._api_post(
                            "ilink/bot/sendtyping",
                            {"ilink_user_id": user_id, "typing_ticket": ticket,
                             "status": 1,
                             "base_info": {"channel_version": self.CHANNEL_VERSION}},
                            timeout=10,
                        )
                except Exception:
                    ticket = None
                self._stop_event.wait(4)

        threading.Thread(target=_type_loop, daemon=True).start()

    def stop_typing(self):
        self._typing = False
        user_id = self.authorized_id
        if not user_id or not self.token:
            return
        try:
            cfg_data = self._api_post(
                "ilink/bot/getconfig",
                {"ilink_user_id": user_id,
                 "context_token": self._context_tokens.get(user_id, ""),
                 "base_info": {"channel_version": self.CHANNEL_VERSION}},
                timeout=10,
            )
            ticket = cfg_data.get("typing_ticket")
            if ticket:
                self._api_post(
                    "ilink/bot/sendtyping",
                    {"ilink_user_id": user_id, "typing_ticket": ticket,
                     "status": 2,
                     "base_info": {"channel_version": self.CHANNEL_VERSION}},
                    timeout=10,
                )
        except Exception:
            pass

    def send(self, message):
        if not self.authorized_id or not self.token:
            return
        if self._bool_config("suppress_progress_messages", True) and self._is_progress_message(message):
            self._log_event("suppressed_progress", {"user_id": self.authorized_id, "text": message})
            return
        self.send_messages_to_user(self.authorized_id, [message])

    # ------------------------------------------------------------------
    # CDN upload helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _aes_ecb_padded_size(plaintext_size):
        import math
        return math.ceil((plaintext_size + 1) / 16) * 16

    @staticmethod
    def _encrypt_aes_ecb(plaintext, key):
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives import padding as crypto_padding
        padder = crypto_padding.PKCS7(128).padder()
        padded = padder.update(plaintext) + padder.finalize()
        enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
        return enc.update(padded) + enc.finalize()

    def _upload_to_cdn(self, file_path, to_user_id, media_type):
        """Encrypt file and upload to WeChat CDN. Returns upload info dict."""
        import hashlib
        import requests as req
        from urllib.parse import quote

        with open(file_path, "rb") as f:
            plaintext = f.read()

        rawsize = len(plaintext)
        rawfilemd5 = hashlib.md5(plaintext).hexdigest()
        aeskey = os.urandom(16)
        filekey = os.urandom(16).hex()
        filesize = self._aes_ecb_padded_size(rawsize)

        upload_resp = self._api_post("ilink/bot/getuploadurl", {
            "filekey": filekey,
            "media_type": media_type,
            "to_user_id": to_user_id,
            "rawsize": rawsize,
            "rawfilemd5": rawfilemd5,
            "filesize": filesize,
            "no_need_thumb": True,
            "aeskey": aeskey.hex(),
            "base_info": {"channel_version": self.CHANNEL_VERSION},
        })

        upload_param = upload_resp.get("upload_param")
        if not upload_param:
            raise RuntimeError(f"getuploadurl returned no upload_param: {upload_resp}")

        ciphertext = self._encrypt_aes_ecb(plaintext, aeskey)
        cdn_base = self.wc_config.get("cdn_base_url", self.CDN_BASE_URL)
        cdn_url = (f"{cdn_base}/upload"
                   f"?encrypted_query_param={quote(upload_param)}"
                   f"&filekey={quote(filekey)}")

        download_param = None
        for attempt in range(1, 4):
            r = req.post(cdn_url, data=ciphertext,
                         headers={"Content-Type": "application/octet-stream"},
                         timeout=120)
            if 400 <= r.status_code < 500:
                raise RuntimeError(f"CDN upload client error {r.status_code}: "
                                   f"{r.headers.get('x-error-message', r.text)}")
            if r.status_code != 200:
                if attempt < 3:
                    continue
                raise RuntimeError(f"CDN upload failed after 3 attempts: {r.status_code}")
            download_param = r.headers.get("x-encrypted-param")
            if download_param:
                break
            if attempt == 3:
                raise RuntimeError("CDN upload response missing x-encrypted-param header")

        return {
            "download_encrypted_query_param": download_param,
            "aeskey": aeskey,
            "file_size": rawsize,
            "file_size_ciphertext": filesize,
            "filekey": filekey,
        }

    def _send_media_message(self, to_user_id, context_token, item):
        import uuid
        msg_obj = {
            "from_user_id": "",
            "to_user_id": to_user_id,
            "client_id": str(uuid.uuid4()),
            "message_type": 2,
            "message_state": 2,
            "item_list": [item],
        }
        if context_token:
            msg_obj["context_token"] = context_token
        self._api_post(
            "ilink/bot/sendmessage",
            {"msg": msg_obj, "base_info": {"channel_version": self.CHANNEL_VERSION}},
            timeout=15,
        )

    def send_file(self, path):
        if not self.authorized_id or not self.token:
            return
        import mimetypes
        full_path = os.path.abspath(os.path.expanduser(path))
        if not os.path.exists(full_path):
            self.send(f"[File not found: {path}]")
            return

        mime = mimetypes.guess_type(full_path)[0] or "application/octet-stream"
        context_token = self._context_tokens.get(self.authorized_id)

        def _aes_key_b64(raw_key):
            # WeChat expects base64(hex_string_of_key) — NOT base64(raw_bytes).
            # parseAesKey on the client side: if decoded length == 32 and all hex chars,
            # it re-parses as hex to recover the 16-byte key. See pic-decrypt.ts.
            return base64.b64encode(raw_key.hex().encode("ascii")).decode()

        try:
            if mime.startswith("image/"):
                info = self._upload_to_cdn(full_path, self.authorized_id, self._MEDIA_IMAGE)
                item = {
                    "type": self._ITEM_IMAGE,
                    "image_item": {
                        "media": {
                            "encrypt_query_param": info["download_encrypted_query_param"],
                            "aes_key": _aes_key_b64(info["aeskey"]),
                            "encrypt_type": 1,
                        },
                        "mid_size": info["file_size_ciphertext"],
                    },
                }
            elif mime.startswith("video/"):
                info = self._upload_to_cdn(full_path, self.authorized_id, self._MEDIA_VIDEO)
                item = {
                    "type": self._ITEM_VIDEO,
                    "video_item": {
                        "media": {
                            "encrypt_query_param": info["download_encrypted_query_param"],
                            "aes_key": _aes_key_b64(info["aeskey"]),
                            "encrypt_type": 1,
                        },
                        "video_size": info["file_size_ciphertext"],
                    },
                }
            else:
                info = self._upload_to_cdn(full_path, self.authorized_id, self._MEDIA_FILE)
                item = {
                    "type": self._ITEM_FILE,
                    "file_item": {
                        "media": {
                            "encrypt_query_param": info["download_encrypted_query_param"],
                            "aes_key": _aes_key_b64(info["aeskey"]),
                            "encrypt_type": 1,
                        },
                        "file_name": os.path.basename(full_path),
                        "len": str(info["file_size"]),
                    },
                }
            self._send_media_message(self.authorized_id, context_token, item)
        except Exception as e:
            print(f"[!] WeChat send_file error: {e}")
            self.send(f"[File upload failed: {e}]")


class QQBotConnector(object):
    def __init__(self, app_id, app_secret, config=None):
        try:
            import botpy
        except ImportError:
            raise ImportError("botpy 未安装，请运行: pip install qq-botpy")

        self.app_id = app_id
        self.app_secret = app_secret
        self.config = config if config else ConfigManager.load()
        self.qq_config = self.config.get("connectors", {}).get("qqbot", {})
        self.callback = None
        self._client = None
        self._loop = None
        self._api = None
        self._last_message = None   # ('c2c'|'group', message_obj)
        self._active_target = None   # ('c2c', openid) or ('group', group_openid)
        self._msg_seq = {}          # Per-target message sequence counter
        self._seen_message_ids = {}
        self._lock = threading.Lock()
        self._instance_lock_handle = None
        self._daily_report_thread_started = False
        self._outbox_worker = None
        try:
            self._access = AccessController("qq", self.qq_config)
        except Exception as exc:
            self._access = None
            print(f"[!] QQ database access policy fallback: {type(exc).__name__}: {exc}")
        self._file_server = None
        self._file_server_thread = None

    def _current_target_kind(self):
        if self._active_target:
            return self._active_target[0]
        if self._last_message:
            return self._last_message[0]
        return None

    def _is_progress_message(self, message):
        text = (message or "").strip()
        if text in {"⏳", "✅"}:
            return True
        prefixes = (
            "📊 Finance ",
            "📈 Finance ",
            "🐚 Shell:",
            "🚀 Async Shell:",
            "📖 Read:",
            "💾 Write:",
            "📤 Upload:",
            "❌ QQ Bot 暂不支持发送文件。",
        )
        return text.startswith(prefixes)

    def _group_mention_for_message(self, message):
        author = getattr(message, "author", None)
        member_openid = getattr(author, "member_openid", None)
        if not member_openid:
            return ""
        return f"<@{member_openid}> "

    def _is_expired_reply_error(self, error):
        text = str(error)
        return (
            "40034005" in text
            or "40034128" in text
            or "304103" in text
            or "msg_id已过期" in text
            or "msgid已经过期" in text
            or "回复消息msg_id已过期" in text
            or "被动回复时间或者次数超过限制" in text
            or "message id expired" in text.lower()
            or "passive reply" in text.lower()
        )

    def _list_config(self, key):
        value = self.qq_config.get(key, [])
        if isinstance(value, str):
            value = [value]
        return {str(x) for x in value if str(x)}

    def _bool_config(self, key, default=False):
        return bool(self.qq_config.get(key, default))

    def _int_config(self, key, default=0):
        try:
            return int(self.qq_config.get(key, default) or 0)
        except Exception:
            return default

    def _now_iso(self):
        return datetime.now(timezone.utc).isoformat()

    def _today_key(self):
        return datetime.now().strftime("%Y-%m-%d")

    def _daily_report_dir(self):
        configured = self.qq_config.get("daily_report_dir")
        if configured:
            return os.path.expanduser(str(configured))
        return str(ConfigManager.CONFIG_DIR.parent / "analysis" / "AI_Hardware" / "daily")

    def _generated_files_dir(self):
        configured = self.qq_config.get("file_server_dir")
        if configured:
            return os.path.expanduser(str(configured))
        return str(ConfigManager.CONFIG_DIR / "generated")

    def _guess_local_ip(self):
        import socket
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.connect(("8.8.8.8", 80))
            ip = sock.getsockname()[0]
            sock.close()
            return ip
        except Exception:
            return "127.0.0.1"

    def _ensure_file_server(self):
        if self._file_server:
            return True
        import functools
        import http.server
        import socketserver

        root = Path(self._generated_files_dir()).resolve()
        root.mkdir(parents=True, exist_ok=True)
        port = self._int_config("file_server_port", 8788)

        class QuietHandler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, format, *args):
                return

        handler = functools.partial(QuietHandler, directory=str(root))
        try:
            socketserver.ThreadingTCPServer.allow_reuse_address = True
            server = socketserver.ThreadingTCPServer(("0.0.0.0", port), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self._file_server = server
            self._file_server_thread = thread
            print(f"[*] QQ file server serving {root} on port {port}")
            self._log_event("file_server_started", {"root": str(root), "port": port})
            return True
        except Exception as e:
            print(f"[!] QQ file server start failed: {e}")
            self._log_event("file_server_failed", {"root": str(root), "port": port, "error": str(e)})
            return False

    def _file_url_for_path(self, path):
        from urllib.parse import quote

        root = Path(self._generated_files_dir()).resolve()
        full_path = Path(os.path.expanduser(path)).resolve()
        try:
            rel_path = full_path.relative_to(root)
        except Exception:
            raise ValueError(f"QQ file upload only allows files under generated dir: {root}")

        configured_base = self.qq_config.get("file_public_base_url")
        if not self._ensure_file_server():
            raise RuntimeError("local file server could not be started")
        if configured_base:
            base_url = str(configured_base).rstrip("/")
        else:
            base_url = f"http://{self._guess_local_ip()}:{self._int_config('file_server_port', 8788)}"
        return base_url + "/" + quote(str(rel_path).replace(os.sep, "/"))

    def _stage_file_for_upload(self, path):
        root = Path(self._generated_files_dir()).resolve()
        full_path = Path(os.path.expanduser(path)).resolve()
        try:
            full_path.relative_to(root)
            return str(full_path)
        except Exception:
            pass

        uploads_dir = root / "uploads"
        uploads_dir.mkdir(parents=True, exist_ok=True)
        stem = re.sub(r"[^0-9A-Za-z._-]+", "_", full_path.stem).strip("._") or "upload"
        suffix = full_path.suffix.lower() or ".png"
        staged = uploads_dir / f"{stem}_{int(time.time())}{suffix}"
        shutil.copy2(full_path, staged)
        self._log_event(
            "file_upload_staged",
            {
                "source": str(full_path),
                "staged": str(staged),
            },
        )
        return str(staged)

    def _daily_report_state_path(self):
        return str(ConfigManager.CONFIG_DIR / "daily_report_push_state.json")

    def _load_daily_report_state(self):
        path = self._daily_report_state_path()
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_daily_report_state(self, state):
        path = self._daily_report_state_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)

    def _log_event(self, event_type, payload):
        if not self._bool_config("log_messages", True):
            return
        try:
            log_dir = self.qq_config.get("log_dir") or str(ConfigManager.CONFIG_DIR / "qq_logs")
            os.makedirs(os.path.expanduser(log_dir), exist_ok=True)
            path = os.path.join(os.path.expanduser(log_dir), f"{self._today_key()}.jsonl")
            record = {"ts": self._now_iso(), "event_type": event_type, **payload}
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            print(f"[!] QQ Bot Log Error: {e}")

    def _usage_path(self):
        usage_dir = self.qq_config.get("usage_dir") or str(ConfigManager.CONFIG_DIR / "qq_usage")
        os.makedirs(os.path.expanduser(usage_dir), exist_ok=True)
        return os.path.join(os.path.expanduser(usage_dir), f"{self._today_key()}.json")

    def _load_usage(self):
        path = self._usage_path()
        if not os.path.exists(path):
            return {"date": self._today_key(), "members": {}, "groups": {}}
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"date": self._today_key(), "members": {}, "groups": {}}

    def _save_usage(self, usage):
        path = self._usage_path()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(usage, f, ensure_ascii=False, indent=2)

    def _member_limit(self, user_openid):
        if self._access is not None:
            try:
                policy = self._access.policy("user", user_openid)
                return {
                    "daily_budget": policy["daily_budget"],
                    "min_interval_seconds": policy["min_interval_seconds"],
                }
            except Exception:
                pass
        if self._is_admin_user(user_openid):
            return {
                "daily_budget": 0,
                "min_interval_seconds": 0,
            }
        limits = self.qq_config.get("member_limits", {}) or {}
        cfg = limits.get(user_openid, {}) if isinstance(limits, dict) else {}
        return {
            "daily_budget": int(cfg.get("daily_budget", self._int_config("default_daily_budget_per_member", 0)) or 0),
            "min_interval_seconds": int(cfg.get("min_interval_seconds", self._int_config("default_min_interval_seconds", 0)) or 0),
        }

    def _group_daily_budget(self):
        if self._access is not None:
            try:
                return self._access.policy("group", None)["daily_budget"]
            except Exception:
                pass
        return self._int_config("default_daily_budget_per_group", 0)

    def _is_admin_user(self, user_openid):
        if self._access is not None:
            try:
                return self._access.is_admin(user_openid)
            except Exception:
                pass
        return bool(user_openid and user_openid in self._list_config("admin_users"))

    def _identity(self, kind, message):
        if kind == "group":
            user_openid = getattr(getattr(message, "author", None), "member_openid", None)
            group_openid = getattr(message, "group_openid", None)
        else:
            user_openid = getattr(getattr(message, "author", None), "user_openid", None)
            group_openid = None
        return str(user_openid or ""), str(group_openid or "")

    def _is_allowed(self, kind, user_openid, group_openid):
        if self._access is not None:
            try:
                return self._access.allowed(user_openid, group_openid if kind == "group" else None)
            except Exception:
                pass
        allowed_users = self._list_config("allowed_users")
        blocked_users = self._list_config("blocked_users")
        allowed_groups = self._list_config("allowed_groups")
        blocked_groups = self._list_config("blocked_groups")

        if user_openid and user_openid in blocked_users:
            return False, "blocked_user"
        if group_openid and group_openid in blocked_groups:
            return False, "blocked_group"
        if allowed_users and user_openid not in allowed_users:
            return False, "user_not_allowed"
        if kind == "group" and allowed_groups and group_openid not in allowed_groups:
            return False, "group_not_allowed"
        return True, ""

    def _check_and_record_budget(self, kind, user_openid, group_openid):
        if self._access is not None:
            try:
                return self._access.consume(user_openid, group_openid if kind == "group" else None)
            except Exception as exc:
                self._log_event("access_database_fallback", {"operation": "consume", "error": str(exc)})
        with self._lock:
            now = time.time()
            usage = self._load_usage()
            member = usage.setdefault("members", {}).setdefault(user_openid or "unknown", {"requests": 0, "last_at": 0})
            limit = self._member_limit(user_openid)
            if limit["daily_budget"] > 0 and member.get("requests", 0) >= limit["daily_budget"]:
                return False, "member_daily_budget_exceeded"
            if limit["min_interval_seconds"] > 0 and now - float(member.get("last_at", 0) or 0) < limit["min_interval_seconds"]:
                return False, "member_rate_limited"

            if kind == "group" and group_openid:
                group = usage.setdefault("groups", {}).setdefault(group_openid, {"requests": 0, "last_at": 0})
                group_budget = self._group_daily_budget()
                if group_budget > 0 and group.get("requests", 0) >= group_budget:
                    return False, "group_daily_budget_exceeded"
                group["requests"] = group.get("requests", 0) + 1
                group["last_at"] = now

            member["requests"] = member.get("requests", 0) + 1
            member["last_at"] = now
            self._save_usage(usage)
        return True, ""

    def _is_duplicate_message(self, message):
        message_id = getattr(message, "id", None)
        if not message_id:
            return False
        now = time.time()
        with self._lock:
            for key, seen_at in list(self._seen_message_ids.items()):
                if now - seen_at > 120:
                    self._seen_message_ids.pop(key, None)
            if message_id in self._seen_message_ids:
                return True
            self._seen_message_ids[message_id] = now
        return False

    def _should_respond_to_group(self, text):
        mode = str(self.qq_config.get("respond_in_groups", "mention")).lower()
        if mode in ("none", "off", "false"):
            return False
        if mode in ("all", "always"):
            return True
        prefixes = self.qq_config.get("group_command_prefixes", ["/", "！", "!"])
        if isinstance(prefixes, str):
            prefixes = [prefixes]
        # on_group_at_message_create only delivers @ messages; prefixes let users avoid accidental calls if desired.
        require_prefix = self._bool_config("group_require_command_prefix", False)
        return not require_prefix or any((text or "").strip().startswith(p) for p in prefixes)

    def _command_text(self, text):
        import re
        return re.sub(r"<@[^>]+>\s*", "", text or "").strip()

    def _is_usage_command(self, text):
        cmd = self._command_text(text).strip().lower()
        return cmd in {"/usage", "usage", "/quota", "quota", "额度", "查询额度"} or cmd.startswith("/usage ")

    def _fmt_limit(self, value):
        return "不限" if int(value or 0) <= 0 else str(int(value))

    def _fmt_remaining(self, used, limit):
        limit = int(limit or 0)
        if limit <= 0:
            return "不限"
        return str(max(0, limit - int(used or 0)))

    def _fmt_time(self, ts):
        try:
            if not ts:
                return "无"
            if isinstance(ts, datetime):
                return ts.astimezone().strftime("%Y-%m-%d %H:%M:%S")
            return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return "无"

    def _usage_report(self, kind, user_openid, group_openid):
        if self._access is not None:
            try:
                member = self._access.usage("user", user_openid)
                lines = [
                    f"今日额度 {self._today_key()}", f"用户ID: {user_openid or 'unknown'}",
                    f"用户请求: {member['request_count']}/{self._fmt_limit(member['daily_budget'])}",
                    f"用户剩余: {self._fmt_remaining(member['request_count'], member['daily_budget'])}",
                    f"最小间隔: {self._fmt_limit(member['min_interval_seconds'])} 秒",
                    f"上次请求: {self._fmt_time(member['last_request_at'])}",
                ]
                if kind == "group":
                    group = self._access.usage("group", group_openid)
                    lines.extend([
                        f"群ID: {group_openid or 'unknown'}",
                        f"群请求: {group['request_count']}/{self._fmt_limit(group['daily_budget'])}",
                        f"群剩余: {self._fmt_remaining(group['request_count'], group['daily_budget'])}",
                    ])
                return "\n".join(lines)
            except Exception:
                pass
        usage = self._load_usage()
        member = usage.get("members", {}).get(user_openid or "unknown", {})
        group = usage.get("groups", {}).get(group_openid or "", {}) if group_openid else {}
        limit = self._member_limit(user_openid)
        group_budget = self._group_daily_budget()
        lines = [
            f"今日额度 {self._today_key()}",
            f"用户ID: {user_openid or 'unknown'}",
            f"用户请求: {int(member.get('requests', 0) or 0)}/{self._fmt_limit(limit['daily_budget'])}",
            f"用户剩余: {self._fmt_remaining(member.get('requests', 0), limit['daily_budget'])}",
            f"最小间隔: {self._fmt_limit(limit['min_interval_seconds'])} 秒",
            f"上次请求: {self._fmt_time(member.get('last_at', 0))}",
        ]
        if kind == "group":
            lines.extend(
                [
                    f"群ID: {group_openid or 'unknown'}",
                    f"群请求: {int(group.get('requests', 0) or 0)}/{self._fmt_limit(group_budget)}",
                    f"群剩余: {self._fmt_remaining(group.get('requests', 0), group_budget)}",
                ]
            )
        return "\n".join(lines)

    def _handle_usage_command(self, kind, user_openid, group_openid, text):
        cmd = self._command_text(text).strip()
        parts = cmd.split()
        if len(parts) == 1:
            self.send(self._usage_report(kind, user_openid, group_openid))
            return True
        action = parts[1].lower()
        if action == "id":
            self.send(f"用户ID: {user_openid or 'unknown'}\n群ID: {group_openid or '无'}")
            return True
        if action in {"set", "unset"}:
            if not self._is_admin_user(user_openid):
                self.send("无权限：只有 admin_users 可以修改额度。")
                return True
            if len(parts) < 3:
                self.send("用法：/usage set <user_openid> <daily_budget> [min_interval_seconds]\n或：/usage unset <user_openid>")
                return True
            target_user = parts[2]
            limits = self.qq_config.setdefault("member_limits", {})
            if action == "unset":
                limits.pop(target_user, None)
                ConfigManager.save(self.config)
                if self._access is not None:
                    self._access.unset_user_limit(target_user)
                self.send(f"已移除用户额度覆盖：{target_user}")
                return True
            if len(parts) < 4:
                self.send("用法：/usage set <user_openid> <daily_budget> [min_interval_seconds]")
                return True
            try:
                daily_budget = max(0, int(parts[3]))
                min_interval = max(0, int(parts[4])) if len(parts) >= 5 else self._int_config("default_min_interval_seconds", 0)
            except Exception:
                self.send("额度必须是整数：/usage set <user_openid> <daily_budget> [min_interval_seconds]")
                return True
            limits[target_user] = {
                "daily_budget": daily_budget,
                "min_interval_seconds": min_interval,
            }
            ConfigManager.save(self.config)
            if self._access is not None:
                self._access.set_user_limit(target_user, daily_budget, min_interval)
            self.send(f"已设置用户额度：{target_user}\n每日 {self._fmt_limit(daily_budget)} 次，间隔 {self._fmt_limit(min_interval)} 秒")
            return True
        self.send("用法：/usage | /usage id | /usage set <user_openid> <daily_budget> [min_interval_seconds] | /usage unset <user_openid>")
        return True

    def _handle_text_message(self, kind, message, text, callback):
        user_openid, group_openid = self._identity(kind, message)
        self._last_message = (kind, message)
        self._active_target = ("group", group_openid) if kind == "group" else ("c2c", user_openid)
        if self._is_duplicate_message(message):
            self._log_event(
                "duplicate_ignored",
                {
                    "kind": kind,
                    "user_openid": user_openid,
                    "group_openid": group_openid,
                    "message_id": getattr(message, "id", None),
                    "text": text,
                },
            )
            return
        self._log_event(
            "inbound",
            {
                "kind": kind,
                "user_openid": user_openid,
                "group_openid": group_openid,
                "message_id": getattr(message, "id", None),
                "text": text,
            },
        )

        allowed, reason = self._is_allowed(kind, user_openid, group_openid)
        if not allowed:
            self._log_event("blocked", {"kind": kind, "user_openid": user_openid, "group_openid": group_openid, "reason": reason})
            return

        if kind == "group" and not self._should_respond_to_group(text):
            self._log_event("ignored", {"kind": kind, "user_openid": user_openid, "group_openid": group_openid, "reason": "group_response_policy"})
            return

        if self._is_usage_command(text):
            self._log_event("usage_command", {"kind": kind, "user_openid": user_openid, "group_openid": group_openid})
            self._handle_usage_command(kind, user_openid, group_openid, text)
            return

        ok, reason = self._check_and_record_budget(kind, user_openid, group_openid)
        if not ok:
            self._log_event("rate_limited", {"kind": kind, "user_openid": user_openid, "group_openid": group_openid, "reason": reason})
            if self._bool_config("notify_on_limit", True):
                self.send(f"请求已被限制：{reason}")
            return

        if text and callback:
            print(f"📩 QQ {kind}: {text}")
            if kind == "group":
                session_key = f"qq:group:{group_openid or 'unknown'}"
            else:
                session_key = f"qq:c2c:{user_openid or 'unknown'}"
            payload = {
                "__mmclaw_text": text,
                "__mmclaw_session_key": session_key,
                "__mmclaw_reply_context": {
                    "last_message": (kind, message),
                    "active_target": self._active_target,
                },
            }
            threading.Thread(target=callback, args=(payload,), daemon=True).start()

    def activate_context(self, context):
        if not isinstance(context, dict):
            return
        last_message = context.get("last_message")
        active_target = context.get("active_target")
        if last_message:
            self._last_message = last_message
        if active_target:
            self._active_target = active_target

    def _daily_report_group_openids(self):
        raw_values = [
            self.qq_config.get("daily_report_group_openids"),
            self.qq_config.get("daily_report_group_openid"),
            self.qq_config.get("daily_push_group_openid"),
            self.qq_config.get("default_group_openid"),
        ]
        group_openids = []
        seen = set()
        for raw in raw_values:
            if not raw:
                continue
            values = raw if isinstance(raw, (list, tuple, set)) else [raw]
            for value in values:
                group_openid = str(value or "").strip()
                if group_openid and group_openid not in seen:
                    group_openids.append(group_openid)
                    seen.add(group_openid)
        return group_openids

    def _daily_report_group_openid(self):
        group_openids = self._daily_report_group_openids()
        return group_openids[0] if group_openids else None

    def _daily_report_due(self):
        if not self._bool_config("daily_report_push_enabled", False):
            return False
        report_time = str(self.qq_config.get("daily_report_time", "09:30"))
        try:
            hour, minute = [int(x) for x in report_time.split(":", 1)]
        except Exception:
            hour, minute = 9, 30
        now = datetime.now()
        return (now.hour, now.minute) >= (hour, minute)

    def _find_daily_report(self):
        today = self._today_key()
        report_dir = self._daily_report_dir()
        exact = os.path.join(report_dir, f"{today}.md")
        if os.path.exists(exact):
            return exact
        try:
            candidates = [
                os.path.join(report_dir, name)
                for name in os.listdir(report_dir)
                if name.startswith(today) and name.endswith(".md")
            ]
        except Exception:
            return None
        return sorted(candidates)[0] if candidates else None

    def _split_text_chunks(self, text, limit):
        limit = max(500, int(limit or 3500))
        chunks = []
        current = []
        current_len = 0
        for line in str(text or "").splitlines(keepends=True):
            pieces = [line]
            if len(line) > limit:
                pieces = [line[i : i + limit] for i in range(0, len(line), limit)]
            for piece in pieces:
                if current and current_len + len(piece) > limit:
                    chunks.append("".join(current))
                    current = [piece]
                    current_len = len(piece)
                else:
                    current.append(piece)
                    current_len += len(piece)
        if current:
            chunks.append("".join(current))
        return chunks or [""]

    def _daily_report_messages(self, path):
        max_chars = max(500, min(self._int_config("daily_report_max_chars", 3500), 3800))
        body_limit = max_chars - 120
        try:
            content = render_report_file(path)
        except Exception as e:
            self._log_event("daily_report_read_failed", {"path": str(path), "error": str(e)})
            return ["全市场日报暂时无法读取，请稍后重试。"]

        chunks = self._split_text_chunks(content, body_limit)
        total = len(chunks)
        messages = []
        for index, chunk in enumerate(chunks, start=1):
            if total == 1:
                title = f"全市场日报 {self._today_key()}"
            else:
                title = f"全市场日报 {self._today_key()} ({index}/{total})"
            messages.append(f"{title}\n\n{chunk}")
        return messages


    async def _send_target_async(self, target_kind, target_id, text):
        if not self._api or not target_id:
            return False
        seq_key = f"{target_kind}:{target_id}"
        start_seq = int(time.time() * 1000) % 1000000
        self._msg_seq[seq_key] = self._msg_seq.get(seq_key, start_seq) + 1
        try:
            if target_kind == "group":
                await self._api.post_group_message(
                    group_openid=target_id,
                    msg_type=0,
                    content=text,
                    msg_seq=self._msg_seq[seq_key],
                )
            else:
                await self._api.post_c2c_message(
                    openid=target_id,
                    msg_type=0,
                    content=text,
                    msg_seq=self._msg_seq[seq_key],
                )
            self._log_event("outbound", {"kind": target_kind, "target_openid": target_id, "text": text})
            return True
        except Exception as e:
            print(f"[!] QQ Bot Direct Send Error: {e}")
            self._log_event(
                "outbound_failed",
                {"kind": target_kind, "target_openid": target_id, "text": text, "error": str(e)},
            )
            return False


    def send_to_group(self, group_openid, message):
        if not self._loop or not group_openid:
            return False
        if self.config.get("tool_profile") == "finance_chat_readonly":
            message = render_finance_text(message)
        # Reserve two characters for the outbound prefix. Never truncate the
        # source text: a long proactive report must be sent as multiple posts.
        chunks = self._split_text_chunks(str(message or ""), 3998)
        for chunk in chunks:
            future = asyncio.run_coroutine_threadsafe(
                self._send_target_async("group", str(group_openid), f"⚡ {chunk}"),
                self._loop,
            )
            try:
                if not future.result(timeout=30):
                    return False
            except Exception as e:
                print(f"[!] QQ Bot Direct Send Error: {e}")
                return False
        return True

    def send_to_user(self, user_openid, message):
        if not self._loop or not user_openid:
            return False
        if self.config.get("tool_profile") == "finance_chat_readonly":
            message = render_finance_text(message)
        future = asyncio.run_coroutine_threadsafe(
            self._send_target_async("c2c", str(user_openid), f"⚡ {message}"), self._loop,
        )
        try:
            return bool(future.result(timeout=30))
        except Exception as exc:
            self._log_event("outbox_direct_send_failed", {"kind": "c2c", "error": str(exc)})
            return False

    def _start_outbox_worker(self):
        if self._outbox_worker is not None:
            return

        def _send(target_type, target_id, payload):
            text = str(payload.get("text") or "")
            if target_type == "group":
                return self.send_to_group(target_id, text)
            return self.send_to_user(target_id, text)

        self._outbox_worker = OutboxWorker("qq", _send, log=self._log_event)
        self._outbox_worker.start()
        print("[*] QQ finance outbox worker started.")

    def send_messages_to_group(self, group_openid, messages, delay_seconds=0.8):
        if not self._loop or not group_openid:
            return False
        for message in messages:
            if not self.send_to_group(group_openid, message):
                return False
            time.sleep(max(0, float(delay_seconds or 0)))
        return True

    def _start_daily_report_scheduler(self):
        if self._daily_report_thread_started:
            return
        if not self._bool_config("daily_report_push_enabled", False):
            return
        self._daily_report_thread_started = True
        threading.Thread(target=self._daily_report_loop, daemon=True).start()
        print("[*] QQ daily report scheduler started.")

    def _daily_report_loop(self):
        while True:
            try:
                self._maybe_push_daily_report()
            except Exception as e:
                print(f"[!] QQ daily report scheduler error: {e}")
            time.sleep(60)

    def _maybe_push_value_plan(self,group_openid,today,delivered):
        try:
            from .value_plan_delivery import deliver_after_report
            from .outbox import _connect
            result=deliver_after_report(group_openid,today,delivered['last_sent_path'],delivered['sent_at'],
                self.send_messages_to_group,_connect,self._log_event)
            if result['status'] not in ('disabled','waiting_for_plan','validation_pending','before_enabled_date') and result.get('parts')!=0:
                self._log_event('daily_value_plan_delivery',result)
        except Exception as exc:
            self._log_event('daily_value_plan_error',{'error_type':type(exc).__name__})

    def _maybe_push_daily_report(self):
        if not self._daily_report_due():
            return
        today = self._today_key()
        state = self._load_daily_report_state()
        group_openids = self._daily_report_group_openids()
        if not group_openids:
            self._log_event("daily_report_skipped", {"reason": "missing_group_openid", "date": today})
            return
        sent_groups = state.setdefault("sent_groups", {})
        # A plan can finish after the report. Check this hook on later polls,
        # including when today's report itself has already been sent.
        for group_openid in group_openids:
            delivered=sent_groups.get(group_openid,{})
            if delivered.get('last_sent_date')==today:
                self._maybe_push_value_plan(group_openid,today,delivered)
        # The scheduler polls every minute. Check delivery state before reading
        # the report or calling the model to summarize an already delivered day.
        if all(
            (state.get("last_sent_date") == today and state.get("group_openid") == group)
            or sent_groups.get(group, {}).get("last_sent_date") == today
            or sent_groups.get(group, {}).get("last_failed_date") == today
            for group in group_openids
        ):
            return
        path = self._find_daily_report()
        if not path:
            self._log_event("daily_report_waiting", {"reason": "missing_report", "date": today, "dir": self._daily_report_dir()})
            return
        messages = self._daily_report_messages(path)
        for group_openid in group_openids:
            group_state = sent_groups.get(group_openid, {})
            legacy_sent = state.get("last_sent_date") == today and state.get("group_openid") == group_openid
            if legacy_sent or group_state.get("last_sent_date") == today:
                if legacy_sent and group_openid not in sent_groups:
                    sent_groups[group_openid] = {
                        "last_sent_date": today,
                        "last_sent_path": state.get("last_sent_path", path),
                        "sent_at": state.get("sent_at", self._now_iso()),
                        "message_count": state.get("message_count", len(messages)),
                        "brief_sent": bool(state.get("brief_sent", False)),
                    }
                    self._save_daily_report_state(state)
                continue
            if group_state.get("last_failed_date") == today:
                continue
            ok = self.send_messages_to_group(group_openid, messages)
            if ok:
                sent_groups[group_openid] = {
                    "last_sent_date": today,
                    "last_sent_path": path,
                    "sent_at": self._now_iso(),
                    "message_count": len(messages),
                    "brief_sent": False,
                }
                state.update(
                    {
                        "last_sent_date": today,
                        "last_sent_path": path,
                        "sent_at": self._now_iso(),
                        "group_openid": group_openid,
                        "message_count": len(messages),
                        "brief_sent": False,
                    }
                )
                self._save_daily_report_state(state)
                self._log_event("daily_report_pushed", {"date": today, "path": path, "group_openid": group_openid, "message_count": len(messages), "brief_sent": False})
                self._maybe_push_value_plan(group_openid,today,sent_groups[group_openid])
            else:
                sent_groups[group_openid] = {
                    "last_failed_date": today,
                    "last_failed_path": path,
                    "failed_at": self._now_iso(),
                    "message_count": len(messages),
                    "brief_sent": False,
                }
                self._save_daily_report_state(state)
                self._log_event(
                    "daily_report_failed",
                    {
                        "date": today,
                        "path": path,
                        "group_openid": group_openid,
                        "message_count": len(messages),
                        "brief_sent": False,
                    },
                )

    def listen(self, callback, stop_on_auth=False):
        if stop_on_auth:
            print("[!] QQ Bot stop-on-auth mode is enabled; QQ connector has no local auth flow to persist.")
            return

        try:
            import fcntl

            lock_path = ConfigManager.CONFIG_DIR / "qqbot.lock"
            os.makedirs(ConfigManager.CONFIG_DIR, exist_ok=True)
            self._instance_lock_handle = open(lock_path, "w", encoding="utf-8")
            fcntl.flock(self._instance_lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._instance_lock_handle.write(str(os.getpid()))
            self._instance_lock_handle.flush()
        except BlockingIOError:
            print("[!] QQ Bot is already running for this workspace; refusing to start a duplicate instance.")
            return
        except Exception as e:
            print(f"[!] QQ Bot singleton lock warning: {e}")

        import botpy
        from botpy.connection import ConnectionState
        from botpy.message import GroupMessage

        if not hasattr(ConnectionState, "parse_group_message_create"):
            def parse_group_message_create(state, payload):
                message = GroupMessage(state.api, payload.get("id", None), payload.get("d", {}))
                state._dispatch("group_message_create", message)

            ConnectionState.parse_group_message_create = parse_group_message_create

        self.callback = callback
        connector = self

        class MMClawBot(botpy.Client):
            async def on_ready(self):
                connector._loop = asyncio.get_event_loop()
                connector._api = self.api
                print("\n[✅] QQ Bot is online! Direct messages and group @ messages are enabled.")
                if connector.qq_config.get("file_public_base_url") and connector.qq_config.get("file_upload_mode", "url") != "inline":
                    connector._ensure_file_server()
                connector._start_outbox_worker()
                connector._start_daily_report_scheduler()

            async def on_c2c_message_create(self, message):
                """Triggered when a user sends a direct message to the bot."""
                import urllib.request
                text = message.content.strip()

                attachments = getattr(message, "attachments", None)
                if attachments:
                    for att in attachments:
                        if getattr(att, "content_type", "").startswith("image/"):
                            user_openid, group_openid = connector._identity("c2c", message)
                            connector._last_message = ("c2c", message)
                            connector._active_target = ("c2c", user_openid)
                            connector._log_event(
                                "inbound_image",
                                {
                                    "kind": "c2c",
                                    "user_openid": user_openid,
                                    "group_openid": group_openid,
                                    "message_id": getattr(message, "id", None),
                                    "text": text,
                                    "filename": getattr(att, "filename", None),
                                },
                            )
                            allowed, reason = connector._is_allowed("c2c", user_openid, group_openid)
                            if not allowed:
                                connector._log_event("blocked", {"kind": "c2c", "user_openid": user_openid, "reason": reason})
                                return
                            ok, reason = connector._check_and_record_budget("c2c", user_openid, group_openid)
                            if not ok:
                                connector._log_event("rate_limited", {"kind": "c2c", "user_openid": user_openid, "reason": reason})
                                if connector._bool_config("notify_on_limit", True):
                                    connector.send(f"请求已被限制：{reason}")
                                return
                            try:
                                with urllib.request.urlopen(att.url, timeout=15) as resp:
                                    image_bytes = resp.read()
                                content = prepare_image_content(image_bytes, text if text else "这张图片里有什么？")
                                print(f"📩 QQ: [Photo] {text} (Compressed)")
                                if connector.callback:
                                    threading.Thread(target=connector.callback, args=(content,), daemon=True).start()
                            except Exception as e:
                                print(f"[!] QQ Bot Photo Error: {e}")
                            return

                if text:
                    connector._handle_text_message("c2c", message, text, connector.callback)

            async def on_group_at_message_create(self, message):
                """Triggered when the bot is @mentioned in a QQ group."""
                text = (message.content or "").strip()
                if text:
                    connector._handle_text_message("group", message, text, connector.callback)

            async def on_group_message_create(self, message):
                """Triggered by newer QQ group message events."""
                text = (message.content or "").strip()
                if text:
                    connector._handle_text_message("group", message, text, connector.callback)

            async def on_group_add_robot(self, event):
                group_openid = getattr(event, "group_openid", None)
                connector._log_event("group_add_robot", {"group_openid": group_openid})
                print(f"[*] QQ Bot added to group: {group_openid}")

        print("\n--- MMClaw Kernel Active (QQ Bot Mode) ---")
        intents = botpy.Intents(public_messages=True)
        connector._client = MMClawBot(intents=intents, bot_log=False)
        connector._client.run(appid=self.app_id, secret=self.app_secret)

    def start_typing(self):
        if self._current_target_kind() == "group":
            return
        self.send("⏳")

    def stop_typing(self):
        if self._current_target_kind() == "group":
            return
        self.send("✅")

    async def _send_group_active_text_async(self, group_openid, text, original_message=None, reason=None):
        if not self._api or not group_openid:
            return False
        mention = self._group_mention_for_message(original_message) if original_message else ""
        fallback_text = f"{mention}{text}" if mention and not str(text).startswith(mention) else str(text)
        limit = 4000
        chunks = self._split_text_chunks(fallback_text, limit)
        seq_key = f"group:{group_openid}"
        for chunk in chunks:
            start_seq = int(time.time() * 1000) % 1000000
            self._msg_seq[seq_key] = self._msg_seq.get(seq_key, start_seq) + 1
            await self._api.post_group_message(
                group_openid=group_openid,
                msg_type=0,
                content=chunk,
                msg_seq=self._msg_seq[seq_key],
            )
        self._log_event(
            "outbound_active_fallback",
            {
                "kind": "group",
                "target_openid": group_openid,
                "text": fallback_text,
                "message_count": len(chunks),
                "reason": str(reason or ""),
            },
        )
        return True

    async def _send_async(self, text):
        if not self._api:
            return
        kind, msg = self._last_message if self._last_message else (None, None)
        target = self._active_target
        if not target:
            default_group = self.qq_config.get("default_group_openid") or self.qq_config.get("daily_push_group_openid")
            default_user = self.qq_config.get("default_user_openid")
            if default_group:
                target = ("group", str(default_group))
            elif default_user:
                target = ("c2c", str(default_user))
        if not target:
            return
        target_kind, target_id = target
        seq_key = f"{target_kind}:{target_id}"
        start_seq = int(time.time() * 1000) % 1000000
        self._msg_seq[seq_key] = self._msg_seq.get(seq_key, start_seq) + 1
        reply_msg_id = getattr(msg, "id", None)
        try:
            if target_kind == "group":
                await self._api.post_group_message(
                    group_openid=target_id,
                    msg_type=0,
                    content=text,
                    msg_id=reply_msg_id if kind == "group" else None,
                    msg_seq=self._msg_seq[seq_key],
                )
            else:
                await self._api.post_c2c_message(
                    openid=target_id,
                    msg_type=0,
                    content=text,
                    msg_id=reply_msg_id if kind == "c2c" else None,
                    msg_seq=self._msg_seq[seq_key],
                )
            self._log_event("outbound", {"kind": target_kind, "target_openid": target_id, "text": text})
        except Exception as e:
            if target_kind == "group" and kind == "group" and self._is_expired_reply_error(e):
                try:
                    await self._send_group_active_text_async(target_id, text, msg, reason=e)
                    self._log_event(
                        "outbound_reply_fallback",
                        {
                            "kind": target_kind,
                            "target_openid": target_id,
                            "reply_msg_id": reply_msg_id,
                            "error": str(e),
                        },
                    )
                    return
                except Exception as fallback_error:
                    print(f"[!] QQ Bot Reply Fallback Error: {fallback_error}")
                    self._log_event(
                        "outbound_reply_fallback_failed",
                        {
                            "kind": target_kind,
                            "target_openid": target_id,
                            "reply_msg_id": reply_msg_id,
                            "error": str(e),
                            "fallback_error": str(fallback_error),
                        },
                    )
            print(f"[!] QQ Bot Reply Error: {e}")

    def send(self, message):
        if not self._loop or not self._last_message:
            return
        is_group = self._current_target_kind() == "group"
        if is_group and self._is_progress_message(message):
            self._log_event(
                "suppressed_progress",
                {
                    "kind": "group",
                    "target_openid": self._active_target[1] if self._active_target else "",
                    "text": message,
                },
            )
            return
        # QQ limits each post to about 4000 characters. Reserve space for the
        # visible prefix and split the complete response before sending it.
        # The previous implementation truncated group replies before splitting,
        # which silently discarded every part after the first 4000 characters.
        if self.config.get("tool_profile") == "finance_chat_readonly":
            message = render_finance_text(message)
        chunks = self._split_text_chunks(str(message or ""), 3998)
        for chunk in chunks:
            text = f"⚡ {chunk}"
            future = asyncio.run_coroutine_threadsafe(self._send_async(text), self._loop)
            try:
                future.result(timeout=30)
            except concurrent.futures.TimeoutError as e:
                future.cancel()
                print(f"[!] QQ Bot Send Timeout, falling back to active group message: {e}")
                if is_group and self._active_target:
                    group_openid = self._active_target[1]
                    active_future = asyncio.run_coroutine_threadsafe(
                        self._send_group_active_text_async(
                            group_openid,
                            text,
                            self._last_message[1] if self._last_message else None,
                            reason="send_timeout",
                        ),
                        self._loop,
                    )
                    try:
                        active_future.result(timeout=30)
                        continue
                    except Exception as fallback_error:
                        print(f"[!] QQ Bot Active Fallback Timeout/Error: {fallback_error}")
                        self._log_event(
                            "outbound_active_fallback_failed",
                            {
                                "kind": "group",
                                "target_openid": group_openid,
                                "text": text,
                                "error": str(fallback_error),
                            },
                        )
                break
            except Exception as e:
                print(f"[!] QQ Bot Send Error: {e}")
                break

    async def _upload_image_media(self, target_kind, target_id, path, url=None):
        if self.qq_config.get("file_upload_mode", "url") == "inline":
            from botpy.http import Route

            root = Path(self._generated_files_dir()).resolve()
            full_path = Path(path).expanduser().resolve()
            if not full_path.is_relative_to(root):
                raise ValueError("QQ image upload must be inside the generated directory")
            max_bytes = self._int_config("file_upload_max_bytes", 10 * 1024 * 1024)
            if full_path.stat().st_size > max_bytes:
                raise ValueError("QQ image exceeds the configured upload size limit")
            content = full_path.read_bytes()
            if not content or len(content) > max_bytes:
                raise ValueError("QQ image is empty or exceeds the upload size limit")
            if target_kind == "group":
                route = Route("POST", "/v2/groups/{group_openid}/files", group_openid=target_id)
            elif target_kind == "c2c":
                route = Route("POST", "/v2/users/{openid}/files", openid=target_id)
            else:
                raise ValueError("Unsupported QQ image target")
            media = await self._api._http.request(route, json={
                "file_type": 1, "file_data": base64.b64encode(content).decode("ascii"),
                "srv_send_msg": False,
            })
        elif target_kind == "group":
            media = await self._api.post_group_file(
                group_openid=target_id, file_type=1,
                url=url or self._file_url_for_path(path), srv_send_msg=False,
            )
        else:
            media = await self._api.post_c2c_file(
                openid=target_id, file_type=1,
                url=url or self._file_url_for_path(path), srv_send_msg=False,
            )
        if not isinstance(media, dict) or not media.get("file_info"):
            raise RuntimeError("QQ upload returned no usable media reference")
        return media

    async def _send_file_async(self, path):
        if not self._api:
            return
        kind, msg = self._last_message if self._last_message else (None, None)
        target = self._active_target
        if not target:
            default_group = self.qq_config.get("default_group_openid") or self.qq_config.get("daily_push_group_openid")
            default_user = self.qq_config.get("default_user_openid")
            if default_group:
                target = ("group", str(default_group))
            elif default_user:
                target = ("c2c", str(default_user))
        if not target:
            raise RuntimeError("no active QQ target for file upload")

        target_kind, target_id = target
        url = None if self.qq_config.get("file_upload_mode", "url") == "inline" else self._file_url_for_path(path)
        seq_key = f"{target_kind}:{target_id}"
        start_seq = int(time.time() * 1000) % 1000000
        self._msg_seq[seq_key] = self._msg_seq.get(seq_key, start_seq) + 1
        reply_msg_id = getattr(msg, "id", None)

        try:
            if target_kind == "group":
                media = await self._upload_image_media("group", target_id, path, url=url)
                await self._api.post_group_message(
                    group_openid=target_id,
                    msg_type=7,
                    media=media,
                    msg_id=reply_msg_id if kind == "group" else None,
                    msg_seq=self._msg_seq[seq_key],
                )
            else:
                media = await self._upload_image_media("c2c", target_id, path, url=url)
                await self._api.post_c2c_message(
                    openid=target_id,
                    msg_type=7,
                    media=media,
                    msg_id=reply_msg_id if kind == "c2c" else None,
                    msg_seq=self._msg_seq[seq_key],
                )
            self._log_event(
                "outbound_file",
                {
                    "kind": target_kind,
                    "target_openid": target_id,
                    "path": path,
                    "url": url,
                },
            )
        except Exception as e:
            if target_kind == "group" and kind == "group" and reply_msg_id and self._is_expired_reply_error(e):
                await self._send_group_active_file_async(target_id, path, url=url, reason=e)
                return
            self._log_event(
                "outbound_file_failed",
                {
                    "kind": target_kind,
                    "target_openid": target_id,
                    "path": path,
                    "url": url,
                    "error": str(e),
                },
            )
            raise

    async def _send_group_active_file_async(self, group_openid, path, url=None, reason=None):
        if not self._api or not group_openid:
            return False
        if self.qq_config.get("file_upload_mode", "url") != "inline":
            url = url or self._file_url_for_path(path)
        seq_key = f"group:{group_openid}"
        start_seq = int(time.time() * 1000) % 1000000
        self._msg_seq[seq_key] = self._msg_seq.get(seq_key, start_seq) + 1
        media = await self._upload_image_media("group", group_openid, path, url=url)
        await self._api.post_group_message(
            group_openid=group_openid,
            msg_type=7,
            media=media,
            msg_seq=self._msg_seq[seq_key],
        )
        self._log_event(
            "outbound_file_active_fallback",
            {
                "kind": "group",
                "target_openid": group_openid,
                "path": path,
                "url": url,
                "reason": str(reason or ""),
            },
        )
        return True

    def send_file(self, path):
        if not self._loop:
            return
        import mimetypes

        full_path = os.path.abspath(os.path.expanduser(path))
        if not os.path.exists(full_path):
            self.send(f"❌ File not found: {path}")
            return

        mime, _ = mimetypes.guess_type(full_path)
        if not (mime or "").startswith("image/"):
            self.send("❌ QQ Bot 目前只支持发送 PNG/JPG 图片。")
            return
        try:
            full_path = self._stage_file_for_upload(full_path)
        except Exception as e:
            print(f"[!] QQ Bot stage file error: {e}")
            self.send(f"图片已生成，但发送前复制到上传目录失败：{e}\n本地路径：{full_path}")
            return

        future = asyncio.run_coroutine_threadsafe(self._send_file_async(full_path), self._loop)
        try:
            future.result(timeout=60)
        except concurrent.futures.TimeoutError as e:
            future.cancel()
            print(f"[!] QQ Bot Send File Timeout, falling back to active group file: {e}")
            if self._current_target_kind() == "group" and self._active_target:
                group_openid = self._active_target[1]
                active_future = asyncio.run_coroutine_threadsafe(
                    self._send_group_active_file_async(group_openid, full_path, reason="send_file_timeout"),
                    self._loop,
                )
                try:
                    active_future.result(timeout=60)
                    return
                except Exception as fallback_error:
                    print(f"[!] QQ Bot Active File Fallback Error: {fallback_error}")
                    self._log_event(
                        "outbound_file_active_fallback_failed",
                        {
                            "kind": "group",
                            "target_openid": group_openid,
                            "path": full_path,
                            "error": str(fallback_error),
                        },
                    )
            self.send(f"图片已生成，但 QQ 图片发送超时。\n本地路径：{full_path}")
        except Exception as e:
            print(f"[!] QQ Bot Send File Error: {e}")
            self.send(f"图片已生成，但 QQ 图片发送失败：{e}\n本地路径：{full_path}")
