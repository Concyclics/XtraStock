# -*- coding: utf-8 -*-
"""HTTP 层：Session + 重试计数器 + 主机回退链 + JSON/JSONP 解析。

重试模式参照 YiLinSpider/utils.py:loadFromURL 的 `cnt = MAX_RETRIES`，
扩展为：/api/qt/* 端点按 QT_HOSTS 顺序回退。不同主机服务的端点子集
不同（push2 全量但常被拦 / push2his 仅 kline,fflow / push2delay 全量
且对 python-requests 最稳定），故按「真实请求」探测：逐主机发起实际
请求，命中即缓存该主机优先复用；缓存主机对某端点失效时自动继续回退。
"""
import json
import os
import subprocess
import sys
import threading
import time
from urllib.parse import urlencode
# curl_cffi 以 Chrome TLS/JA3 指纹发起请求，绕过 push2his 等主机针对
# python-requests 的 TLS 指纹拦截；接口与 requests 基本兼容。
from curl_cffi import requests as cffi_requests
from curl_cffi.requests.exceptions import RequestException as CffiError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import systemConfig  # noqa: E402
from eastmoney.circuit import EndpointCircuit


class EastMoneyClient:
    def __init__(self):
        self._local = threading.local()
        self._good_hosts = {}  # endpoint family -> last successful qt host
        self._lock = threading.Lock()
        self._last_history_request = 0.0
        self.circuit = EndpointCircuit()
        self.timeout = systemConfig.TIMEOUT
        self.retries = systemConfig.MAX_RETRIES
        self.deadline = None

    def configure_budget(self, seconds, timeout=4, retries=1):
        self.deadline = time.monotonic() + max(1, seconds)
        self.timeout, self.retries = max(0.1, timeout), max(1, retries)

    def remaining(self):
        return max(0.0, self.deadline - time.monotonic()) if self.deadline else float("inf")

    def _timeout(self):
        return min(self.timeout, self.remaining())

    def _throttle_history(self):
        """Apply one process-wide history rate limit across worker threads."""
        with self._lock:
            wait = systemConfig.REQUEST_INTERVAL - (time.monotonic() - self._last_history_request)
            if wait > 0:
                time.sleep(wait)
            self._last_history_request = time.monotonic()

    def _session(self):
        session = getattr(self._local, "session", None)
        if session is None:
            session = cffi_requests.Session(impersonate="chrome")
            session.headers.update(systemConfig.HEADERS)
            self._local.session = session
        return session

    def _endpoint_key(self, path):
        if "fflow/daykline/get" in path:
            return "fundflow"
        if "kline/get" in path:
            return "history"
        if "trends2/get" in path:
            return "trends"
        if "stock/get" in path:
            return "snapshot"
        return path

    def _host_order(self, path):
        key = self._endpoint_key(path)
        if key in {"history", "fundflow"}:
            hosts = ["push2his.eastmoney.com", "push2.eastmoney.com", "push2delay.eastmoney.com"]
        elif key == "trends":
            # push2his can return the recent multi-day window. The delay host
            # generally responds, but may only provide the latest session.
            hosts = ["push2his.eastmoney.com", "push2delay.eastmoney.com", "push2.eastmoney.com"]
        else:
            hosts = list(systemConfig.QT_HOSTS)
        with self._lock:
            good_host = self._good_hosts.get(key)
        if good_host and good_host in hosts:
            hosts.remove(good_host)
            hosts.insert(0, good_host)
        return hosts

    @staticmethod
    def _decode_payload(text, jsonp):
        text = text.strip()
        if not text:
            return None
        if jsonp or text[0] not in "{[":
            left = text.find("(")
            right = text.rfind(")")
            if left < 0 or right <= left:
                return None
            text = text[left + 1:right]
        return json.loads(text)

    def _request(self, host, path, params, jsonp):
        url = f"https://{host}{path}"
        for _ in range(self.retries):
            if self.remaining() <= systemConfig.REQUEST_INTERVAL:
                return None
            try:
                time.sleep(systemConfig.REQUEST_INTERVAL)
                r = self._session().get(url, params=params,
                                        timeout=max(0.1, self._timeout()))
                r.raise_for_status()
                data = self._decode_payload(r.text, jsonp)
                if data is not None:
                    return data
            except (CffiError, ValueError):
                continue
        return None

    def _request_with_curl(self, host, path, params, jsonp):
        """Use the system curl transport for endpoints that reset Python TLS.

        Eastmoney currently closes the K-line connection for this host's
        Python TLS clients, while the same request from curl succeeds. Keep
        this narrow fallback limited to historical endpoints; all other
        calls retain the in-process connection pool.
        """
        url = f"https://{host}{path}?{urlencode(params)}"
        command = [
            "curl", "--fail", "--silent", "--show-error", "--location",
            "--max-time", str(max(0.1, self._timeout())),
            "--user-agent", systemConfig.HEADERS["User-Agent"],
            "--referer", systemConfig.HEADERS["Referer"],
            url,
        ]
        last_error = ""
        for attempt in range(self.retries):
            if self.remaining() <= systemConfig.REQUEST_INTERVAL:
                return None
            self._throttle_history()
            try:
                result = subprocess.run(
                    command,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=max(0.1, self._timeout()) + 1,
                )
                payload = self._decode_payload(result.stdout, jsonp)
                if payload is not None:
                    return payload
                last_error = "empty or invalid response"
            except subprocess.CalledProcessError as exc:
                last_error = f"curl exit {exc.returncode}"
            except (OSError, subprocess.SubprocessError, ValueError) as exc:
                last_error = str(exc)
            if attempt + 1 < self.retries:
                time.sleep(min(2 ** attempt, 4))
        print(f"[client] curl host={host} failed: {last_error[:240]}")
        return None

    def get_qt(self, path, params, jsonp=False, required_data_key=None):
        """/api/qt/* 端点：按回退链逐主机尝试，命中即缓存主机。"""
        key = self._endpoint_key(path)
        if self.remaining() <= 0 or not self.circuit.allow(path):
            return None
        transport_ok = False
        for host in self._host_order(path):
            if self.remaining() <= 0:
                break
            # Prefer curl for historical K-line and fund-flow calls on this
            # machine. It avoids the endpoint's selective Python TLS reset.
            data = self._request_with_curl(host, path, params, jsonp) if key in {"history", "fundflow"} else None
            if data is None:
                data = self._request(host, path, params, jsonp)
            transport_ok = transport_ok or isinstance(data, dict)
            if isinstance(data, dict) and data.get("data") is not None:
                if required_data_key and not data["data"].get(required_data_key):
                    continue
                with self._lock:
                    old_host = self._good_hosts.get(key)
                    self._good_hosts[key] = host
                if host != old_host:
                    print(f"[client] 使用行情主机 {host}")
                self.circuit.record(path, True)
                return data
        self.circuit.record(path, transport_ok)
        # An empty response for one suspended/delisted instrument is not an
        # endpoint outage. Never log authentication-like query parameters.
        print(f"[client] no usable data path={path} transport_ok={transport_ok}")
        return None

    def datacenter(self, params):
        data = self._request(systemConfig.HOST_DATACENTER,
                             "/api/data/v1/get", params, False)
        if data is None:
            print(f"[client] datacenter 失败 params={params}")
        return data


# 模块级单例，供 fetchers 复用同一连接池
client = EastMoneyClient()
