import gzip
import io
import socket
import threading
import time
import unittest
import zlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import format_datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
from urllib3.response import HTTPResponse

from scraper import fetching
from scraper.fetching import FetchError, Fetcher, FetchResult, normalize_url, same_site


def response(body=b"", status=200, headers=None):
    result = requests.Response()
    result.status_code = status
    result.headers.update(headers or {})
    result.raw = HTTPResponse(body=io.BytesIO(body), preload_content=False, headers=result.headers)
    return result


class NormalizeTests(unittest.TestCase):
    def test_significant_query_encoding_order_and_trailing_slash(self):
        self.assertEqual(
            normalize_url("HTTPS://Example.COM:443/posts/?tag=a%20b&tag=c+z&utm_source=mail&empty=&flag&sig=%2f%2B#part"),
            "https://example.com/posts/?tag=a%20b&tag=c+z&empty=&flag&sig=%2f%2B",
        )
        self.assertEqual(normalize_url("http://example.com:80"), "http://example.com/")
        self.assertEqual(normalize_url("http://example.com:8080/a"), "http://example.com:8080/a")
        self.assertNotEqual(normalize_url("https://example.com/a"), normalize_url("https://example.com/a/"))
        self.assertEqual(normalize_url("https://example.com/?ref=source&page=2"), "https://example.com/?ref=source&page=2")

    def test_tracking_keys_are_removed_case_insensitively(self):
        self.assertEqual(
            normalize_url("https://example.com/?UTM_campaign=a&%75tm_source=b&fbclid=c&gclid=d&mc_eid=e&id=1"),
            "https://example.com/?id=1",
        )

    def test_idna_and_public_ip_literals(self):
        self.assertEqual(normalize_url("https://BÜCHER.de./a"), "https://xn--bcher-kva.de/a")
        self.assertEqual(normalize_url("http://8.8.8.8/"), "http://8.8.8.8/")
        self.assertEqual(normalize_url("https://[2606:4700:4700::1111]/"), "https://[2606:4700:4700::1111]/")

    def test_url_length_is_bounded_in_utf8_bytes(self):
        prefix = "https://example.com/"
        ascii_url = prefix + "a" * (2000 - len(prefix))
        self.assertEqual(normalize_url(ascii_url), ascii_url)
        self.assertIsNone(normalize_url(ascii_url + "a"))
        available_bytes = 2000 - len(prefix)
        unicode_url = prefix + "é" * (available_bytes // 2) + "a" * (available_bytes % 2)
        self.assertEqual(len(unicode_url.encode("utf-8")), 2000)
        self.assertEqual(normalize_url(unicode_url), unicode_url)
        self.assertIsNone(normalize_url(unicode_url + "é"))
        self.assertIsNone(normalize_url(prefix + "\ud800"))

    def test_url_limit_also_applies_after_idna_expansion(self):
        prefix = "https://bücher.de/"
        url = prefix + "a" * (2000 - len(prefix.encode("utf-8")))
        self.assertEqual(len(url.encode("utf-8")), 2000)
        self.assertIsNone(normalize_url(url))

    def test_invalid_and_nonpublic_urls(self):
        urls = [
            None, 5, "", "/relative", "//example.com/a", "ftp://example.com/a",
            "https://user:secret@example.com/", "https://@example.com/",
            "https://example.com:0/", "https://example.com:65536/", "https://example.com:/",
            "https://example.com:bad/", "https://[broken/", "https://exa_mple.com/",
            "https://-example.com/", "https://example-.com/", "https://example..com/",
            "http://localhost/", "http://metadata/", "http://foo.localhost/",
            "http://service.local/", "http://service.internal/", "http://router.home.arpa/",
            "http://127.0.0.1/", "http://127.1/", "http://2130706433/", "http://0x7f000001/",
            "http://0177.0.0.1/", "http://0x7f.0.0.1/", "http://10.0.0.1/",
            "http://172.16.0.1/", "http://192.168.0.1/", "http://169.254.169.254/",
            "http://100.64.0.1/", "http://0.0.0.0/", "http://224.0.0.1/",
            "http://192.0.2.1/", "http://198.18.0.1/", "http://240.0.0.1/",
            "http://[::1]/", "http://[::]/", "http://[fc00::1]/", "http://[fe80::1]/",
            "http://[::ffff:127.0.0.1]/", "http://[ff02::1]/", "http://[fe80::1%25eth0]/",
            "https://example.com\\@127.0.0.1/", " https://example.com/",
            "https://example.com/\r\nsecret", "https://example.com/a b", "https://example.com/\x00",
        ]
        for url in urls:
            with self.subTest(url=url):
                self.assertIsNone(normalize_url(url))

    def test_same_site_only_allows_www_mirror(self):
        for url in ("http://example.com/a", "https://www.example.com:8443/a", "https://EXAMPLE.COM./"):
            with self.subTest(url=url):
                self.assertTrue(same_site(url, "https://example.com/"))
                self.assertTrue(same_site("https://www.example.com/", url))
        for url in ("https://blog.example.com/", "https://example.com.evil.com/", "https://another.com/", "/relative", "http://localhost/"):
            with self.subTest(url=url):
                self.assertFalse(same_site(url, "https://example.com/"))


class FetchingTests(unittest.TestCase):
    def setUp(self):
        self.routes = {}
        self.requests = []
        self.responses = []
        self.dns = patch("scraper.fetching.socket.getaddrinfo", side_effect=self.resolve).start()
        self.send = patch("requests.adapters.HTTPAdapter.send", side_effect=self.dispatch).start()
        self.addCleanup(patch.stopall)
        self.fetcher = Fetcher(delay=0)
        self.addCleanup(self.fetcher.close)
        # Any accidental use of the real adapter must also fail before I/O.
        patch("socket.create_connection", side_effect=AssertionError("Unexpected network call")).start()

    def resolve(self, host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    def dispatch(self, request, **kwargs):
        self.requests.append((request, kwargs))
        if request.url not in self.routes:
            raise AssertionError("Unexpected HTTP request")
        route = self.routes[request.url]
        if isinstance(route, list):
            route = route.pop(0)
        if isinstance(route, Exception):
            raise route
        result = route() if callable(route) else route
        self.responses.append(result)
        return result

    def allow_robots(self, origin="https://example.com", body=b"User-agent: *\nAllow: /\n"):
        self.routes[origin + "/robots.txt"] = lambda: response(body)

    def urls(self):
        return [request.url for request, _ in self.requests]

    def test_fetch_result_defaults_and_error_defaults(self):
        self.assertEqual(FetchResult("https://example.com/", b"xml", 200).content_type, "")
        error = FetchError()
        self.assertTrue(error.retryable)
        self.assertIsNone(error.retry_after)

    def test_xml_bytes_and_conditional_headers_are_preserved(self):
        self.allow_robots()
        xml = b'<?xml version="1.0" encoding="iso-8859-1"?><feed>caf\xe9</feed>'
        self.routes["https://example.com/feed"] = response(xml, headers={
            "ETag": '"new"', "Last-Modified": "Mon, 07 Sep 2026 12:00:00 GMT",
            "Content-Type": "application/xml; charset=iso-8859-1",
        })
        result = self.fetcher.fetch("https://example.com/feed#entry", '"old"', "previous")
        self.assertEqual(result.body, xml)
        self.assertEqual(result.url, "https://example.com/feed")
        self.assertEqual(result.etag, '"new"')
        self.assertEqual(result.status, 200)
        self.assertEqual(result.last_modified, "Mon, 07 Sep 2026 12:00:00 GMT")
        self.assertEqual(result.content_type, "application/xml; charset=iso-8859-1")
        request, options = self.requests[-1]
        self.assertEqual(request.headers["If-None-Match"], '"old"')
        self.assertEqual(request.headers["If-Modified-Since"], "previous")
        self.assertEqual(request.headers["User-Agent"], self.fetcher.user_agent)
        self.assertTrue(options["stream"])
        self.assertTrue(options["verify"])
        self.assertEqual(options["proxies"], {})
        self.assertTrue(all(0 < value <= 20 for value in options["timeout"]))
        self.assertNotIn("If-None-Match", self.requests[0][0].headers)
        self.assertTrue(all(result.raw.closed for result in self.responses))

    def test_304_skips_body_and_large_content_length(self):
        self.routes["https://example.com/feed"] = response(status=304, headers={"ETag": '"same"', "Content-Length": "999999999"})
        result = self.fetcher.fetch("https://example.com/feed", check_robots=False)
        self.assertEqual(result.status, 304)
        self.assertEqual(result.body, b"")
        self.assertEqual(result.etag, '"same"')

    def test_retry_statuses_and_retry_after_seconds(self):
        for status in (408, 425, 429, 500, 502, 503, 504, 599):
            with self.subTest(status=status):
                self.routes["https://example.com/"] = response(status=status, headers={"Retry-After": "120"})
                with self.assertRaises(FetchError) as caught:
                    self.fetcher.fetch("https://example.com/", check_robots=False)
                self.assertTrue(caught.exception.retryable)
                self.assertEqual(caught.exception.retry_after, 120)
                self.assertEqual(caught.exception.status_code, status)
                self.assertEqual(str(caught.exception), f"HTTP status {status}")

    def test_nonretryable_statuses(self):
        for status in (400, 401, 403, 404, 410, 422):
            with self.subTest(status=status):
                self.routes["https://example.com/"] = response(status=status)
                with self.assertRaises(FetchError) as caught:
                    self.fetcher.fetch("https://example.com/", check_robots=False)
                self.assertFalse(caught.exception.retryable)

    def test_retry_after_http_date_past_and_invalid(self):
        now = 1_700_000_000
        for value, expected in (
            (format_datetime(datetime.fromtimestamp(now + 90, timezone.utc), usegmt=True), 90),
            (format_datetime(datetime.fromtimestamp(now - 90, timezone.utc), usegmt=True), 0),
            ("untrusted secret", None), ("-10", None), ("NaN", None), ("1.5", None),
        ):
            with self.subTest(value=value), patch("scraper.fetching.time.time", return_value=now):
                self.routes["https://example.com/"] = response(status=429, headers={"Retry-After": value})
                with self.assertRaises(FetchError) as caught:
                    self.fetcher.fetch("https://example.com/", check_robots=False)
                self.assertEqual(caught.exception.retry_after, expected)
                self.assertNotIn("untrusted", str(caught.exception))

    def test_transport_errors_are_retryable_and_sanitized(self):
        for error in (requests.Timeout("secret URL"), requests.ConnectionError("secret URL"), OSError("secret URL")):
            with self.subTest(error=type(error)):
                self.routes["https://example.com/"] = error
                with self.assertRaises(FetchError) as caught:
                    self.fetcher.fetch("https://example.com/", check_robots=False)
                self.assertTrue(caught.exception.retryable)
                self.assertNotIn("secret", str(caught.exception))
                self.assertTrue(caught.exception.__suppress_context__)

    def test_invalid_header_is_sanitized(self):
        with self.assertRaises(FetchError) as caught:
            self.fetcher.fetch("https://example.com/", etag="secret\r\nInjected: yes", check_robots=False)
        self.assertFalse(caught.exception.retryable)
        self.assertNotIn("secret", str(caught.exception))
        self.assertEqual(self.send.call_count, 0)

    def test_no_environment_authentication_or_proxies(self):
        self.routes["https://example.com/"] = response(b"ok")
        with patch.dict("os.environ", {"HTTPS_PROXY": "http://127.0.0.1:1234"}), patch("requests.sessions.get_netrc_auth") as netrc:
            self.fetcher.fetch("https://example.com/", check_robots=False)
        netrc.assert_not_called()
        self.assertNotIn("Authorization", self.requests[0][0].headers)
        self.assertEqual(self.requests[0][1]["proxies"], {})

    def test_internal_literals_never_reach_dns_or_http(self):
        for url in ("http://127.0.0.1/secret", "http://169.254.169.254/", "http://[::1]/", "http://localhost/", "https://user:secret@example.com/"):
            with self.subTest(url=url), self.assertRaises(FetchError) as caught:
                self.fetcher.fetch(url)
            self.assertFalse(caught.exception.retryable)
            self.assertNotIn("secret", str(caught.exception))
        self.assertEqual(self.send.call_count, 0)
        self.assertEqual(self.dns.call_count, 0)

    def test_all_dns_answers_must_be_public(self):
        for address in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "100.64.0.1", "::1", "fc00::1", "::ffff:192.168.1.1", "224.0.0.1"):
            with self.subTest(address=address):
                self.dns.side_effect = lambda host, port, **kw: self.resolve(host, port) + [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", (address, port))]
                with self.assertRaises(FetchError) as caught:
                    self.fetcher.fetch("https://example.com/")
                self.assertFalse(caught.exception.retryable)
        self.assertEqual(self.send.call_count, 0)

    def test_dns_errors_and_empty_answers_retry_without_leaking(self):
        for answer in (socket.gaierror("secret hostname"), []):
            self.dns.side_effect = answer if isinstance(answer, Exception) else None
            self.dns.return_value = answer
            with self.assertRaises(FetchError) as caught:
                self.fetcher.fetch("https://example.com/", check_robots=False)
            self.assertTrue(caught.exception.retryable)
            self.assertNotIn("secret", str(caught.exception))
        self.assertEqual(self.send.call_count, 0)

    def test_dns_is_rechecked_after_robots(self):
        self.allow_robots()
        self.dns.side_effect = [self.resolve("example.com", 443), [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]]
        with self.assertRaises(FetchError) as caught:
            self.fetcher.fetch("https://example.com/feed")
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(self.urls(), ["https://example.com/robots.txt"])

    def test_five_redirects_succeed_but_six_fail(self):
        self.allow_robots()
        for index in range(6):
            self.routes[f"https://example.com/{index}"] = lambda index=index: response(status=302, headers={"Location": f"/{index + 1}"})
        self.routes["https://example.com/5"] = response(b"done")
        self.assertEqual(self.fetcher.fetch("https://example.com/0").url, "https://example.com/5")
        self.requests.clear()
        self.routes["https://example.com/5"] = response(status=302, headers={"Location": "/6"})
        with self.assertRaises(FetchError) as caught:
            self.fetcher.fetch("https://example.com/0")
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(len(self.urls()), 6)
        self.assertNotIn("https://example.com/6", self.urls())

    def test_redirect_loop_is_bounded(self):
        self.routes["https://example.com/"] = lambda: response(status=301, headers={"Location": "/"})
        with self.assertRaises(FetchError):
            self.fetcher.fetch("https://example.com/", check_robots=False)
        self.assertEqual(self.send.call_count, 6)

    def test_redirect_body_is_never_consumed(self):
        redirect = response(b"unused", status=302, headers={"Location": "/next", "Content-Length": "999999999"})
        redirect.raw.read = Mock(side_effect=AssertionError("Unbounded redirect body read"))
        redirect.raw.read1 = Mock(side_effect=AssertionError("Unnecessary redirect body read"))
        self.routes["https://example.com/"] = redirect
        self.routes["https://example.com/next"] = response(b"ok")
        self.assertEqual(self.fetcher.fetch("https://example.com/", check_robots=False).body, b"ok")
        self.assertTrue(redirect.raw.closed)

    def test_redirects_to_internal_targets_are_blocked(self):
        for target in ("http://127.0.0.1/secret", "http://[::1]/", "http://169.254.169.254/", "file:///etc/passwd", "https://user:secret@example.com/"):
            with self.subTest(target=target):
                self.routes["https://example.com/"] = response(status=302, headers={"Location": target})
                with self.assertRaises(FetchError) as caught:
                    self.fetcher.fetch("https://example.com/", check_robots=False)
                self.assertFalse(caught.exception.retryable)
                self.assertNotIn("secret", str(caught.exception))
        self.assertTrue(all(url == "https://example.com/" for url in self.urls()))

    def test_redirect_target_dns_must_be_public(self):
        self.routes["https://example.com/"] = response(status=302, headers={"Location": "https://other.com/secret"})
        self.dns.side_effect = lambda host, port, **kw: self.resolve(host, port) if host == "example.com" else [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", port))]
        with self.assertRaises(FetchError) as caught:
            self.fetcher.fetch("https://example.com/", check_robots=False)
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(self.urls(), ["https://example.com/"])

    def test_redirect_host_robots_enforced_even_after_opt_out(self):
        self.routes["https://example.com/"] = response(status=302, headers={"Location": "https://other.com/private"})
        self.allow_robots("https://other.com", b"User-agent: *\nDisallow: /private\n")
        with self.assertRaises(FetchError) as caught:
            self.fetcher.fetch("https://example.com/", check_robots=False)
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(self.urls(), ["https://example.com/", "https://other.com/robots.txt"])

    def test_cross_origin_redirect_drops_validators(self):
        self.allow_robots()
        self.allow_robots("https://other.com")
        self.routes["https://example.com/feed"] = response(status=307, headers={"Location": "https://other.com/feed"})
        self.routes["https://other.com/feed"] = response(b"ok")
        self.fetcher.fetch("https://example.com/feed", '"secret"', "date")
        self.assertNotIn("If-None-Match", self.requests[-1][0].headers)
        self.assertNotIn("If-Modified-Since", self.requests[-1][0].headers)

    def test_redirect_without_location_is_permanent(self):
        self.routes["https://example.com/"] = response(status=302)
        with self.assertRaises(FetchError) as caught:
            self.fetcher.fetch("https://example.com/", check_robots=False)
        self.assertFalse(caught.exception.retryable)

    def test_robots_disallow_is_cached(self):
        self.allow_robots(body=b"User-agent: *\nDisallow: /private\n")
        for _ in range(2):
            with self.assertRaises(FetchError) as caught:
                self.fetcher.fetch("https://example.com/private/feed")
            self.assertFalse(caught.exception.retryable)
        self.assertEqual(self.urls(), ["https://example.com/robots.txt"])

    def test_robots_uses_configured_user_agent(self):
        self.allow_robots(body=b"User-agent: BlogSearchBot\nDisallow: /\n\nUser-agent: *\nAllow: /\n")
        with self.assertRaises(FetchError):
            self.fetcher.fetch("https://example.com/")

    def test_missing_robots_is_allowed_and_cached(self):
        for status in (404, 410):
            with self.subTest(status=status):
                fetcher = Fetcher(delay=0)
                self.routes["https://example.com/robots.txt"] = response(status=status)
                self.routes["https://example.com/"] = lambda: response(b"ok")
                self.assertEqual(fetcher.fetch("https://example.com/").body, b"ok")
                self.assertEqual(fetcher.fetch("https://example.com/").body, b"ok")
        self.assertEqual(self.urls().count("https://example.com/robots.txt"), 2)

    def test_robots_auth_denial_fails_closed(self):
        for status in (401, 403):
            self.routes["https://example.com/robots.txt"] = response(status=status)
            with self.assertRaises(FetchError) as caught:
                Fetcher(delay=0).fetch("https://example.com/")
            self.assertFalse(caught.exception.retryable)
        self.assertTrue(all(url.endswith("/robots.txt") for url in self.urls()))

    def test_robots_transport_and_retry_statuses_fail_closed_and_are_not_cached(self):
        for failure in (requests.Timeout("secret"), 429, 500, 503):
            with self.subTest(failure=failure):
                fetcher = Fetcher(delay=0)
                failed = failure if isinstance(failure, Exception) else response(status=failure, headers={"Retry-After": "45"})
                self.routes["https://example.com/robots.txt"] = [failed, response(b"User-agent: *\nAllow: /\n")]
                self.routes["https://example.com/"] = response(b"ok")
                with self.assertRaises(FetchError) as caught:
                    fetcher.fetch("https://example.com/")
                self.assertTrue(caught.exception.retryable)
                self.assertNotIn("secret", str(caught.exception))
                if isinstance(failure, int):
                    self.assertEqual(caught.exception.retry_after, 45)
                self.assertEqual(fetcher.fetch("https://example.com/").body, b"ok")

    def test_robots_ttl_and_sitemaps(self):
        self.allow_robots(body=b"\xef\xbb\xbfUser-agent: *\nAllow: /\nSitemap: https://example.com/map.xml?part=2&utm_source=x\nSitemap: https://example.com/map.xml?part=2\nSitemap: http://127.0.0.1/secret\n")
        with patch("scraper.fetching.time.monotonic", return_value=100):
            self.assertEqual(self.fetcher.robots_sitemaps("https://example.com/post"), ["https://example.com/map.xml?part=2"])
        with patch("scraper.fetching.time.monotonic", return_value=100 + fetching.ROBOTS_TTL - 1):
            self.fetcher.robots_sitemaps("https://example.com/other")
        self.assertEqual(self.send.call_count, 1)
        with patch("scraper.fetching.time.monotonic", return_value=100 + fetching.ROBOTS_TTL + 1):
            self.fetcher.robots_sitemaps("https://example.com/other")
        self.assertEqual(self.send.call_count, 2)

    def test_robots_sitemaps_rejects_invalid_input(self):
        with self.assertRaises(FetchError) as caught:
            self.fetcher.robots_sitemaps("http://localhost/secret")
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(self.send.call_count, 0)

    def test_robots_redirect_is_bounded_and_checks_internal_target(self):
        self.routes["https://example.com/robots.txt"] = lambda: response(status=302, headers={"Location": "/robots.txt"})
        with self.assertRaises(FetchError):
            self.fetcher.fetch("https://example.com/")
        self.assertEqual(self.send.call_count, 6)
        self.routes["https://example.com/robots.txt"] = response(status=302, headers={"Location": "http://127.0.0.1/robots.txt"})
        with self.assertRaises(FetchError) as caught:
            self.fetcher.fetch("https://example.com/")
        self.assertFalse(caught.exception.retryable)

    def test_cross_host_robots_redirect_respects_destination_policy(self):
        self.routes["https://example.com/robots.txt"] = response(status=302, headers={"Location": "https://other.com/policy.txt"})
        self.allow_robots("https://other.com", b"User-agent: *\nDisallow: /policy.txt\n")
        with self.assertRaises(FetchError) as caught:
            self.fetcher.fetch("https://example.com/")
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(self.urls(), ["https://example.com/robots.txt", "https://other.com/robots.txt"])

    def test_canonical_cross_host_robots_redirect_bootstraps_policy(self):
        self.routes["https://example.com/robots.txt"] = response(status=301, headers={"Location": "https://www.example.com/robots.txt"})
        self.allow_robots("https://www.example.com", b"User-agent: *\nDisallow: /\n")
        with self.assertRaises(FetchError) as caught:
            self.fetcher.fetch("https://example.com/")
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(self.send.call_count, 2)

    def test_circular_cross_origin_robots_lookup_fails_without_deadlock(self):
        self.routes["https://example.com/robots.txt"] = response(status=302, headers={"Location": "https://other.com/policy"})
        self.routes["https://other.com/robots.txt"] = response(status=302, headers={"Location": "https://example.com/policy"})
        with self.assertRaises(FetchError) as caught:
            self.fetcher.fetch("https://example.com/")
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(self.send.call_count, 2)

    def test_content_length_and_streaming_size_limits(self):
        for headers, body in (({"Content-Length": "101"}, b"x"), ({}, b"x" * 101)):
            with self.subTest(headers=headers):
                self.routes["https://example.com/"] = response(body, headers=headers)
                with self.assertRaises(FetchError) as caught:
                    Fetcher(delay=0, max_bytes=100).fetch("https://example.com/", check_robots=False)
                self.assertFalse(caught.exception.retryable)
                self.assertTrue(self.responses[-1].raw.closed)
        self.routes["https://example.com/"] = response(b"x" * 100)
        self.assertEqual(len(Fetcher(delay=0, max_bytes=100).fetch("https://example.com/", check_robots=False).body), 100)

    def test_invalid_content_length_is_sanitized(self):
        for length in ("secret", "-1"):
            self.routes["https://example.com/"] = response(headers={"Content-Length": length})
            with self.assertRaises(FetchError) as caught:
                self.fetcher.fetch("https://example.com/", check_robots=False)
            self.assertNotIn("secret", str(caught.exception))

    def test_gzip_deflate_and_concatenated_gzip_preserve_bytes(self):
        xml = b'<?xml version="1.0" encoding="iso-8859-1"?><feed>caf\xe9</feed>'
        compressed = [
            ("gzip", gzip.compress(xml)),
            ("gzip", gzip.compress(xml[:10]) + gzip.compress(xml[10:])),
            ("deflate", zlib.compress(xml)),
            ("deflate", zlib.compress(xml)[2:-4]),
        ]
        for encoding, body in compressed:
            with self.subTest(encoding=encoding, size=len(body)):
                self.routes["https://example.com/"] = response(body, headers={"Content-Encoding": encoding})
                self.assertEqual(self.fetcher.fetch("https://example.com/", check_robots=False).body, xml)

    def test_decompressed_and_compressed_limits_are_independent(self):
        for body in (gzip.compress(b"x" * 100_000), gzip.compress(b"x" * 60) + gzip.compress(b"x" * 60)):
            self.routes["https://example.com/"] = response(body, headers={"Content-Encoding": "gzip"})
            with self.assertRaises(FetchError) as caught:
                Fetcher(delay=0, max_bytes=100).fetch("https://example.com/", check_robots=False)
            self.assertFalse(caught.exception.retryable)
        self.routes["https://example.com/"] = response(gzip.compress(b""), headers={"Content-Encoding": "gzip"})
        with self.assertRaises(FetchError) as caught:
            Fetcher(delay=0, max_bytes=10).fetch("https://example.com/", check_robots=False)
        self.assertFalse(caught.exception.retryable)

    def test_invalid_truncated_and_unsupported_compression(self):
        for encoding, body in (("gzip", b"secret"), ("gzip", gzip.compress(b"xml")[:-3]), ("gzip", b""), ("deflate", b"x"), ("br", b"secret")):
            with self.subTest(encoding=encoding, body=body):
                self.routes["https://example.com/"] = response(body, headers={"Content-Encoding": encoding})
                with self.assertRaises(FetchError) as caught:
                    self.fetcher.fetch("https://example.com/", check_robots=False)
                self.assertNotIn("secret", str(caught.exception))

    def test_robots_body_uses_same_size_limit(self):
        self.allow_robots(body=b"x" * 101)
        with self.assertRaises(FetchError) as caught:
            Fetcher(delay=0, max_bytes=100).fetch("https://example.com/")
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(self.send.call_count, 1)

    def test_crawl_delay_and_request_rate_are_respected(self):
        for directive, expected in ((b"Crawl-delay: 7", 7), (b"Request-rate: 1/9", 9)):
            with self.subTest(directive=directive):
                now = [100.0]
                self.allow_robots(body=b"User-agent: *\n" + directive + b"\nAllow: /\n")
                self.routes["https://example.com/"] = response(b"ok")
                with patch("scraper.fetching.time.monotonic", side_effect=lambda: now[0]), patch("scraper.fetching.time.sleep", side_effect=lambda duration: now.__setitem__(0, now[0] + duration)) as sleep:
                    Fetcher(delay=2).fetch("https://example.com/")
                sleep.assert_called_once_with(expected)

    def test_host_spacing_and_robots_cache_across_threads(self):
        self.allow_robots()
        self.routes["https://example.com/"] = lambda: response(b"ok")
        fetcher = Fetcher(delay=0.015)
        starts = []
        original_dispatch = self.dispatch

        def dispatch(request, **kwargs):
            starts.append(time.monotonic())
            return original_dispatch(request, **kwargs)

        self.send.side_effect = dispatch
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda _: fetcher.fetch("https://example.com/"), range(4)))
        self.assertTrue(all(result.body == b"ok" for result in results))
        self.assertEqual(self.urls().count("https://example.com/robots.txt"), 1)
        self.assertTrue(all(right - left >= 0.012 for left, right in zip(starts, starts[1:])))

    def test_delay_is_shared_across_schemes_and_ports_but_not_other_hosts(self):
        now = [100.0]
        for url in ("https://example.com/", "http://example.com:8080/", "https://other.com/"):
            self.routes[url] = response(b"ok")
        with patch("scraper.fetching.time.monotonic", side_effect=lambda: now[0]), patch("scraper.fetching.time.sleep", side_effect=lambda duration: now.__setitem__(0, now[0] + duration)) as sleep:
            fetcher = Fetcher(delay=2)
            for url in self.routes:
                fetcher.fetch(url, check_robots=False)
        sleep.assert_called_once_with(2)

    def test_excessive_crawl_delay_returns_retry_after_without_sleeping(self):
        self.allow_robots(body=b"User-agent: *\nCrawl-delay: 120\nAllow: /\n")
        with patch("scraper.fetching.time.monotonic", return_value=100), patch("scraper.fetching.time.sleep") as sleep:
            with self.assertRaises(FetchError) as caught:
                self.fetcher.fetch("https://example.com/")
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(caught.exception.retry_after, 120)
        sleep.assert_not_called()

    def test_body_deadline_cannot_be_extended_by_small_chunks(self):
        now = [100.0]
        result = response()

        def read1(amount, decode_content=False):
            now[0] += 11
            return b"x"

        result.raw.read1 = Mock(side_effect=read1)
        self.routes["https://example.com/"] = result
        with patch("scraper.fetching.time.monotonic", side_effect=lambda: now[0]):
            with self.assertRaises(FetchError) as caught:
                self.fetcher.fetch("https://example.com/", check_robots=False)
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(result.raw.read1.call_count, 6)
        self.assertTrue(result.raw.closed)

    def test_deadline_is_shared_by_robots_and_redirects(self):
        now = [100.0]

        def slow_response():
            now[0] += 21
            return response(status=302, headers={"Location": "/next"})

        self.routes["https://example.com/robots.txt"] = slow_response
        self.routes["https://example.com/next"] = slow_response
        with patch("scraper.fetching.time.monotonic", side_effect=lambda: now[0]):
            with self.assertRaises(FetchError) as caught:
                self.fetcher.fetch("https://example.com/")
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(self.send.call_count, 3)

    def test_socket_watchdog_interrupts_blocked_body_reader(self):
        # Model a read blocked inside HTTP chunk framing and the CPython socket
        # chain used by urllib3. The watchdog must actively interrupt the read.
        interrupted = threading.Event()
        raw_socket = SimpleNamespace(shutdown=Mock(side_effect=lambda how: interrupted.set()))
        result = response()
        result.raw._fp.fp = SimpleNamespace(raw=SimpleNamespace(_sock=raw_socket))

        def blocked_read(amount, decode_content=False):
            if not interrupted.wait(1):
                self.fail("Watchdog did not interrupt the body reader")
            return b""

        result.raw.read1 = blocked_read
        self.routes["https://example.com/"] = result
        started = time.monotonic()
        with patch("scraper.fetching.MAX_SECONDS", 0.08):
            with self.assertRaises(FetchError) as caught:
                self.fetcher.fetch("https://example.com/", check_robots=False)
        self.assertTrue(caught.exception.retryable)
        self.assertLess(time.monotonic() - started, 0.5)
        raw_socket.shutdown.assert_called_once_with(socket.SHUT_RDWR)


if __name__ == "__main__":
    unittest.main()
