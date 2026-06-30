"""Graph explorer UI route."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

router = APIRouter(prefix="/ui", tags=["ui"])

_TEMPLATES = Jinja2Templates(
    directory=str(Path(__file__).parent.parent.parent.parent / "web" / "templates")
)


@router.get("/graph", response_class=HTMLResponse)
async def graph_page(request: Request):
    return _TEMPLATES.TemplateResponse(request, "graph.html")
