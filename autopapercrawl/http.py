from __future__ import annotations

import http.client
import json
import socket
import time
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import ClassVar

import requests
import os


USER_AGENT = "AutoPaperCrawl/0.1 (lawful metadata search and open-access retrieval)"


@dataclass
class HttpClient:
    _arxiv_lock: ClassVar[threading.Lock] = threading.Lock()
    _arxiv_last_request: ClassVar[float] = 0.0
    _host_lock: ClassVar[threading.Lock] = threading.Lock()
    _host_requests: ClassVar[dict] = {}
    delay_seconds: float = 1.0
    timeout_seconds: float = 30.0
    retries: int = 2
    last_request_at: float = 0.0
    request_count: int = 0
    success_count: int = 0
    failure_count: int = 0

    def wait(self, url: str = "") -> None:
        elapsed = time.monotonic() - self.last_request_at
        remaining = self.delay_seconds - elapsed
        if remaining > 0:
            time.sleep(remaining)
        # search_all creates clients per source/query. Keep arXiv pacing shared
        # across those clients rather than resetting its delay on every query.
        if urllib.parse.urlparse(url).hostname == "export.arxiv.org":
            with self._arxiv_lock:
                remaining = max(3.0, self.delay_seconds) - (time.monotonic() - HttpClient._arxiv_last_request)
                if remaining > 0:
                    time.sleep(remaining)
                HttpClient._arxiv_last_request = time.monotonic()
        else:
            host = urllib.parse.urlparse(url).hostname
            if host and host in {"dblp.org", "api.openalex.org", "api.crossref.org"}:
                with HttpClient._host_lock:
                    spacing = max(1.0, self.delay_seconds)
                    remaining = spacing - (time.monotonic() - HttpClient._host_requests.get(host, 0))
                    if remaining > 0:
                        time.sleep(remaining)
                    HttpClient._host_requests[host] = time.monotonic()

    def get_bytes(self, url: str, headers: dict[str, str] | None = None) -> bytes:
        request_headers = {"User-Agent": USER_AGENT, "Accept": "*/*", "Connection": "close"}
        if headers:
            request_headers.update(headers)
        if urllib.parse.urlparse(url).hostname == "api.openalex.org" and os.getenv("OPENALEX_API_KEY"):
            request_headers["Authorization"] = "Bearer " + os.environ["OPENALEX_API_KEY"]

        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            self.wait(url)
            self.request_count += 1
            try:
                response = requests.get(url, headers=request_headers, timeout=self.timeout_seconds)
                if response.status_code in {200, 202, 403} and "text/html" in response.headers.get("Content-Type", "") and any(word in response.text[:4096].lower() for word in ("anubis_challenge", "cf-chl-", "making sure you're not a bot", "making sure you&#39;re not a bot")):
                    self.failure_count += 1
                    raise ValueError("站点访问校验，未尝试绕过；请稍后重试或使用其他已授权数据源")
                self.last_request_at = time.monotonic()
                if response.status_code in {429, 500, 502, 503, 504} and attempt < self.retries:
                    retry_after = response.headers.get("Retry-After")
                    sleep_for = float(retry_after) if retry_after and retry_after.isdigit() else 2**attempt
                    time.sleep(sleep_for)
                    continue
                response.raise_for_status()
                self.success_count += 1
                return response.content
            except requests.RequestException as exc:
                self.failure_count += 1
                self.last_request_at = time.monotonic()
                last_error = exc
                if attempt < self.retries:
                    time.sleep(2**attempt)
                    continue
                return self.get_bytes_urllib(url, request_headers)

        raise RuntimeError(f"GET failed for {url}: {last_error}")

    def get_bytes_urllib(self, url: str, request_headers: dict[str, str]) -> bytes:
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            self.wait(url)
            self.request_count += 1
            request = urllib.request.Request(url, headers=request_headers)
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                    try:
                        body = response.read()
                    except http.client.IncompleteRead as exc:
                        raise urllib.error.URLError(exc) from exc
                    self.last_request_at = time.monotonic()
                    self.success_count += 1
                    return body
            except urllib.error.HTTPError as exc:
                self.failure_count += 1
                self.last_request_at = time.monotonic()
                last_error = exc
                if exc.code in {429, 500, 502, 503, 504} and attempt < self.retries:
                    retry_after = exc.headers.get("Retry-After")
                    sleep_for = float(retry_after) if retry_after and retry_after.isdigit() else 2**attempt
                    time.sleep(sleep_for)
                    continue
                raise
            except (urllib.error.URLError, http.client.HTTPException, ConnectionError, TimeoutError, socket.timeout) as exc:
                self.failure_count += 1
                self.last_request_at = time.monotonic()
                last_error = exc
                if attempt < self.retries:
                    time.sleep(2**attempt)
                    continue
                raise

        raise RuntimeError(f"GET failed for {url}: {last_error}")

    def get_json(self, url: str, headers: dict[str, str] | None = None) -> dict:
        body = self.get_bytes(url, headers={"Accept": "application/json", **(headers or {})})
        try:
            return json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeError) as exc:
            raise ValueError("数据源未返回有效 JSON（可能为访问校验或错误页面）") from exc


def urlencode(params: dict[str, object | None]) -> str:
    clean = {k: v for k, v in params.items() if v is not None and v != ""}
    return urllib.parse.urlencode(clean)
