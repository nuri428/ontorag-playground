"""Ingest route — Stage 1 (schema load) + Stage 3 (data + ACL linking).

Domain-neutral: reads domain_dir from env/request, zero movie knowledge.
"""
from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from rdflib import Graph as RDFGraph, Literal, URIRef
from rdflib.namespace import RDF, RDFS

from ontorag.stores.factory import create_store
from engine.ingest.mapper import DomainMapper, MappingConfig
from engine.acl.linker import ACLRule, EntityLinker

router = APIRouter(prefix="/api", tags=["ingest"])
logger = logging.getLogger(__name__)

_ACL_FETCH_LIMIT = 5000
_DOMAIN_ROOT = Path("domains").resolve()


async def _build_existing_graph(store, new_graph: RDFGraph, rules: list[ACLRule]) -> RDFGraph:
    """스토어에서 기존 엔티티를 가져와 ACL 결선에 필요한 프로퍼티만 담은 그래프를 반환한다.

    new_graph에 등장하는 클래스 URI만 조회해 불필요한 전체 스캔을 피한다.
    """
    match_preds = {URIRef(r.match_property) for r in rules}
    class_uris = {str(o) for _, _, o in new_graph.triples((None, RDF.type, None))}

    existing = RDFGraph()
    for class_uri in class_uris:
        try:
            entities = await store.find_entities(class_uri=class_uri, limit=_ACL_FETCH_LIMIT)
        except Exception as exc:
            logger.warning("ACL: cannot fetch entities for %s: %s", class_uri, exc)
            continue
        for entity in entities:
            s = URIRef(entity.uri)
            if RDFS.label in match_preds and entity.label:
                existing.add((s, RDFS.label, Literal(entity.label)))
            for pred_uri, values in entity.properties.items():
                pred = URIRef(pred_uri)
                if pred not in match_preds:
                    continue
                for v in (values if isinstance(values, list) else [values]):
                    if v is not None:
                        existing.add((s, pred, Literal(str(v))))

    logger.debug("ACL existing graph: %d triples for %d classes", len(existing), len(class_uris))
    return existing


def _domain_dir() -> Path:
    return Path(os.environ.get("DOMAIN_DIR", "domains/default"))


def _resolve_domain(raw: str | None) -> Path:
    if raw is None:
        return _domain_dir()
    candidate = Path(raw).resolve()
    if not candidate.is_relative_to(_DOMAIN_ROOT):
        raise HTTPException(400, "domain_dir must be inside domains/")
    return candidate


class DataIngestRequest(BaseModel):
    records: list[dict[str, Any]]
    domain_dir: str | None = None


@router.post("/ingest/schema")
async def ingest_schema():
    """Load TBox schema.ttl from the active domain directory."""
    schema_path = _domain_dir() / "schema.ttl"
    if not schema_path.exists():
        raise HTTPException(400, f"schema.ttl not found: {schema_path}")
    store = create_store()
    result = await store.load_rdf(str(schema_path), mode="schema")
    return {"triples_loaded": result.triples_loaded, "source": str(schema_path)}


@router.post("/ingest/data")
async def ingest_data(req: DataIngestRequest):
    """Map source records → RDF via mapping.yaml, apply ACL links, load into ABox."""
    domain = _resolve_domain(req.domain_dir)
    mapping_path = domain / "mapping.yaml"
    acl_path = domain / "acl_rules.yaml"

    if not mapping_path.exists():
        raise HTTPException(400, f"mapping.yaml not found: {mapping_path}")
    if not req.records:
        raise HTTPException(400, "No records provided")

    cfg = MappingConfig(mapping_path)
    mapper = DomainMapper(cfg)
    new_graph = mapper.map_records(req.records)

    store = create_store()
    links_added = 0
    if acl_path.exists():
        linker = EntityLinker(acl_path)
        existing_graph = await _build_existing_graph(store, new_graph, linker.rules)
        links = linker.find_links(new_graph, existing_graph)
        new_graph = new_graph + links
        links_added = len(links)

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".ttl", delete=False, encoding="utf-8"
    ) as tmp:
        tmp.write(new_graph.serialize(format="turtle"))
        tmp_path = tmp.name

    try:
        result = await store.load_rdf(tmp_path, mode="data")
        return {
            "records": len(req.records),
            "triples_loaded": result.triples_loaded,
            "links_added": links_added,
        }
    finally:
        Path(tmp_path).unlink(missing_ok=True)


class InferenceRequest(BaseModel):
    rule_name: str
    entity_uris: list[str]          # 명시적 필수 — 암묵적 전체 스캔 금지
    domain_dir: str | None = None


@router.post("/inference/run")
async def run_inference(req: InferenceRequest):
    """Stage 7: inference_rules.yaml 규칙을 적용해 inference named graph에 기록.

    entity_uris는 필수다. 암묵적 전체 스캔은 실수로 수백 건 LLM 호출을 유발하므로 금지.
    find_entities()는 list[EntityResult]를 반환 (EntityResult.uri 로 접근).
    """
    from engine.inference import InferenceExtractor, load_rules
    from ontorag.llm.factory import get_llm_provider

    if not req.entity_uris:
        raise HTTPException(400, "entity_uris must be provided explicitly")

    domain = _resolve_domain(req.domain_dir)
    rules_path = domain / "inference_rules.yaml"
    if not rules_path.exists():
        raise HTTPException(400, f"inference_rules.yaml not found: {rules_path}")

    rules = load_rules(rules_path)
    store = create_store()
    llm = get_llm_provider()
    extractor = InferenceExtractor(llm=llm, store=store, rules=rules)

    written = await extractor.process_entities(req.entity_uris, req.rule_name)
    return {
        "rule": req.rule_name,
        "processed": len(req.entity_uris),
        "written": written,
    }


@router.get("/ingest/status")
async def ingest_status():
    """Show what's loaded in the active domain."""
    domain = _domain_dir()
    store = create_store()
    schema = await store.get_schema()
    return {
        "domain_dir": str(domain),
        "schema_classes": len(schema.classes),
        "total_instances": sum(c.instance_count for c in schema.classes),
    }
