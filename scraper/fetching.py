"""Bounded, robots-aware fetching without converting XML bytes to text.

DNS is checked immediately before every request, including redirects, and all
answers must be public. Requests resolves again when connecting: this does not
eliminate DNS rebinding. Use network egress filtering for that guarantee. The
60-second deadline covers waiting, redirects and streaming bodies; requests'
socket timeouts cannot strictly bound OS DNS resolution or trickled headers.
"""

import ipaddress
import math
import re
import socket
import threading
import time
import zlib
from dataclasses import dataclass
from datetime import timezone
from email.utils import parsedate_to_datetime
from urllib.parse import unquote_plus, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import requests
from urllib3.exceptions import HTTPError


MAX_SECONDS = 60.0
MAX_REDIRECTS = 5
MAX_URL_BYTES = 2000
ROBOTS_TTL = 3600.0
TRACKING_PARAMS = {"fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid", "igshid", "_ga", "_gl"}


class FetchError(Exception):
    def __init__(self, message="Fetch failed", retryable: bool = True, retry_after: float | None = None, *, status_code=None):
        super().__init__(message)
        self.retryable = bool(retryable)
        self.retry_after = retry_after
        self.status_code = status_code


@dataclass
class FetchResult:
    url: str
    body: bytes
    status: int
    etag: str | None = None
    last_modified: str | None = None
    content_type: str = ""


def _public_ip(address):
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not (ip.is_multicast or ip.is_reserved)


def normalize_url(url) -> str | None:
    """Validate syntax without DNS; retain query order, encoding and slashes."""
    if not isinstance(url, str) or re.search(r"[\s\x00-\x1f\x7f\\]", url):
        return None
    try:
        if len(url.encode("utf-8")) > MAX_URL_BYTES:
            return None
        parts = urlsplit(url)
        if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
            return None
        if parts.username is not None or parts.password is not None:
            return None
        host = parts.hostname.rstrip(".").encode("idna").decode("ascii").lower()
        if not host or "%" in host:
            return None
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            labels = host.split(".")
            if (
                len(host) > 253
                or len(labels) < 2
                or not re.search(r"[a-z]", labels[-1])
                or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels)
                or labels[-1] in {"localhost", "local", "internal", "lan", "home", "invalid", "test", "example", "onion"}
                or host.endswith(".home.arpa")
            ):
                return None
        else:
            if not _public_ip(host):
                return None
            host = f"[{address.compressed}]" if address.version == 6 else str(address)
        port = parts.port
        if port == 0 or parts.netloc.endswith(":"):
            return None
        scheme = parts.scheme.lower()
        if port is not None and port != (443 if scheme == "https" else 80):
            host += f":{port}"
        query = []
        for parameter in parts.query.split("&"):
            key = unquote_plus(parameter.partition("=")[0]).lower()
            if not key.startswith("utm_") and key not in TRACKING_PARAMS:
                query.append(parameter)
        normalized = urlunsplit((scheme, host, parts.path or "/", "&".join(query), ""))
        # IDNA conversion can expand the hostname beyond the input byte length.
        return normalized if len(normalized.encode("utf-8")) <= MAX_URL_BYTES else None
    except (ValueError, UnicodeError):
        return None


def same_site(url, base) -> bool:
    normalized = normalize_url(url)
    normalized_base = normalize_url(base)
    if normalized is None or normalized_base is None:
        return False
    return urlsplit(normalized).hostname.removeprefix("www.") == urlsplit(normalized_base).hostname.removeprefix("www.")


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise FetchError("Fetch deadline exceeded")
    return remaining


def _retry_after(value):
    if not value:
        return None
    try:
        if re.fullmatch(r"\d+", value.strip()):
            return float(int(value))
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return max(0.0, date.timestamp() - time.time())
    except (ValueError, TypeError, OverflowError):
        return None


class Fetcher:
    def __init__(self, user_agent="BlogSearchBot/2.0 (+https://blogsearch.io/bot)", delay=2.0, max_bytes=5_000_000, timeout=20):
        if not math.isfinite(delay) or delay < 0 or not math.isfinite(timeout) or timeout <= 0 or max_bytes <= 0:
            raise ValueError("Invalid fetch limits")
        self.user_agent = user_agent
        self.delay = delay
        self.max_bytes = max_bytes
        self.timeout = min(timeout, MAX_SECONDS)
        self._guard = threading.Lock()
        self._locks = {}
        self._last_request = {}
        self._robots_cache = {}

    def close(self):
        """Sessions are scoped to individual requests; nothing stays open."""

    def _lock_for(self, kind, key):
        with self._guard:
            return self._locks.setdefault((kind, key), threading.Lock())

    def fetch(self, url, etag=None, last_modified=None, check_robots=True) -> FetchResult:
        headers = {}
        if etag is not None:
            headers["If-None-Match"] = etag
        if last_modified is not None:
            headers["If-Modified-Since"] = last_modified
        return self._fetch(url, headers, time.monotonic() + MAX_SECONDS, check_robots)

    def robots_sitemaps(self, url) -> list[str]:
        url = normalize_url(url)
        if url is None:
            raise FetchError("Invalid or non-public URL", retryable=False)
        policy = self._robots(url, time.monotonic() + MAX_SECONDS, frozenset())
        # The sitemap itself is DNS-checked, with robots enforced, when fetched.
        return list(dict.fromkeys(
            normalized for value in policy.site_maps() or []
            if (normalized := normalize_url(value)) is not None
        ))

    def _robots(self, url, deadline, loading):
        parts = urlsplit(url)
        origin = urlunsplit((parts.scheme, parts.netloc, "", "", ""))
        if origin in loading:
            raise FetchError("Circular robots redirect")
        lock = self._lock_for("robots", origin)
        if not lock.acquire(timeout=_remaining(deadline)):
            raise FetchError("Robots lookup deadline exceeded")
        try:
            cached = self._robots_cache.get(origin)
            if cached and cached[0] > time.monotonic():
                return cached[1]
            result = self._fetch(
                origin + "/robots.txt", {}, deadline, False,
                robots_origin=origin, loading=loading | {origin},
            )
            policy = RobotFileParser()
            if result.status in (404, 410):
                policy.allow_all = True
            elif result.status in (401, 403):
                policy.disallow_all = True
            else:
                try:
                    policy.parse(result.body.decode("utf-8-sig", errors="replace").splitlines())
                except ValueError:
                    raise FetchError("Invalid robots response") from None
            self._robots_cache[origin] = (time.monotonic() + ROBOTS_TTL, policy)
            return policy
        finally:
            lock.release()

    def _fetch(self, url, headers, deadline, check_robots, robots_origin=None, loading=frozenset()):
        original_host = None
        for redirects in range(MAX_REDIRECTS + 1):
            url = normalize_url(url)
            if url is None:
                raise FetchError("Invalid or non-public URL", retryable=False)
            parts = urlsplit(url)
            origin = urlunsplit((parts.scheme, parts.netloc, "", "", ""))
            if original_host is None:
                original_host = parts.hostname
            policy = None
            if robots_origin is None:
                # Opting out for the input URL does not waive a new host's policy.
                if check_robots or parts.hostname != original_host:
                    check_robots = True
                    policy = self._robots(url, deadline, loading)
            elif origin != robots_origin and parts.path != "/robots.txt":
                # A redirected robots.txt is itself the policy bootstrap. Other
                # paths on a new origin require that origin's policy first.
                policy = self._robots(url, deadline, loading)
            delay = self.delay
            if policy is not None:
                if not policy.can_fetch(self.user_agent, url):
                    raise FetchError("Blocked by robots policy", retryable=False)
                delay = max(delay, policy.crawl_delay(self.user_agent) or 0)
                rate = policy.request_rate(self.user_agent)
                if rate and rate.requests > 0:
                    delay = max(delay, rate.seconds / rate.requests)
            result, location = self._request(url, headers, deadline, delay, robots_origin is not None)
            if location is None:
                return result
            if redirects == MAX_REDIRECTS:
                raise FetchError("Too many redirects", retryable=False)
            try:
                destination = normalize_url(urljoin(url, location))
            except ValueError:
                destination = None
            if destination is None:
                raise FetchError("Invalid or non-public redirect", retryable=False)
            if urlsplit(destination).netloc != parts.netloc or urlsplit(destination).scheme != parts.scheme:
                headers = {}  # Validators belong to the original origin.
            url = destination
        raise FetchError("Too many redirects", retryable=False)

    def _validate_dns(self, url):
        parts = urlsplit(url)
        try:
            addresses = socket.getaddrinfo(
                parts.hostname, parts.port or (443 if parts.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
            if not addresses:
                raise FetchError("DNS resolution failed")
            if any(not _public_ip(address[4][0]) for address in addresses):
                raise FetchError("Non-public network target", retryable=False)
        except (OSError, ValueError):
            raise FetchError("DNS resolution failed") from None

    def _request(self, url, headers, deadline, delay, robots):
        hostname = urlsplit(url).hostname
        lock = self._lock_for("host", hostname)
        if not lock.acquire(timeout=_remaining(deadline)):
            raise FetchError("Host wait deadline exceeded")
        try:
            wait = max(0.0, self._last_request.get(hostname, -math.inf) + delay - time.monotonic())
            if wait >= _remaining(deadline):
                raise FetchError("Host delay exceeds fetch deadline", retry_after=wait)
            if wait:
                time.sleep(wait)
            self._validate_dns(url)
            remaining = _remaining(deadline)
            self._last_request[hostname] = time.monotonic()
            with requests.Session() as session:
                # Neither environment proxies nor .netrc credentials may route
                # or authenticate these untrusted public URLs.
                session.trust_env = False
                request = session.prepare_request(requests.Request(
                    "GET", url,
                    headers={"User-Agent": self.user_agent, "Accept-Encoding": "gzip, deflate", **headers},
                ))
                # Session.send/get consumes redirect bodies even with
                # allow_redirects=False. The stock adapter sends exactly one
                # request, leaving every response body under our size limit.
                with session.get_adapter(url).send(
                    request, stream=True, verify=True, proxies={},
                    timeout=(min(self.timeout, remaining / 2), min(self.timeout, remaining / 2)),
                ) as response:
                    _remaining(deadline)
                    status = response.status_code
                    result = FetchResult(
                        url=url, body=b"", status=status,
                        etag=response.headers.get("ETag"),
                        last_modified=response.headers.get("Last-Modified"),
                        content_type=response.headers.get("Content-Type", ""),
                    )
                    if status in (301, 302, 303, 307, 308):
                        location = response.headers.get("Location")
                        if not location:
                            raise FetchError("Redirect missing destination", retryable=False)
                        return result, location
                    if status == 304 or (robots and status in (401, 403, 404, 410)):
                        if status == 304 and robots:
                            raise FetchError("Unexpected robots response")
                        return result, None
                    if not 200 <= status < 300:
                        raise FetchError(
                            f"HTTP status {status}",
                            retryable=status in (408, 425, 429) or 500 <= status < 600,
                            retry_after=_retry_after(response.headers.get("Retry-After")),
                            status_code=status,
                        )
                    result.body = self._body(response, deadline)
                    return result, None
        except requests.exceptions.InvalidHeader:
            raise FetchError("Invalid request headers", retryable=False) from None
        except (requests.RequestException, HTTPError, OSError, ValueError, zlib.error):
            raise FetchError("Network request or response decoding failed") from None
        finally:
            lock.release()

    def _body(self, response, deadline):
        length = response.headers.get("Content-Length")
        if length is not None:
            try:
                length = int(length)
            except ValueError:
                raise FetchError("Invalid response length") from None
            if length < 0:
                raise FetchError("Invalid response length")
            if length > self.max_bytes:
                raise FetchError("Response exceeds byte limit", retryable=False)
        encoding = response.headers.get("Content-Encoding", "").strip().lower()
        if encoding not in ("", "identity", "gzip", "deflate"):
            raise FetchError("Unsupported response encoding", retryable=False)
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS) if encoding == "gzip" else None
        body = bytearray()
        encoded_bytes = 0
        pending = b""

        # read1 avoids filling a large buffer from a trickling peer. A socket
        # shutdown watchdog also interrupts trickled HTTP chunk framing. This
        # accesses the ordinary requests/urllib3 CPython socket, not a transport
        # override; sessions are never shared or reused after this response.
        raw_socket = getattr(getattr(getattr(getattr(response.raw, "_fp", None), "fp", None), "raw", None), "_sock", None)
        timer = None
        if raw_socket is not None:
            def interrupt():
                try:
                    raw_socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

            timer = threading.Timer(_remaining(deadline), interrupt)
            timer.daemon = True
            timer.start()
        try:
            while True:
                _remaining(deadline)
                chunk = response.raw.read1(min(65536, self.max_bytes - encoded_bytes + 1), decode_content=False)
                _remaining(deadline)
                if not chunk:
                    break
                encoded_bytes += len(chunk)
                if encoded_bytes > self.max_bytes:
                    raise FetchError("Response exceeds byte limit", retryable=False)
                if encoding == "deflate" and decoder is None:
                    pending += chunk
                    if len(pending) < 2:
                        continue
                    # Servers use both zlib-wrapped and raw deflate streams.
                    wrapped = pending[0] & 15 == 8 and int.from_bytes(pending[:2], "big") % 31 == 0
                    decoder = zlib.decompressobj(zlib.MAX_WBITS if wrapped else -zlib.MAX_WBITS)
                    chunk, pending = pending, b""
                if decoder is None:
                    body.extend(chunk)
                else:
                    while chunk:
                        if decoder.eof:
                            if encoding != "gzip":
                                raise FetchError("Invalid compressed response")
                            decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
                        body.extend(decoder.decompress(chunk, self.max_bytes - len(body) + 1))
                        if len(body) > self.max_bytes:
                            raise FetchError("Response exceeds byte limit", retryable=False)
                        chunk = decoder.unused_data
                if len(body) > self.max_bytes:
                    raise FetchError("Response exceeds byte limit", retryable=False)
            if (decoder is not None and not decoder.eof) or pending or (encoding in ("gzip", "deflate") and not encoded_bytes):
                raise FetchError("Incomplete compressed response")
            return bytes(body)
        finally:
            if timer is not None:
                timer.cancel()
                timer.join()
