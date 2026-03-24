"""
Search engine module for blog search.
Handles Elasticsearch and PostgreSQL full-text search with proper query parsing.
"""

import os
import random
import re
import sys
from dataclasses import dataclass
from typing import Any

from dotenv import load_dotenv
from elasticsearch import Elasticsearch
from psycopg2 import Error, pool


@dataclass
class SearchResult:
    title: str
    url: str
    date: str | None
    text: str
    score: float | None = None


@dataclass
class SearchResponse:
    results: list[SearchResult]
    total_hits: int
    search_time_ms: int


class QueryParser:
    """Parse search queries, extracting filters and preserving quoted phrases."""

    SITE_FILTER_PATTERN = re.compile(r'\bsite:([^\s"]+)', re.IGNORECASE)
    QUOTED_PHRASE_PATTERN = re.compile(r'"([^"]+)"')

    @staticmethod
    def parse(query: str) -> tuple[str, list[str], list[str]]:
        """
        Parse query to extract:
        - Clean query text (with quoted phrases preserved)
        - Site domains from site: filters (for URL scoping in Elasticsearch)
        - Quoted phrases (for exact matching)

        Returns: (clean_query, site_domains, quoted_phrases)
        """
        if not query or not query.strip():
            return "", [], []

        site_domains: list[str] = []

        # Extract site: filters
        site_matches = QueryParser.SITE_FILTER_PATTERN.findall(query)
        for domain in site_matches:
            clean_domain = re.sub(r"[^a-zA-Z0-9.\-_]", "", domain)
            if clean_domain:
                site_domains.append(clean_domain)

        # Remove site: from query
        clean_query = QueryParser.SITE_FILTER_PATTERN.sub("", query)

        # Extract quoted phrases (reserved for future query behavior)
        quoted_phrases = QueryParser.QUOTED_PHRASE_PATTERN.findall(clean_query)

        # Normalize whitespace but preserve quotes
        clean_query = " ".join(clean_query.split()).strip()

        return clean_query, site_domains, quoted_phrases


class DatabasePool:
    """Manages PostgreSQL connection pool."""

    def __init__(self):
        self._pool: pool.ThreadedConnectionPool | None = None
        self._init_pool()

    def _init_pool(self):
        try:
            self._pool = pool.ThreadedConnectionPool(
                minconn=3,
                maxconn=10,
                host=os.getenv("PGHOST"),
                database=os.getenv("PGDATABASE"),
                user=os.getenv("PGUSER"),
                password=os.getenv("PGPASSWORD"),
                port=os.getenv("PGPORT", 5432),
                sslmode=os.getenv("PGSSLMODE", "prefer"),
                channel_binding=os.getenv("PGCHANNELBINDING", "prefer"),
                connect_timeout=10,
            )
        except (Exception, Error) as error:
            print(f"Error creating connection pool: {error}")
            sys.exit(1)

    def get_connection(self):
        if self._pool is None:
            self._init_pool()
        conn = self._pool.getconn()
        # Test connection is still alive
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
        except Exception:
            self._pool.putconn(conn, close=True)
            conn = self._pool.getconn()
        return conn

    def release(self, conn):
        if self._pool:
            self._pool.putconn(conn)

    def close(self):
        if self._pool:
            self._pool.closeall()
            self._pool = None


class SearchEngine:
    """Main search engine class supporting Elasticsearch and PostgreSQL."""

    DEFAULT_PER_PAGE = 6
    RESULTS_LIMIT = 24  # Max results for PostgreSQL fallback and latest posts

    # Drop hits below this BM25 _score (not 0–1; tune against your index). None disables.
    ES_MIN_SCORE: float | None = 1.0

    def __init__(self, use_elasticsearch: bool = True):
        load_dotenv()
        self.db = DatabasePool()
        self._init_schema()
        self.size = self._get_db_size()
        self.elasticsearch: Elasticsearch | None = None
        self._es_index = "pages"
        if use_elasticsearch:
            self._init_elasticsearch()

    def _init_schema(self):
        """Initialize database schema."""
        conn = self.db.get_connection()
        try:
            schema_path = os.path.join(os.path.dirname(__file__), "..", "db", "schema.sql")
            with open(schema_path, "r") as f:
                with conn.cursor() as cur:
                    cur.execute(f.read())
                conn.commit()
        except (Exception, Error) as error:
            print(f"Error initializing database: {error}")
            sys.exit(1)
        finally:
            self.db.release(conn)

    def _get_db_size(self) -> int:
        """Get total number of indexed pages."""
        conn = self.db.get_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) FROM pages")
                result = cursor.fetchone()
                return result[0] if result else 0
        finally:
            self.db.release(conn)

    def _init_elasticsearch(self):
        url = os.getenv("ELASTICSEARCH_URL", "http://localhost:9200")
        try:
            client = Elasticsearch(url)
            if not client.ping():
                print("Warning: Elasticsearch ping failed; search may be unavailable")
                self.elasticsearch = None
                return
            self.elasticsearch = client
        except Exception as e:
            print(f"Warning: could not connect to Elasticsearch: {e}")
            self.elasticsearch = None

    @staticmethod
    def _es_url_filters(site_domains: list[str]) -> list[dict[str, Any]]:
        """Restrict to documents whose keyword url contains each listed host."""
        return [{"wildcard": {"url": f"*{d}*"}} for d in site_domains]

    def _es_keyword_body(self, clean_query: str, site_domains: list[str]) -> dict[str, Any]:
        if clean_query:
            must: list[dict[str, Any]] = [
                {
                    "multi_match": {
                        "query": clean_query,
                        "fields": ["title^2", "text"],
                        "type": "cross_fields",
                        "operator": "and",
                    }
                }
            ]
        else:
            must = [{"match_all": {}}]
        bool_query: dict[str, Any] = {"must": must}
        if site_domains:
            bool_query["filter"] = self._es_url_filters(site_domains)
        return {"query": {"bool": bool_query}}

    def __del__(self):
        self.db.close()

    # -------------------------------------------------------------------------
    # Elasticsearch search
    # -------------------------------------------------------------------------

    def search_elasticsearch(
        self, query: str, page: int = 1, per_page: int | None = None
    ) -> dict[str, Any]:
        """Keyword search using Elasticsearch with pagination."""
        per_page = per_page or self.DEFAULT_PER_PAGE

        if not self.elasticsearch:
            self._init_elasticsearch()
            if not self.elasticsearch:
                return self._empty_response(page, per_page)

        clean_query, site_domains, _ = QueryParser.parse(query)

        if not clean_query and not site_domains:
            return self._empty_response(page, per_page)

        offset = (page - 1) * per_page
        body = self._es_keyword_body(clean_query, site_domains)
        body["from"] = offset
        body["size"] = per_page
        body["track_total_hits"] = True
        if self.ES_MIN_SCORE is not None:
            body["min_score"] = self.ES_MIN_SCORE

        try:
            raw = self.elasticsearch.search(index=self._es_index, body=body)
        except Exception as e:
            print(f"Elasticsearch search error: {e}")
            return self._empty_response(page, per_page)

        return self._format_es_response(raw, page, per_page)

    def search_elasticsearch_hybrid(
        self, query: str, page: int = 1, per_page: int | None = None
    ) -> dict[str, Any]:
        # TODO(elasticsearch): real hybrid = BM25 + sparse/dense vectors (ELSER, text_embedding
        # inference, kNN) with RRF or linear combination; index must expose vector fields.
        return self.search_elasticsearch(query, page=page, per_page=per_page)

    def _empty_response(self, page: int, per_page: int) -> dict[str, Any]:
        """Return empty response with pagination metadata."""
        return {
            "results": [],
            "results_size": 0,
            "search_time": 0,
            "page": page,
            "per_page": per_page,
            "total_pages": 0,
        }

    def _es_total_hits(self, raw: dict[str, Any]) -> int:
        total = raw.get("hits", {}).get("total", 0)
        if isinstance(total, dict):
            return int(total.get("value", 0))
        return int(total or 0)

    def _source_to_row(self, src: dict[str, Any]) -> dict[str, Any]:
        return {
            "title": src.get("title", ""),
            "url": (src.get("url") or "").rstrip("/"),
            "date": src.get("date"),
            "text": self._truncate_text(src.get("text", ""), 300),
        }

    def _format_es_response(
        self, raw: dict[str, Any], page: int, per_page: int
    ) -> dict[str, Any]:
        """Format Elasticsearch response for keyword search with pagination."""
        hits = raw.get("hits", {}).get("hits", [])
        total_hits = self._es_total_hits(raw)
        total_pages = (total_hits + per_page - 1) // per_page if total_hits > 0 else 0

        return {
            "results": [self._source_to_row(h.get("_source", {})) for h in hits],
            "results_size": total_hits,
            "search_time": raw.get("took", 0),
            "page": page,
            "per_page": per_page,
            "total_pages": total_pages,
        }

    @staticmethod
    def _truncate_text(text: str, max_words: int) -> str:
        """Truncate text to max_words while preserving word boundaries."""
        if not text:
            return ""
        words = text.split()
        if len(words) <= max_words:
            return text
        return " ".join(words[:max_words]) + "..."

    # -------------------------------------------------------------------------
    # PostgreSQL Full-Text Search (fallback)
    # -------------------------------------------------------------------------

    def search(self, query: str) -> list[dict]:
        """Full-text search using PostgreSQL tsquery."""
        conn = self.db.get_connection()
        try:
            # Normalize whitespace
            query_text = " ".join(query.split()).strip()
            if not query_text:
                return []

            with conn.cursor() as cursor:
                sql = """
                    WITH search_query AS (
                        SELECT websearch_to_tsquery('english', %s) AS q
                    ),
                    ranked AS (
                        SELECT 
                            pages.id,
                            ts_rank_cd(pages.page_tsv, search_query.q) AS score
                        FROM pages, search_query
                        WHERE pages.page_tsv @@ search_query.q
                        ORDER BY score DESC
                        LIMIT 200
                    )
                    SELECT 
                        pages.title,
                        pages.url,
                        pages.date,
                        LEFT(pages.text, 1500) AS text,
                        ranked.score
                    FROM ranked
                    JOIN pages ON pages.id = ranked.id
                    ORDER BY ranked.score DESC
                    LIMIT %s;
                """
                cursor.execute(sql, (query_text, self.RESULTS_LIMIT))
                results = cursor.fetchall()

                return [
                    {
                        "title": row[0],
                        "url": row[1].rstrip("/"),
                        "date": row[2],
                        "text": self._truncate_text(row[3], 300),
                    }
                    for row in results
                ]
        finally:
            self.db.release(conn)

    # -------------------------------------------------------------------------
    # Browse Methods
    # -------------------------------------------------------------------------

    def get_latest_posts(self, page: int = 1) -> dict[str, Any]:
        """Get most recently published posts with pagination."""
        total_pages = 5
        page = min(page, total_pages)
        per_page = self.DEFAULT_PER_PAGE
        offset = (page - 1) * per_page
        
        conn = self.db.get_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute("""
                    SELECT p.title, p.url, p.date, LEFT(p.text, 1500)
                    FROM (
                        SELECT id FROM pages 
                        WHERE date IS NOT NULL 
                        ORDER BY date DESC 
                        LIMIT %s OFFSET %s
                    ) AS ids
                    JOIN pages p ON p.id = ids.id
                    ORDER BY p.date DESC
                """, (per_page, offset))
                
                return {
                    "results": [
                        {
                            "title": row[0],
                            "url": row[1].rstrip("/"),
                            "date": row[2],
                            "text": self._truncate_text(row[3], 300),
                        }
                        for row in cursor.fetchall()
                    ],
                    "page": page,
                    "total_pages": total_pages,
                }
        finally:
            self.db.release(conn)

    def get_random_post(self) -> dict:
        """Get a random post from the database."""
        conn = self.db.get_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT MAX(id) FROM pages")
                max_id = cursor.fetchone()[0]

                if not max_id:
                    return {}

                # Try up to 10 times to find a valid random ID
                for _ in range(10):
                    random_id = random.randint(1, max_id)
                    cursor.execute(
                        "SELECT title, url, date, LEFT(text, 1500) FROM pages WHERE id >= %s LIMIT 1",
                        (random_id,),
                    )
                    row = cursor.fetchone()
                    if row:
                        return {
                            "title": row[0],
                            "url": row[1].rstrip("/"),
                            "date": row[2],
                            "text": self._truncate_text(row[3], 300),
                        }
                return {}
        finally:
            self.db.release(conn)

    # -------------------------------------------------------------------------
    # Analytics
    # -------------------------------------------------------------------------

    def log_query(self, query: str, ip_address: str, user_agent: str) -> None:
        """Log search query for analytics."""
        conn = self.db.get_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO query_logs (query, ip_address, user_agent) VALUES (%s, %s, %s)",
                    (query, ip_address, user_agent),
                )
                conn.commit()
        except Exception as e:
            print(f"Error logging query: {e}")
        finally:
            self.db.release(conn)


# -----------------------------------------------------------------------------
# CLI for testing
# -----------------------------------------------------------------------------

if __name__ == "__main__":
    engine = SearchEngine()

    try:
        while True:
            query = input("Search (or 'quit'): ")
            if query.lower() == "quit":
                break

            print("\n--- Keyword Search ---")
            response = engine.search_elasticsearch(query)
            print(f"Found {response['results_size']} results in {response['search_time']}ms")
            for r in response["results"][:5]:
                print(f"  {r['title'][:60]}... - {r['url']}")

            print("\n--- Hybrid Search ---")
            response = engine.search_elasticsearch_hybrid(query)
            print(f"Found {response['results_size']} results in {response['search_time']}ms")
            for r in response["results"][:5]:
                print(f"  {r['title'][:60]}... - {r['url']}")
            print()
    finally:
        engine.db.close()
