import time
from fastapi import FastAPI, Request, Form # type: ignore
from fastapi.templating import Jinja2Templates # type: ignore
from fastapi.responses import HTMLResponse # type: ignore
from search_engine import SearchEngine
import os

app = FastAPI()
templates_path = os.path.join(os.path.dirname(__file__), "templates")
templates = Jinja2Templates(directory=templates_path)
search_engine = SearchEngine()

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "results": None, "posts_size": search_engine.posts_size})

@app.post("/", response_class=HTMLResponse)
async def search(request: Request, query: str = Form(...)):
    if not query.strip():
        return templates.TemplateResponse("index.html", {"request": request, "results": None, "query": "", "error": "Please enter a search query."})
    
    start_time = time.time()
    results = search_engine.search(query)
    end_time = time.time()
    return templates.TemplateResponse("index.html", {"request": request, "results": results, "query": query, "time": round(end_time - start_time, 2)})

if __name__ == "__main__":
    import uvicorn # type: ignore
    uvicorn.run(app, host="0.0.0.0", port=8000)
    # uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=False)
    # uvicorn.run("web.server:app", host="0.0.0.0", port=8000, reload=True)