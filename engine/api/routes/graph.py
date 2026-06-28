"""Graph visualization endpoint — Cytoscape.js-compatible JSON.

Stage 2 done: answer nodes highlighted in live graph view.

API 확인된 메서드:
  store.traverse(start_uri, max_depth, ...) → TraversalResult(.nodes, .edges)
  store.find_path(uri_a, uri_b, ...) → TraversalResult(.nodes, .edges)
  TraversalResult.nodes: list[dict] — {"uri", "label", "depth", ...}
  TraversalResult.edges: list[dict] — {"from", "to", "predicate", ...}
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Query

from ontorag.stores.factory import create_store

router = APIRouter(prefix="/api", tags=["graph"])
logger = logging.getLogger(__name__)


def _node_label(uri: str) -> str:
    frag = uri.split("#")[-1] if "#" in uri else uri.rstrip("/").split("/")[-1]
    return frag[:30]


def _to_cytoscape(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> dict:
    """TraversalResult.nodes/.edges → Cytoscape.js elements 형식."""
    cy_nodes = []
    cy_edges = []
    seen: set[str] = set()

    for n in nodes:
        uri = str(n.get("uri", ""))
        if not uri or uri in seen:
            continue
        seen.add(uri)
        label = n.get("label") or _node_label(uri)
        depth = n.get("depth", 0)
        cy_nodes.append({
            "data": {"id": uri, "label": label, "depth": depth},
            "classes": "root" if depth == 0 else "",
        })

    for e in edges:
        src = str(e.get("from", e.get("subject", "")))
        tgt = str(e.get("to", e.get("object", "")))
        pred = str(e.get("predicate", ""))
        pred_label = e.get("predicate_label") or _node_label(pred)
        eid = f"{src[-30:]}_{tgt[-30:]}_{pred_label}"
        cy_edges.append({
            "data": {"id": eid, "source": src, "target": tgt, "label": pred_label}
        })

    return {"elements": {"nodes": cy_nodes, "edges": cy_edges}}


@router.get("/graph/schema")
async def graph_schema():
    """TBox overview as Cytoscape elements (class hierarchy)."""
    store = create_store()
    schema = await store.get_schema()
    nodes = []
    edges = []
    for cls in schema.classes:
        label = cls.label or _node_label(cls.uri)
        nodes.append({
            "data": {
                "id": cls.uri,
                "label": f"{label}\n({cls.instance_count})",
                "instance_count": cls.instance_count,
            },
            "classes": "schema-class",
        })
        if cls.parent_uri:
            edges.append({
                "data": {
                    "id": f"sub_{cls.uri}",
                    "source": cls.uri,
                    "target": cls.parent_uri,
                    "label": "subClassOf",
                }
            })
    return {"elements": {"nodes": nodes, "edges": edges}}


@router.get("/graph/traverse")
async def graph_traverse(uri: str, depth: int = Query(default=2, ge=1, le=4)):
    """BFS from entity URI — returns neighborhood as Cytoscape elements."""
    store = create_store()
    result = await store.traverse(start_uri=uri, max_depth=depth)
    return _to_cytoscape(result.nodes, result.edges)


@router.get("/graph/path")
async def graph_path(uri_a: str, uri_b: str):
    """Shortest path between two URIs."""
    store = create_store()
    result = await store.find_path(uri_a=uri_a, uri_b=uri_b)
    return _to_cytoscape(result.nodes, result.edges)
