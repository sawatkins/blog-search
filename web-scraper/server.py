from fastapi import FastAPI, Request, Form
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from search_engine import SearchEngine

app = FastAPI()
templates = Jinja2Templates(directory="templates")
search_engine = SearchEngine(use_db=True)

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "results": None})

@app.post("/", response_class=HTMLResponse)
async def search(request: Request, query: str = Form(...)):
    results = search_engine.search(query)
    return templates.TemplateResponse("index.html", {"request": request, "results": results, "query": query})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)