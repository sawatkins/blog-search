import time
from fastapi import FastAPI, Request, Form, Query, HTTPException 
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

@app.post("/search", response_class=HTMLResponse)
async def search(request: Request, query: str = Form(...)):
    if not query.strip():
        return PlainTextResponse("Please enter a search query.")
    
    start_time = time.time()
    results = search_engine.search(query)
    end_time = time.time()
    
    return templates.TemplateResponse("search_results.html", {
        "request": request,
        "results": results,
        "query": query,
        "time": round(end_time - start_time, 2)
    })

@app.get("/api/search", response_class=JSONResponse)
async def api_search(q: str = Query(...)):
    #TODO: checck if q is actual text (ex. quotes will pass as valid input)
    if not q.strip():
        return JSONResponse({"results": []})
    
    try:
        results = search_engine.search(q)
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
Disallow: /
"""

@app.get("/favicon.ico")
async def favicon():
    return FileResponse(os.path.join(static_path, "favicon.ico"))

if __name__ == "__main__":
    import uvicorn # type: ignore
    uvicorn.run(app, host="0.0.0.0", port=8000)
    # uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=False)
    # uvicorn.run("web.server:app", host="0.0.0.0", port=8000, reload=True)