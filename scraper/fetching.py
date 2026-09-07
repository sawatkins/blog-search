"""Bounded, robots-aware fetching without converting XML bytes to text.

DNS is checked immediately before every request, including redirects, and all
answers must be public. The socket connects only to those checked addresses,
without resolving again; HTTPS still authenticates the original hostname. The
60-second deadline covers waiting, redirects and streaming bodies; requests'
socket timeouts cannot strictly bound OS DNS resolution or trickled headers.
"""

import ipaddress
import math
import re
import socket
import sys
import threading
import time
import zlib
from dataclasses import dataclass
from datetime import timezone
from email.utils import parsedate_to_datetime
from urllib.parse import unquote_plus, urljoin, urlsplit, urlunsplit

import requests
from protego import Protego
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.exceptions import HTTPError, NewConnectionError

from scraper.http_cache import HttpCache


MAX_SECONDS = 60.0
MAX_REDIRECTS = 5
MAX_URL_BYTES = 2000
ROBOTS_TTL = 3600.0
# Avoid overflow from malicious headers, without shortening realistic delays.
MAX_COOLDOWN = 100 * 366 * 86400.0
TRACKING_PARAMS = {"fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid", "igshid", "_ga", "_gl"}


class FetchError(Exception):
    def __init__(self, message="Fetch failed", retryable: bool = True, retry_after: float | None = None, *, status_code=None, host=None, cooldown=False, failure_kind=None):
        super().__init__(message)
        self.retryable = bool(retryable)
        self.retry_after = retry_after
        self.status_code = status_code
        self.host = host
        self.cooldown = cooldown
        self.failure_kind = failure_kind


@dataclass
class FetchResult:
    url: str
    body: bytes
    status: int
    etag: str | None = None
    last_modified: str | None = None
    content_type: str = ""
    robots_header: str = ""


class _PinnedSocket:
    """Replace only TCP dialing, leaving urllib3's HTTP/TLS behavior intact."""

    def __init__(self, *args, pinned_addresses, **kwargs):
        self.pinned_addresses = pinned_addresses
        super().__init__(*args, **kwargs)

    def _new_conn(self):
        deadline = time.monotonic() + self.timeout
        for family, kind, protocol, _, address in self.pinned_addresses:
            sock = None
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                sock = socket.socket(family, kind, protocol)
                sock.settimeout(remaining)
                for option in self.socket_options or ():
                    sock.setsockopt(*option)
                # address is the numeric sockaddr returned by the checked DNS
                # lookup. Do not call create_connection/getaddrinfo here.
                sock.connect(address)
                sys.audit('http.client.connect', self, self.host, self.port)
                return sock
            except OSError:
                if sock is not None:
                    sock.close()
        raise NewConnectionError(self, 'Could not connect to a checked public address')


class _PinnedHTTPConnection(_PinnedSocket, HTTPConnection):
    pass


class _PinnedHTTPSConnection(_PinnedSocket, HTTPSConnection):
    pass


class _PinnedAdapter(requests.adapters.HTTPAdapter):
    """One adapter per request, so no pool or DNS state crosses destinations."""

    def __init__(self, addresses):
        self.addresses = tuple(addresses)
        super().__init__(max_retries=0)

    def get_connection_with_tls_context(self, request, verify, proxies=None, cert=None):
        if proxies:
            raise FetchError('Crawler proxies are not supported', retryable=False)
        pool = super().get_connection_with_tls_context(request, verify, proxies={}, cert=cert)
        pool.ConnectionCls = _PinnedHTTPSConnection if request.url.startswith('https:') else _PinnedHTTPConnection
        pool.conn_kw['pinned_addresses'] = self.addresses
        return pool


def _public_ip(address):
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if isinstance(ip, ipaddress.IPv6Address) and (
        ip.sixtofour is not None or ip.teredo is not None
        or ip in ipaddress.ip_network('64:ff9b::/96')
        or ip in ipaddress.ip_network('64:ff9b:1::/48')
    ):
        return False  # Transition/NAT64 addresses can tunnel into private IPv4.
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
            return float(min(int(value), int(MAX_COOLDOWN)))
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return min(MAX_COOLDOWN, max(0.0, date.timestamp() - time.time()))
    except (ValueError, TypeError, OverflowError):
        return None


class Fetcher:
    def __init__(self, user_agent="BlogSearchBot/2.0 (+https://blogsearch.io/bot)", delay=5.0, max_bytes=5_000_000, timeout=20, cooldowns=None, cache_path=None):
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
        self._cooldowns = {host: time.monotonic() + seconds for host, seconds in (cooldowns or {}).items()}
        self._cache = HttpCache(cache_path) if cache_path else None
        if self._cache:
            self._last_request = {host: time.monotonic() - max(0, time.time() - requested)
                                  for host, requested in self._cache.recent_requests().items()}

    def close(self):
        """Close the local cache after workers drain; sessions close per request."""
        if self._cache:
            self._cache.close()
            self._cache = None

    def _lock_for(self, kind, key):
        with self._guard:
            return self._locks.setdefault((kind, key), threading.Lock())

    def fetch(self, url, etag=None, last_modified=None, check_robots=True, validator_url=None) -> FetchResult:
        headers = {}
        if etag is not None:
            headers["If-None-Match"] = etag
        if last_modified is not None:
            headers["If-Modified-Since"] = last_modified
        return self._fetch(url, headers, time.monotonic() + MAX_SECONDS, check_robots,
                           validator_url=normalize_url(validator_url or url))

    def robots_sitemaps(self, url) -> list[str]:
        url = normalize_url(url)
        if url is None:
            raise FetchError("Invalid or non-public URL", retryable=False)
        policy = self._robots(url, time.monotonic() + MAX_SECONDS, frozenset())
        # The sitemap itself is DNS-checked, with robots enforced, when fetched.
        return list(dict.fromkeys(
            normalized for value in policy.sitemaps
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
            if self._cache:
                saved = self._cache.robots(origin)
                if saved:
                    policy = Protego.parse(saved[0])
                    self._robots_cache[origin] = (time.monotonic() + max(0, saved[1] - time.time()), policy)
                    return policy
            result = self._fetch(
                origin + "/robots.txt", {}, deadline, False,
                robots_origin=origin, loading=loading | {origin},
            )
            if result.status in (404, 410):
                policy_text = ''
            elif result.status in (401, 403):
                policy_text = 'User-agent: *\nDisallow: /\n'
            else:
                policy_text = result.body.decode('utf-8-sig', errors='replace')
            policy = Protego.parse(policy_text)
            # Keep long-delay policies long enough for the deferred post to use
            # them on a later run, still within the normal 24-hour cache ceiling.
            policy_delay = policy.crawl_delay(self.user_agent) or 0
            rate = policy.request_rate(self.user_agent)
            if rate and rate.requests > 0:
                policy_delay = max(policy_delay, rate.seconds / rate.requests)
            ttl = min(86400, max(ROBOTS_TTL, 2 * policy_delay))
            if self._cache:
                self._cache.save_robots(origin, policy_text, ttl)
            self._robots_cache[origin] = (time.monotonic() + ttl, policy)
            return policy
        except FetchError as error:
            if error.retryable and not error.cooldown:
                # A failed robots lookup affects the whole site, not just this
                # post. Do not hit a broken robots endpoint for every queued URL.
                error.retry_after = max(300, error.retry_after or 0)
                error.host = error.host or parts.hostname
                self._cooldowns[error.host] = time.monotonic() + error.retry_after
                error.cooldown = True
            raise
        finally:
            lock.release()

    def _fetch(self, url, headers, deadline, check_robots, robots_origin=None, loading=frozenset(), validator_url=None):
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
                if not policy.can_fetch(url, self.user_agent):
                    raise FetchError("Blocked by robots policy", retryable=False, failure_kind='robots_denied')
                delay = max(delay, policy.crawl_delay(self.user_agent) or 0)
                rate = policy.request_rate(self.user_agent)
                if rate and rate.requests > 0:
                    delay = max(delay, rate.seconds / rate.requests)
                if delay >= 86400:
                    raise FetchError('Robots delay exceeds the supported one-day window', retryable=False)
            # Validators identify one representation, not an origin or redirect
            # link. Send them only when we reach their previously saved URL.
            request_headers = headers if url == validator_url else {}
            result, location = self._request(url, request_headers, deadline, delay, robots_origin is not None)
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
                raise FetchError("DNS resolution failed", host=parts.hostname, failure_kind='network_error')
            if any(not _public_ip(address[4][0]) for address in addresses):
                raise FetchError("Non-public network target", retryable=False)
            return addresses
        except (OSError, ValueError):
            raise FetchError("DNS resolution failed", host=parts.hostname, failure_kind='network_error') from None

    def _request(self, url, headers, deadline, delay, robots):
        hostname = urlsplit(url).hostname
        received_response = False
        lock = self._lock_for("host", hostname)
        if not lock.acquire(timeout=_remaining(deadline)):
            raise FetchError("Host wait deadline exceeded")
        try:
            cooldown = self._cooldowns.get(hostname, 0) - time.monotonic()
            if cooldown > 0:
                raise FetchError("Host cooling down", retry_after=cooldown, host=hostname, cooldown=True)
            wait = max(0.0, self._last_request.get(hostname, -math.inf) + delay - time.monotonic())
            if wait >= _remaining(deadline):
                raise FetchError("Host delay exceeds fetch deadline", retry_after=wait, host=hostname)
            if wait:
                time.sleep(wait)
            addresses = self._validate_dns(url)
            remaining = _remaining(deadline)
            self._last_request[hostname] = time.monotonic()
            if self._cache:
                self._cache.requested(hostname)
            with requests.Session() as session:
                # Neither environment proxies nor .netrc credentials may route
                # or authenticate these untrusted public URLs.
                session.trust_env = False
                session.mount(urlsplit(url).scheme + '://', _PinnedAdapter(addresses))
                request = session.prepare_request(requests.Request(
                    "GET", url,
                    headers={"User-Agent": self.user_agent, "Accept-Encoding": "gzip, deflate", **headers},
                ))
                # Session.send/get consumes redirect bodies even with
                # allow_redirects=False. The adapter sends exactly one
                # request, leaving every response body under our size limit.
                with session.get_adapter(url).send(
                    request, stream=True, verify=True, proxies={},
                    timeout=(min(self.timeout, remaining / 2), min(self.timeout, remaining / 2)),
                ) as response:
                    received_response = True
                    _remaining(deadline)
                    status = response.status_code
                    result = FetchResult(
                        url=url, body=b"", status=status,
                        etag=response.headers.get("ETag"),
                        last_modified=response.headers.get("Last-Modified"),
                        content_type=response.headers.get("Content-Type", ""),
                        robots_header=response.headers.get("X-Robots-Tag", ""),
                    )
                    if status in (301, 302, 303, 307, 308):
                        location = response.headers.get("Location")
                        if not location:
                            raise FetchError("Redirect missing destination", retryable=False)
                        return result, location
                    if status == 304 or (robots and status in (401, 403, 404, 410)):
                        if status == 304 and (robots or not headers):
                            raise FetchError("Unexpected not-modified response without a conditional request")
                        return result, None
                    if not 200 <= status < 300:
                        retry_after = _retry_after(response.headers.get("Retry-After"))
                        if retry_after is not None or status == 429:
                            self._cooldowns[hostname] = time.monotonic() + max(60, retry_after or 0)
                        raise FetchError(
                            f"HTTP status {status}",
                            retryable=status in (408, 425, 429) or 500 <= status < 600,
                            retry_after=retry_after,
                            status_code=status,
                            host=hostname,
                        )
                    result.body = self._body(response, deadline)
                    return result, None
        except FetchError as error:
            if received_response and error.status_code is None:
                error.status_code = response.status_code
            raise
        except requests.exceptions.InvalidHeader:
            raise FetchError("Invalid request headers", retryable=False) from None
        except requests.exceptions.SSLError:
            # A certificate/TLS configuration problem is unlikely to heal during
            # a short retry burst. Do not downgrade to HTTP or disable verification.
            raise FetchError('TLS/certificate verification failed', host=hostname,
                             failure_kind='tls_error') from None
        except (requests.RequestException, HTTPError, OSError, ValueError, zlib.error) as error:
            # A corrupt body is a site problem, not evidence that this machine
            # cannot connect. Only transport failures before headers count.
            transport_error = (isinstance(error, (requests.ConnectionError, requests.Timeout, HTTPError))
                               or isinstance(error, OSError) and not isinstance(error, requests.RequestException))
            raise FetchError("Network request or response decoding failed", host=hostname,
                             status_code=response.status_code if received_response else None,
                             failure_kind='network_error' if transport_error and not received_response else None) from None
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
