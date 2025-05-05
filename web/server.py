import time
from fastapi import FastAPI, Request, Form, Query, HTTPException, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates 
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, JSONResponse  
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
    return templates.TemplateResponse("index.html", {
        "request": request,
        "posts_size": search_engine.size
    })

@app.get("/about", response_class=HTMLResponse)
async def api(request: Request):
    return templates.TemplateResponse("about.html", {
        "request": request,
    })

@app.get("/api", response_class=HTMLResponse)
async def about(request: Request):
    return templates.TemplateResponse("api.html", {
        "request": request,
    })

@app.get("/search", response_class=HTMLResponse)
async def search_page(request: Request, background_tasks: BackgroundTasks, q: str = Query(None)):
    query = q.strip() if q else ""
    results = []
    search_time = 0

    if query:
        background_tasks.add_task(
            search_engine.log_query,
            query=query,
            ip_address=request.headers.get("X-Forwarded-For", request.client.host),
            user_agent=request.headers.get("user-agent", "")
        )
        
        start_time = time.time()
        results = search_engine.search(query)
        end_time = time.time()
        search_time = round(end_time - start_time, 2)
    
    return templates.TemplateResponse("search.html", {
        "request": request,
        "results": results,
        "query": query,
        "time": search_time
    })

@app.get("/api/search", response_class=JSONResponse)
async def api_search(q: str = Query(...)):
    query = q.strip() if q else None 
    if not query:
        return JSONResponse({"results": []})
    
    try:
        results = search_engine.search(query)
        api_results = []
        for result in results[:5]:  # Limit to 5 results
            api_results.append({
                "url": result['url'] if 'url' in result else '',
                "title": result['title'] if 'title' in result else '',
                "date": result['date'] if 'date' in result else '',
                "snippet": result['text'][:100] if 'text' in result else ''
            })
        return JSONResponse({"results": api_results})
    except Exception as e:
        print(f"An error occurred: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal server error: " + str(e))

@app.get("/robots.txt", response_class=PlainTextResponse)
async def robots():
    return """
User-agent: *
Disallow: 
"""

@app.get("/favicon.ico")
async def favicon():
    return FileResponse(os.path.join(static_path, "favicon.ico"))

if __name__ == "__main__":
    import uvicorn # type: ignore
    uvicorn.run(app, host="0.0.0.0", port=8000)
