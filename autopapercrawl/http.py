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


USER_AGENT = "AutoPaperCrawl/0.1 (lawful metadata search and open-access retrieval)"


@dataclass
class HttpClient:
    _arxiv_lock: ClassVar[threading.Lock] = threading.Lock()
    _arxiv_last_request: ClassVar[float] = 0.0
    delay_seconds: float = 1.0
    timeout_seconds: float = 30.0
    retries: int = 2
    last_request_at: float = 0.0

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

    def get_bytes(self, url: str, headers: dict[str, str] | None = None) -> bytes:
        request_headers = {"User-Agent": USER_AGENT, "Accept": "*/*", "Connection": "close"}
        if headers:
            request_headers.update(headers)

        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            self.wait(url)
            try:
                response = requests.get(url, headers=request_headers, timeout=self.timeout_seconds)
                self.last_request_at = time.monotonic()
                if response.status_code in {429, 500, 502, 503, 504} and attempt < self.retries:
                    retry_after = response.headers.get("Retry-After")
                    sleep_for = float(retry_after) if retry_after and retry_after.isdigit() else 2**attempt
                    time.sleep(sleep_for)
                    continue
                response.raise_for_status()
                return response.content
            except requests.RequestException as exc:
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
            request = urllib.request.Request(url, headers=request_headers)
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                    try:
                        body = response.read()
                    except http.client.IncompleteRead as exc:
                        raise urllib.error.URLError(exc) from exc
                    self.last_request_at = time.monotonic()
                    return body
            except urllib.error.HTTPError as exc:
                self.last_request_at = time.monotonic()
                last_error = exc
                if exc.code in {429, 500, 502, 503, 504} and attempt < self.retries:
                    retry_after = exc.headers.get("Retry-After")
                    sleep_for = float(retry_after) if retry_after and retry_after.isdigit() else 2**attempt
                    time.sleep(sleep_for)
                    continue
                raise
            except (urllib.error.URLError, http.client.HTTPException, ConnectionError, TimeoutError, socket.timeout) as exc:
                self.last_request_at = time.monotonic()
                last_error = exc
                if attempt < self.retries:
                    time.sleep(2**attempt)
                    continue
                raise

        raise RuntimeError(f"GET failed for {url}: {last_error}")

    def get_json(self, url: str, headers: dict[str, str] | None = None) -> dict:
        body = self.get_bytes(url, headers={"Accept": "application/json", **(headers or {})})
        return json.loads(body.decode("utf-8"))


def urlencode(params: dict[str, object | None]) -> str:
    clean = {k: v for k, v in params.items() if v is not None and v != ""}
    return urllib.parse.urlencode(clean)
