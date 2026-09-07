"""Backend regressions using fake DB/ES modules and in-process ASGI requests.

Run with: .venv/bin/python -m unittest discover -s tests -p test_web_backend.py
"""

import importlib.util
import inspect
import json
import os
from pathlib import Path
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, PropertyMock, patch
from urllib.parse import urlencode


WEB_PATH = Path(__file__).resolve().parents[1] / "web"


def load_backend_modules():
    """Never import a production DB/ES client, even if import behavior regresses."""
    database = ModuleType("psycopg2")
    database.Error = type("DatabaseError", (Exception,), {})
    database.pool = SimpleNamespace(
        ThreadedConnectionPool=Mock(side_effect=AssertionError("Unexpected DB init"))
    )
    elasticsearch = ModuleType("elasticsearch")
    elasticsearch.Elasticsearch = Mock(side_effect=AssertionError("Unexpected ES init"))
    dotenv = ModuleType("dotenv")
    dotenv.load_dotenv = Mock(side_effect=AssertionError("Unexpected dotenv load"))

    modules = {"psycopg2": database, "elasticsearch": elasticsearch, "dotenv": dotenv}
    names = (*modules, "search_engine", "server")
    originals = {name: sys.modules.get(name) for name in names}
    try:
        sys.modules.update(modules)
        loaded = []
        for name in ("search_engine", "server"):
            spec = importlib.util.spec_from_file_location(name, WEB_PATH / f"{name}.py")
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
            loaded.append(module)
    finally:
        # Restore only our stubs. Unloading Jinja's newly imported modules would
        # split its shared "missing" sentinel and corrupt real template renders.
        for name, original in originals.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original
    return loaded


search_engine, server = load_backend_modules()


class ImportSafetyTests(unittest.TestCase):
    def test_server_import_does_not_initialize_backends_or_load_environment(self):
        engine_module, server_module = load_backend_modules()
        engine_module.pool.ThreadedConnectionPool.assert_not_called()
        engine_module.Elasticsearch.assert_not_called()
        engine_module.load_dotenv.assert_not_called()
        self.assertFalse(hasattr(server_module.app.state, "search_engine"))


class DatabasePoolTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.factory_patch = patch.object(search_engine.pool, "ThreadedConnectionPool")
        self.factory = self.factory_patch.start()
        self.addCleanup(self.factory_patch.stop)
        self.raw_pool = self.factory.return_value
        self.database = search_engine.DatabasePool()
        self.addCleanup(self.database.close)

    @staticmethod
    def connection():
        connection = MagicMock()
        connection.closed = False
        return connection

    def test_pool_defaults_are_small_and_configurable(self):
        self.assertEqual(self.factory.call_args.kwargs["minconn"], 1)
        self.assertEqual(self.factory.call_args.kwargs["maxconn"], 10)
        with patch.dict(os.environ, {"WEB_DB_POOL_MIN": "2", "WEB_DB_POOL_MAX": "8"}):
            database = search_engine.DatabasePool()
            self.addCleanup(database.close)
        self.assertEqual(self.factory.call_args.kwargs["minconn"], 2)
        self.assertEqual(self.factory.call_args.kwargs["maxconn"], 8)

    def test_invalid_pool_bounds_fail_before_connecting(self):
        for minimum, maximum in (("0", "10"), ("4", "3"), ("1", "33"), ("bad", "10")):
            with self.subTest(minimum=minimum, maximum=maximum):
                self.factory.reset_mock()
                with patch.dict(os.environ, {"WEB_DB_POOL_MIN": minimum, "WEB_DB_POOL_MAX": maximum}):
                    with self.assertRaises(ValueError):
                        search_engine.DatabasePool()
                self.factory.assert_not_called()

    def test_initial_connection_failure_raises_instead_of_exiting(self):
        self.factory.side_effect = RuntimeError("private connection details")
        with self.assertRaises(search_engine.BackendUnavailable):
            search_engine.DatabasePool()

    def test_dead_connection_is_discarded_and_replacement_validated(self):
        dead, healthy = self.connection(), self.connection()
        dead.cursor.side_effect = RuntimeError("connection lost")
        self.raw_pool.getconn.side_effect = [dead, healthy]
        self.assertIs(self.database.get_connection(), healthy)
        self.raw_pool.putconn.assert_called_once_with(dead, close=True)
        healthy.cursor.return_value.__enter__.return_value.execute.assert_called_once_with("SELECT 1")
        healthy.rollback.assert_called_once_with()

    def test_failed_replacement_is_also_discarded(self):
        dead, replacement = self.connection(), self.connection()
        dead.cursor.side_effect = RuntimeError("connection lost")
        replacement.rollback.side_effect = RuntimeError("connection lost during validation")
        self.raw_pool.getconn.side_effect = [dead, replacement]
        with self.assertRaises(search_engine.BackendUnavailable):
            self.database.get_connection()
        self.assertEqual(self.raw_pool.getconn.call_count, 2)
        self.assertEqual(self.raw_pool.putconn.call_count, 2)
        self.raw_pool.putconn.assert_any_call(dead, close=True)
        self.raw_pool.putconn.assert_any_call(replacement, close=True)

    def test_exhaustion_and_reconnection_failure_are_backend_errors(self):
        self.raw_pool.getconn.side_effect = RuntimeError("pool exhausted")
        with self.assertRaises(search_engine.BackendUnavailable):
            self.database.get_connection()
        dead = self.connection()
        dead.cursor.side_effect = RuntimeError("disconnected")
        self.raw_pool.getconn.side_effect = [dead, RuntimeError("cannot reconnect")]
        with self.assertRaises(search_engine.BackendUnavailable):
            self.database.get_connection()
        self.raw_pool.putconn.assert_called_once_with(dead, close=True)

    def test_release_rolls_back_before_returning_connection(self):
        connection = self.connection()
        calls = []
        connection.rollback.side_effect = lambda: calls.append("rollback")
        self.raw_pool.putconn.side_effect = lambda *args, **kwargs: calls.append("release")
        self.database.release(connection)
        self.assertEqual(calls, ["rollback", "release"])
        self.raw_pool.putconn.assert_called_once_with(connection, close=False)

    def test_release_discards_closed_or_unrecoverable_connections(self):
        for closed in (False, True):
            with self.subTest(closed=closed):
                connection = self.connection()
                connection.closed = closed
                connection.rollback.side_effect = RuntimeError("rollback failed")
                self.database.release(connection)
                self.raw_pool.putconn.assert_called_with(connection, close=True)
                if closed:
                    connection.rollback.assert_not_called()

    def test_close_is_idempotent_and_does_not_reopen_pool(self):
        self.database.close()
        self.database.close()
        self.raw_pool.closeall.assert_called_once_with()
        with self.assertRaises(search_engine.BackendUnavailable):
            self.database.get_connection()
        self.assertEqual(self.factory.call_count, 1)


class SearchEngineTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        for attribute in ("DatabasePool", "Elasticsearch", "load_dotenv"):
            patcher = patch.object(search_engine, attribute)
            setattr(self, attribute, patcher.start())
            self.addCleanup(patcher.stop)
        self.engine = search_engine.SearchEngine()
        self.database = self.DatabasePool.return_value
        self.client = self.Elasticsearch.return_value
        self.connection = self.database.get_connection.return_value
        self.cursor = self.connection.cursor.return_value.__enter__.return_value
        self.addCleanup(self.engine.close)

    def test_initialization_does_not_execute_schema_or_query_database(self):
        self.database.get_connection.assert_not_called()
        self.client.ping.assert_not_called()

    def test_latest_order_matches_index_and_breaks_date_ties_consistently(self):
        self.cursor.fetchall.return_value = []
        self.engine.get_latest_posts(page=2)
        query, parameters = self.cursor.execute.call_args.args
        self.assertIn('ORDER BY date DESC NULLS LAST, id', query)
        self.assertIn('ORDER BY p.date DESC NULLS LAST, p.id', query)
        self.assertEqual(parameters, (self.engine.DEFAULT_PER_PAGE, self.engine.DEFAULT_PER_PAGE))

    def test_elasticsearch_defaults_to_pages_without_authentication(self):
        self.Elasticsearch.assert_called_once_with(
            "http://localhost:9200", request_timeout=10
        )
        self.client.search.return_value = {"hits": {"total": 0, "hits": []}}
        self.engine.search_elasticsearch("hello")
        self.assertEqual(self.client.search.call_args.kwargs["index"], "pages")

    def test_elasticsearch_uses_configured_index_and_api_key(self):
        with patch.dict(os.environ, {
            "ELASTICSEARCH_URL": "https://search.example.test:9200",
            "ELASTICSEARCH_INDEX": "custom-pages",
            "ELASTICSEARCH_API_KEY": "test-api-key",
        }):
            engine = search_engine.SearchEngine()
            self.addCleanup(engine.close)
        self.Elasticsearch.assert_called_with(
            "https://search.example.test:9200",
            request_timeout=10,
            api_key="test-api-key",
        )
        self.client.search.return_value = {"hits": {"total": 0, "hits": []}}
        for search in (engine.search_elasticsearch, engine.search_elasticsearch_hybrid):
            search("hello")
            self.assertEqual(self.client.search.call_args.kwargs["index"], "custom-pages")

    def test_empty_elasticsearch_api_key_omits_authentication(self):
        with patch.dict(os.environ, {"ELASTICSEARCH_API_KEY": ""}):
            engine = search_engine.SearchEngine()
            self.addCleanup(engine.close)
        self.Elasticsearch.assert_called_with(
            "http://localhost:9200", request_timeout=10
        )

    def test_close_releases_both_clients_once(self):
        self.engine.close()
        self.engine.close()
        self.client.close.assert_called_once_with()
        self.database.close.assert_called_once_with()

    def test_database_closes_even_when_es_cleanup_fails(self):
        self.client.close.side_effect = RuntimeError("close failed")
        with self.assertRaises(RuntimeError):
            self.engine.close()
        self.database.close.assert_called_once_with()

    def test_partial_initialization_closes_database(self):
        self.Elasticsearch.side_effect = RuntimeError("invalid ES URL")
        with self.assertRaises(search_engine.BackendUnavailable):
            search_engine.SearchEngine()
        self.database.close.assert_called_once_with()

    def test_outage_raises_and_same_es_client_can_recover(self):
        self.client.search.side_effect = [
            RuntimeError("private ES address"),
            {"hits": {"total": {"value": 0}, "hits": []}, "took": 1},
        ]
        with self.assertRaises(search_engine.BackendUnavailable):
            self.engine.search_elasticsearch("hello")
        response = self.engine.search_elasticsearch("hello")
        self.assertEqual(response["results_size"], 0)
        self.assertEqual(response["results"], [])
        self.assertEqual(self.Elasticsearch.call_count, 1)

    def test_blank_query_does_not_contact_es(self):
        self.assertEqual(self.engine.search_elasticsearch("  ")["results"], [])
        self.client.search.assert_not_called()

    def test_all_search_and_browse_paths_preserve_urls(self):
        for url in ("https://example.com/", "https://example.com/post/", "https://example.com/post?next=/"):
            with self.subTest(url=url):
                row = ("Title", url, None, "Text")
                self.cursor.fetchall.return_value = [row]
                self.assertEqual(self.engine.search("hello")[0]["url"], url)
                self.assertEqual(self.engine.get_latest_posts()["results"][0]["url"], url)
                self.cursor.fetchone.side_effect = [(1,), row]
                self.assertEqual(self.engine.get_random_post()["url"], url)
                self.client.search.return_value = {
                    "hits": {"total": {"value": 1}, "hits": [{"_source": {"url": url}}]}
                }
                self.assertEqual(self.engine.search_elasticsearch("hello")["results"][0]["url"], url)

    def test_failed_read_still_releases_connection(self):
        self.cursor.execute.side_effect = RuntimeError("query failed")
        for operation in (lambda: self.engine.search("hello"), self.engine.get_latest_posts, self.engine.get_random_post, self.engine._get_db_size):
            with self.subTest(operation=operation):
                self.database.release.reset_mock()
                with self.assertRaises(RuntimeError):
                    operation()
                self.database.release.assert_called_once_with(self.connection)

    def test_count_refreshes_after_ttl_and_retains_stale_count_on_failure(self):
        with patch.object(search_engine.time, "monotonic") as clock, patch.object(self.engine, "_get_db_size") as count:
            count.side_effect = [12, RuntimeError("database down"), 19, 0]
            clock.return_value = 100
            self.assertEqual(self.engine.size, 12)
            clock.return_value = 100 + self.engine.SIZE_CACHE_TTL - 1
            self.assertEqual(self.engine.size, 12)
            self.assertEqual(count.call_count, 1)
            clock.return_value += 1
            with self.assertLogs(search_engine.logger, level="ERROR"):
                self.assertEqual(self.engine.size, 12)
            self.assertEqual(self.engine.size, 12)
            self.assertEqual(count.call_count, 2)
            clock.return_value += self.engine.SIZE_RETRY_TTL
            self.assertEqual(self.engine.size, 19)
            clock.return_value += self.engine.SIZE_CACHE_TTL
            self.assertEqual(self.engine.size, 0)

    def test_initial_count_failure_is_throttled(self):
        with patch.object(self.engine, "_get_db_size", side_effect=RuntimeError("offline")) as count:
            with self.assertLogs(search_engine.logger, level="ERROR"):
                self.assertEqual(self.engine.size, 0)
            self.assertEqual(self.engine.size, 0)
            count.assert_called_once_with()

    def test_concurrent_count_refresh_serves_stale_value_without_duplicate_query(self):
        started, finish = threading.Event(), threading.Event()
        self.engine._size = 7

        def refresh():
            started.set()
            if not finish.wait(timeout=5):
                raise AssertionError("refresh did not finish")
            return 13

        with patch.object(self.engine, "_get_db_size", side_effect=refresh) as count:
            with ThreadPoolExecutor(max_workers=1) as workers:
                future = workers.submit(lambda: self.engine.size)
                try:
                    self.assertTrue(started.wait(timeout=5))
                    self.assertEqual(self.engine.size, 7)
                    count.assert_called_once_with()
                finally:
                    finish.set()
                self.assertEqual(future.result(timeout=5), 13)

    def test_analytics_checkout_and_insert_failures_do_not_escape(self):
        self.database.get_connection.side_effect = RuntimeError("pool exhausted")
        with self.assertLogs(search_engine.logger, level="ERROR"):
            self.engine.log_query("query", "127.0.0.1", "agent")
        self.database.release.assert_not_called()
        self.database.get_connection.side_effect = None
        self.cursor.execute.side_effect = RuntimeError("insert failed")
        with self.assertLogs(search_engine.logger, level="ERROR"):
            self.engine.log_query("query", "127.0.0.1", "agent")
        self.database.release.assert_called_once_with(self.connection)
        self.connection.commit.assert_not_called()


async def request_app(path, **params):
    """Exercise real FastAPI routing without a socket, server, or HTTP client."""
    messages = []
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "GET", "scheme": "http", "path": path,
        "raw_path": path.encode(), "query_string": urlencode(params).encode(),
        "root_path": "", "headers": [], "client": None,
        "server": ("testserver", 80),
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    await server.app(scope, receive, send)
    status = next(message["status"] for message in messages if message["type"] == "http.response.start")
    body = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
    return status, body


class ServerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = Mock(size=42)
        self.engine.search_elasticsearch.return_value = {
            "results": [{"title": "Title", "url": "https://example.com/post/"}],
            "results_size": 1, "page": 1, "total_pages": 1,
        }
        server.app.state.search_engine = self.engine
        self.addCleanup(setattr, server.app.state, "search_engine", None)

    async def test_lifespan_initialization_and_cleanup_run_off_event_loop(self):
        event_loop_thread = threading.get_ident()
        threads = []

        def initialize():
            threads.append(threading.get_ident())
            return self.engine

        self.engine.close.side_effect = lambda: threads.append(threading.get_ident())
        with patch.object(server, "SearchEngine", side_effect=initialize) as factory:
            async with server.app.router.lifespan_context(server.app):
                self.assertIs(server.app.state.search_engine, self.engine)
                self.engine.close.assert_not_called()
            factory.assert_called_once_with()
        self.engine.close.assert_called_once_with()
        self.assertIsNone(server.app.state.search_engine)
        self.assertEqual(len(threads), 2)
        self.assertTrue(all(thread != event_loop_thread for thread in threads))

    async def test_lifespan_cleans_up_when_serving_fails(self):
        with patch.object(server, "SearchEngine", return_value=self.engine):
            with self.assertRaisesRegex(RuntimeError, "serving failed"):
                async with server.app.router.lifespan_context(server.app):
                    raise RuntimeError("serving failed")
        self.engine.close.assert_called_once_with()
        self.assertIsNone(server.app.state.search_engine)

    async def test_backend_routes_are_sync_and_execute_in_worker_threads(self):
        event_loop_thread = threading.get_ident()
        threads = []

        def count():
            threads.append(threading.get_ident())
            return 42

        type(self.engine).size = PropertyMock(side_effect=count)
        self.engine.search.return_value = []
        self.engine.get_latest_posts.return_value = {"results": [], "page": 1, "total_pages": 5}
        self.engine.get_random_post.return_value = {}
        routes = ("/", "/about", "/api", "/bot", "/search", "/latest", "/random", "/api/search")
        for route in server.app.routes:
            if route.path in routes:
                self.assertFalse(inspect.iscoroutinefunction(route.endpoint), route.path)
        response = {"results": [], "page": 1, "total_pages": 0}

        def search(*args, **kwargs):
            threads.append(threading.get_ident())
            return response

        self.engine.search_elasticsearch.side_effect = search
        with patch.object(
            server.templates,
            "TemplateResponse",
            side_effect=lambda *args, **kwargs: server.HTMLResponse("ok"),
        ):
            for path in routes:
                with self.subTest(path=path):
                    status, _ = await request_app(path, q="hello")
                    self.assertEqual(status, 200)
        self.assertGreaterEqual(len(threads), len(routes))
        self.assertTrue(all(thread != event_loop_thread for thread in threads))

    async def test_real_templates_render_home_and_information_pages(self):
        for path in ("/", "/search", "/about", "/api", "/bot"):
            with self.subTest(path=path):
                status, body = await request_app(path)
                self.assertEqual(status, 200)
                self.assertIn(b"<!DOCTYPE html>", body)
                if path in ("/", "/search"):
                    self.assertIn(b"42 pages indexed", body)
        self.engine.search_elasticsearch.assert_not_called()
        self.engine.log_query.assert_not_called()

    async def test_real_templates_render_search_latest_and_random_results(self):
        post = {
            "title": "Template render post",
            "url": "https://example.com/post/",
            "date": "2026-09-07",
            "text": "Text from the fake backend.",
        }
        self.engine.search_elasticsearch.return_value = {
            "results": [post], "results_size": 1, "search_time": 12,
            "page": 1, "total_pages": 1,
        }
        self.engine.get_latest_posts.return_value = {
            "results": [post], "page": 2, "total_pages": 5,
        }
        self.engine.get_random_post.return_value = post
        scenarios = (
            ("/search", {"q": "hello"}, b"1 results (0.01s)"),
            ("/latest", {"page": 2}, b"Latest Posts"),
            ("/random", {}, b"Random Post"),
        )
        for path, params, marker in scenarios:
            with self.subTest(path=path):
                status, body = await request_app(path, **params)
                self.assertEqual(status, 200)
                for content in (
                    b"<!DOCTYPE html>", marker, post["title"].encode(),
                    post["text"].encode(), post["date"].encode(),
                    b'href="https://example.com/post/"',
                ):
                    self.assertIn(content, body)
                if path == "/latest":
                    self.assertIn(b"page 2 of 5", body)
                    self.assertIn(b'href="/latest?page=3"', body)
        self.engine.search_elasticsearch.assert_called_once_with("hello", page=1)
        self.engine.get_latest_posts.assert_called_once_with(page=2)
        self.engine.get_random_post.assert_called_once_with()
        self.engine.log_query.assert_called_once_with(
            query="hello", ip_address="", user_agent=""
        )

    async def test_es_outages_return_sanitized_503_for_api_and_html(self):
        error = search_engine.BackendUnavailable("secret backend address")
        self.engine.search_elasticsearch.side_effect = error
        self.engine.search_elasticsearch_hybrid.side_effect = error
        for path, params in (("/api/search", {}), ("/search", {}), ("/search", {"search_mode": "hybrid"})):
            with self.subTest(path=path, params=params):
                with self.assertLogs(server.logger, level="ERROR"):
                    status, body = await request_app(path, q="hello", **params)
                self.assertEqual(status, 503)
                self.assertEqual(json.loads(body), {"detail": "Search service temporarily unavailable"})
                self.assertNotIn(b"secret", body)

    async def test_database_outages_return_sanitized_503(self):
        self.engine.get_latest_posts.side_effect = server.DatabaseError("private DB error")
        with self.assertLogs(server.logger, level="ERROR"):
            status, body = await request_app("/latest")
        self.assertEqual(status, 503)
        self.assertNotIn(b"private", body)

    async def test_api_database_errors_reach_the_existing_503_handler(self):
        self.engine.search_elasticsearch.side_effect = server.DatabaseError("private DB error")
        with self.assertLogs(server.logger, level="ERROR"):
            status, body = await request_app("/api/search", q="hello")
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body), {"detail": "Search service temporarily unavailable"})
        self.engine.search_elasticsearch.assert_called_once_with("hello", page=1)
        self.engine.search.assert_not_called()

    async def test_unexpected_api_errors_do_not_leak_details(self):
        self.engine.search_elasticsearch.side_effect = RuntimeError("secret password")
        with self.assertLogs(server.logger, level="ERROR"):
            status, body = await request_app("/api/search", q="hello")
        self.assertEqual(status, 500)
        self.assertEqual(json.loads(body), {"detail": "Internal server error"})

    async def test_api_keeps_success_contract_and_meaningful_slash(self):
        status, body = await request_app("/api/search", q=" hello ", page=2)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {
            "results": [{"title": "Title", "url": "https://example.com/post/"}],
            "total": 1, "page": 1, "total_pages": 1,
        })
        self.engine.search_elasticsearch.assert_called_once_with("hello", page=2)

    async def test_blank_and_invalid_requests_do_not_contact_backends(self):
        status, body = await request_app("/api/search", q="   ")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["results"], [])
        for params in ({}, {"q": "hello", "page": 0}):
            status, _ = await request_app("/api/search", **params)
            self.assertEqual(status, 422)
        self.engine.search_elasticsearch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
