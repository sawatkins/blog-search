"""
Search engine module for blog search.
Handles Meilisearch and PostgreSQL full-text search with proper query parsing.
"""

import os
import random
import re
import sys
from dataclasses import dataclass
from typing import Any

from dotenv import load_dotenv
from meilisearch import Client as MeiliSearch
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
        - Site filters
        - Quoted phrases (for exact matching)
        
        Returns: (clean_query, filters, quoted_phrases)
        """
        if not query or not query.strip():
            return "", [], []

        filters = []
        quoted_phrases = []

        # Extract site: filters
        site_matches = QueryParser.SITE_FILTER_PATTERN.findall(query)
        for domain in site_matches:
            clean_domain = re.sub(r"[^a-zA-Z0-9.\-_]", "", domain)
            if clean_domain:
                filters.append(f'domain = "{clean_domain}"')

        # Remove site: from query
        clean_query = QueryParser.SITE_FILTER_PATTERN.sub("", query)

        # Extract quoted phrases for reference (keep them in query for Meilisearch)
        quoted_phrases = QueryParser.QUOTED_PHRASE_PATTERN.findall(clean_query)

        # Normalize whitespace but preserve quotes
        clean_query = " ".join(clean_query.split()).strip()

        return clean_query, filters, quoted_phrases


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
    """Main search engine class supporting Meilisearch and PostgreSQL."""

    DEFAULT_PER_PAGE = 6
    HYBRID_FETCH_LIMIT = 100  # Fetch more to dedupe by URL
    RESULTS_LIMIT = 24  # Max results for PostgreSQL fallback and latest posts

    def __init__(self, use_meilisearch: bool = True):
        load_dotenv()
        self.db = DatabasePool()
        self._init_schema()
        self.size = self._get_db_size()
        self.meilisearch: MeiliSearch | None = None
        if use_meilisearch:
            self._init_meilisearch()

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

    def _init_meilisearch(self):
        """Initialize Meilisearch client."""
        url = os.getenv("MEILISEARCH_URL")
        key = os.getenv("MEILISEARCH_API_KEY")
        if url and key:
            self.meilisearch = MeiliSearch(url=url, api_key=key)

    def __del__(self):
        self.db.close()

    # -------------------------------------------------------------------------
    # Meilisearch Search Methods
    # -------------------------------------------------------------------------

    def search_meilisearch(
        self, query: str, page: int = 1, per_page: int | None = None
    ) -> dict[str, Any]:
        """Keyword search using Meilisearch with pagination."""
        per_page = per_page or self.DEFAULT_PER_PAGE
        
        if not self.meilisearch:
            self._init_meilisearch()
            if not self.meilisearch:
                return self._empty_response(page, per_page)

        clean_query, filters, _ = QueryParser.parse(query)

        if not clean_query and not filters:
            return self._empty_response(page, per_page)

        offset = (page - 1) * per_page
        
        search_params = {
            "limit": per_page,
            "offset": offset,
            "matchingStrategy": "all",  # All words must match for better precision
            "attributesToSearchOn": ["title", "text"],
        }

        if filters:
            search_params["filter"] = " AND ".join(filters)

        results = self.meilisearch.index("pages").search(clean_query, search_params)
        return self._format_response(results, page, per_page)

    def search_meilisearch_hybrid(
        self, query: str, page: int = 1, per_page: int | None = None
    ) -> dict[str, Any]:
        """Hybrid semantic + keyword search using Meilisearch with pagination."""
        per_page = per_page or self.DEFAULT_PER_PAGE
        
        if not self.meilisearch:
            self._init_meilisearch()
            if not self.meilisearch:
                return self._empty_response(page, per_page)

        clean_query, filters, quoted_phrases = QueryParser.parse(query)

        if not clean_query and not filters:
            return self._empty_response(page, per_page)

        # Adjust semantic ratio based on query type
        # More semantic for natural language, more keyword for exact phrases
        semantic_ratio = 0.5 if quoted_phrases else 0.7

        # For hybrid, we fetch more to dedupe by URL, then paginate
        search_params = {
            "limit": self.HYBRID_FETCH_LIMIT,
            "hybrid": {
                "semanticRatio": semantic_ratio,
                "embedder": "default",
            },
            "rankingScoreThreshold": 0.6,  # Filter out low-relevance results
            "attributesToSearchOn": ["title", "text"],
        }

        if filters:
            search_params["filter"] = " AND ".join(filters)

        results = self.meilisearch.index("pages").search(clean_query, search_params)
        return self._format_grouped_response(results, page, per_page)

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

    def _format_response(
        self, results: dict[str, Any], page: int, per_page: int
    ) -> dict[str, Any]:
        """Format Meilisearch response for keyword search with pagination."""
        hits = results.get("hits", [])
        total_hits = results.get("estimatedTotalHits", 0)
        total_pages = (total_hits + per_page - 1) // per_page if total_hits > 0 else 0
        
        return {
            "results": [
                {
                    "title": hit.get("title", ""),
                    "url": hit.get("url", "").rstrip("/"),
                    "date": hit.get("date"),
                    "text": self._truncate_text(hit.get("text", ""), 300),
                }
                for hit in hits
            ],
            "results_size": total_hits,
            "search_time": results.get("processingTimeMs", 0),
            "page": page,
            "per_page": per_page,
            "total_pages": total_pages,
        }

    def _format_grouped_response(
        self, results: dict[str, Any], page: int, per_page: int
    ) -> dict[str, Any]:
        """Format and dedupe results by URL for hybrid search, then paginate."""
        hits = results.get("hits", [])
        seen_urls = set()
        all_deduped = []

        # First dedupe all results
        for hit in hits:
            url = hit.get("url", "").rstrip("/")
            if url in seen_urls:
                continue

            seen_urls.add(url)
            all_deduped.append({
                "title": hit.get("title", ""),
                "url": url,
                "date": hit.get("date"),
                "text": self._truncate_text(hit.get("text", ""), 300),
            })

        # Calculate pagination on deduped results
        total_deduped = len(all_deduped)
        total_pages = (total_deduped + per_page - 1) // per_page if total_deduped > 0 else 0
        
        # Slice for current page
        start_idx = (page - 1) * per_page
        end_idx = start_idx + per_page
        page_results = all_deduped[start_idx:end_idx]

        return {
            "results": page_results,
            "results_size": total_deduped,
            "search_time": results.get("processingTimeMs", 0),
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

    def get_latest_posts(self) -> list[dict]:
        """Get most recently published posts."""
        conn = self.db.get_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute("""
                    SELECT title, url, date, LEFT(text, 1500) 
                    FROM pages 
                    WHERE date IS NOT NULL 
                    ORDER BY date DESC 
                    LIMIT %s
                """, (self.RESULTS_LIMIT,))
                return [
                    {
                        "title": row[0],
                        "url": row[1].rstrip("/"),
                        "date": row[2],
                        "text": self._truncate_text(row[3], 300),
                    }
                    for row in cursor.fetchall()
                ]
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
            response = engine.search_meilisearch(query)
            print(f"Found {response['results_size']} results in {response['search_time']}ms")
            for r in response["results"][:5]:
                print(f"  {r['title'][:60]}... - {r['url']}")

            print("\n--- Hybrid Search ---")
            response = engine.search_meilisearch_hybrid(query)
            print(f"Found {response['results_size']} results in {response['search_time']}ms")
            for r in response["results"][:5]:
                print(f"  {r['title'][:60]}... - {r['url']}")
            print()
    finally:
        engine.db.close()
