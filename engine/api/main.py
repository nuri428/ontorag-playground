"""ontorag-playground FastAPI application.

Domain-neutral: no domain-specific code anywhere in engine/.
"""
from __future__ import annotations

import logging

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI  # noqa: E402
from fastapi.responses import RedirectResponse  # noqa: E402
from fastapi_mcp import FastApiMCP  # noqa: E402

from engine.api.routes import chat, graph, ingest, ui  # noqa: E402

logger = logging.getLogger(__name__)

app = FastAPI(
    title="ontorag-playground",
    description="Domain-neutral ontology chatbot. Swap domains/ to change domain.",
    version="0.1.0",
)

app.include_router(chat.router)
app.include_router(graph.router)
app.include_router(ingest.router)
app.include_router(ui.router)

mcp = FastApiMCP(app)
mcp.mount()


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse("/ui/chat")


@app.get("/health", tags=["meta"])
async def health():
    return {"status": "ok"}
