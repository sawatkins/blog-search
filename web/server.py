import time
from fastapi import FastAPI, Request, Form, Query, HTTPException, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    PlainTextResponse,
    JSONResponse,
)
from search_engine import SearchEngine
import os

app = FastAPI()
templates_path = os.path.join(os.path.dirname(__file__), "templates")
templates = Jinja2Templates(directory=templates_path)
static_path = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=static_path), name="static")
search_engine = SearchEngine()


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse(
        "index.html", {"request": request, "posts_size": search_engine.size}
    )


@app.get("/about", response_class=HTMLResponse)
async def api(request: Request):
    return templates.TemplateResponse(
        "about.html",
        {
            "request": request,
        },
    )


@app.get("/privacy", response_class=HTMLResponse)
async def privacy(request: Request):
    return templates.TemplateResponse(
        "privacy.html",
        {
            "request": request,
        },
    )


@app.get("/api", response_class=HTMLResponse)
async def about(request: Request):
    return templates.TemplateResponse(
        "api.html",
        {
            "request": request,
        },
    )


@app.get("/bot", response_class=HTMLResponse)
async def bot(request: Request):
    return templates.TemplateResponse(
        "bot.html",
        {
            "request": request,
        },
    )


@app.get("/search", response_class=HTMLResponse)
async def search_page(
    request: Request, background_tasks: BackgroundTasks, q: str = Query(None)
):
    query = q.strip() if q else ""
    results = []
    search_time = 0

    if not query:
        return templates.TemplateResponse(
            "index.html", {"request": request, "posts_size": search_engine.size}
        )

    background_tasks.add_task(
        search_engine.log_query,
        query=query,
        ip_address=request.headers.get("X-Forwarded-For", request.client.host),
        user_agent=request.headers.get("user-agent", ""),
    )

    use_postgres = request.query_params.get("use_postgres", "false").lower() == "true"
    search_mode = request.query_params.get("search_mode", "keyword").lower()
    if use_postgres:
        start_time = time.time()
        results = search_engine.search(query)
        end_time = time.time()
        search_time = round(end_time - start_time, 2)
        response = {"results_size": len(results)}
    else:
        if search_mode == "keyword":
            response = search_engine.search_meilisearch(query)
        else:
            response = search_engine.search_meilisearch_hybrid(query)
        search_time = round(response.get("search_time", 0) / 1000, 2)
        results = response.get("results", [])

    return templates.TemplateResponse(
        "search.html",
        {
            "request": request,
            "results": results,
            "query": query,
            "time": search_time,
            "results_size": response.get("results_size", len(results)),
        },
    )


@app.get("/latest", response_class=HTMLResponse)
async def latest(request: Request):
    start_time = time.time()
    results = search_engine.get_latest_posts()
    end_time = time.time()
    search_time = round(end_time - start_time, 2)
    return templates.TemplateResponse(
        "search.html",
        {
            "request": request,
            "results": results,
            "query": "",
            "time": search_time,
            "results_size": len(results),
        },
    )


@app.get("/random", response_class=HTMLResponse)
async def random(request: Request):
    start_time = time.time()
    result = search_engine.get_random_post()
    end_time = time.time()
    search_time = round(end_time - start_time, 2)
    return templates.TemplateResponse(
        "search.html",
        {
            "request": request,
            "results": [result] if result else [],
            "query": "",
            "time": search_time,
            "results_size": 1 if result else 0,
        },
    )


@app.get("/api/search", response_class=JSONResponse)
async def api_search(q: str = Query(...)):
    query = q.strip() if q else None
    if not query:
        return JSONResponse({"results": []})

    try:
        results = search_engine.search(query)
        api_results = []
        for result in results[:24]:
            api_results.append(
                {
                    "url": result["url"] if "url" in result else "",
                    "title": result["title"] if "title" in result else "",
                    "date": result["date"] if "date" in result else "",
                    "snippet": result["text"][:100] if "text" in result else "",
                }
            )
        return JSONResponse({"results": api_results})
    except Exception as e:
        print(f"An error occurred: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal server error: " + str(e))


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
