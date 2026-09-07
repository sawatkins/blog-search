import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Query, HTTPException, BackgroundTasks
from fastapi.concurrency import run_in_threadpool
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    PlainTextResponse,
    JSONResponse,
)
from psycopg2 import Error as DatabaseError

if __package__:
    from .search_engine import BackendUnavailable, SearchEngine
else:
    from search_engine import BackendUnavailable, SearchEngine


logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    engine = await run_in_threadpool(SearchEngine)
    app.state.search_engine = engine
    try:
        yield
    finally:
        try:
            await run_in_threadpool(engine.close)
        finally:
            app.state.search_engine = None


app = FastAPI(lifespan=lifespan)
templates_path = os.path.join(os.path.dirname(__file__), "templates")
templates = Jinja2Templates(directory=templates_path)
static_path = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=static_path), name="static")


@app.exception_handler(BackendUnavailable)
@app.exception_handler(DatabaseError)
async def backend_unavailable(request: Request, error: Exception):
    logger.error("Search backend unavailable", exc_info=error)
    return JSONResponse(
        status_code=503,
        content={"detail": "Search service temporarily unavailable"},
    )


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    search_engine = request.app.state.search_engine
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"posts_size": search_engine.size},
    )


@app.get("/about", response_class=HTMLResponse)
def api(request: Request):
    search_engine = request.app.state.search_engine
    return templates.TemplateResponse(
        request=request,
        name="about.html",
        context={"posts_size": search_engine.size},
    )


@app.get("/api", response_class=HTMLResponse)
def about(request: Request):
    search_engine = request.app.state.search_engine
    return templates.TemplateResponse(
        request=request,
        name="api.html",
        context={"posts_size": search_engine.size},
    )


@app.get("/bot", response_class=HTMLResponse)
def bot(request: Request):
    search_engine = request.app.state.search_engine
    return templates.TemplateResponse(
        request=request,
        name="bot.html",
        context={"posts_size": search_engine.size},
    )


@app.get("/search", response_class=HTMLResponse)
def search_page(
    request: Request,
    background_tasks: BackgroundTasks,
    q: str = Query(None),
    page: int = Query(1, ge=1),
):
    search_engine = request.app.state.search_engine
    query = q.strip() if q else ""
    results = []
    search_time = 0

    if not query:
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={"posts_size": search_engine.size},
        )

    background_tasks.add_task(
        search_engine.log_query,
        query=query,
        ip_address=request.headers.get(
            "X-Forwarded-For", request.client.host if request.client else ""
        ),
        user_agent=request.headers.get("user-agent", ""),
    )

    use_postgres = request.query_params.get("use_postgres", "false").lower() == "true"
    search_mode = request.query_params.get("search_mode", "keyword").lower()
    if use_postgres:
        start_time = time.time()
        results = search_engine.search(query)
        end_time = time.time()
        search_time = round(end_time - start_time, 2)
        response = {
            "results_size": len(results),
            "page": 1,
            "per_page": len(results),
            "total_pages": 1,
        }
    else:
        if search_mode == "keyword":
            response = search_engine.search_elasticsearch(query, page=page)
        else:
            response = search_engine.search_elasticsearch_hybrid(query, page=page)
        search_time = round(response.get("search_time", 0) / 1000, 2)
        results = response.get("results", [])

    return templates.TemplateResponse(
        request=request,
        name="search.html",
        context={
            "results": results,
            "query": query,
            "time": search_time,
            "results_size": response.get("results_size", len(results)),
            "page": response.get("page", 1),
            "total_pages": response.get("total_pages", 1),
            "posts_size": search_engine.size,
        },
    )


@app.get("/latest", response_class=HTMLResponse)
def latest(request: Request, page: int = Query(1, ge=1)):
    search_engine = request.app.state.search_engine
    start_time = time.time()
    response = search_engine.get_latest_posts(page=page)
    search_time = round(time.time() - start_time, 2)
    return templates.TemplateResponse(
        request=request,
        name="search.html",
        context={
            "results": response["results"],
            "query": "",
            "time": search_time,
            "page": response["page"],
            "total_pages": response["total_pages"],
            "posts_size": search_engine.size,
            "is_latest": True,
        },
    )


@app.get("/random", response_class=HTMLResponse)
def random(request: Request):
    search_engine = request.app.state.search_engine
    start_time = time.time()
    result = search_engine.get_random_post()
    search_time = round(time.time() - start_time, 2)
    return templates.TemplateResponse(
        request=request,
        name="search.html",
        context={
            "results": [result] if result else [],
            "query": "",
            "time": search_time,
            "page": 1,
            "total_pages": 1,
            "posts_size": search_engine.size,
            "is_random": True,
        },
    )


@app.get("/api/search", response_class=JSONResponse)
def api_search(request: Request, q: str = Query(...), page: int = Query(1, ge=1)):
    search_engine = request.app.state.search_engine
    query = q.strip() if q else None
    if not query:
        return JSONResponse({"results": [], "total": 0, "page": 1, "total_pages": 0})

    try:
        response = search_engine.search_elasticsearch(query, page=page)
        return JSONResponse({
            "results": response.get("results", []),
            "total": response.get("results_size", 0),
            "page": response.get("page", 1),
            "total_pages": response.get("total_pages", 0),
        })
    except (BackendUnavailable, DatabaseError):
        raise
    except Exception:
        logger.exception("Search API request failed")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/robots.txt", response_class=PlainTextResponse)
async def robots():
    return """User-agent: *
Disallow: /search
Disallow: /api/search
Disallow: /latest
Disallow: /random
"""


@app.get("/favicon.ico")
async def favicon():
    return FileResponse(os.path.join(static_path, "favicon.ico"))


if __name__ == "__main__":
    import uvicorn  # type: ignore

    uvicorn.run(app, host="0.0.0.0", port=8000)
