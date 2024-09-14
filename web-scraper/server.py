from fastapi import FastAPI, Request, Form
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from search_engine import SearchEngine
import time

app = FastAPI()
templates = Jinja2Templates(directory="templates")
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
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)