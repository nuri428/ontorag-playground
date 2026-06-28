"""ontorag-playground FastAPI application.

Domain-neutral: no movie-specific code anywhere in engine/.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi_mcp import FastApiMCP

from engine.api.routes import chat, graph, ingest, ui

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
