"""Domain-neutral entity linker driven by acl_rules.yaml.

Reads acl_rules.yaml, finds new entities matching existing ones by property
value, and emits linking triples (default: owl:sameAs).
No movie-specific field names here.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

import yaml
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import OWL, RDFS


class ACLRule:
    def __init__(self, raw: dict[str, Any]):
        self.name: str = raw["name"]
        self.match_property: str = raw["match_property"]
        self.link_predicate: str = raw.get("link_predicate", str(OWL.sameAs))
        self.confidence: float = raw.get("confidence", 0.9)
        self.normalize: bool = raw.get("normalize", True)


def _normalize(v: str) -> str:
    v = unicodedata.normalize("NFKC", v).strip().lower()
    return re.sub(r"\s+", " ", v)


class EntityLinker:
    """Links new entities against existing graph via domain-neutral rules."""

    def __init__(self, rules_path: str | Path):
        with open(rules_path) as f:
            raw = yaml.safe_load(f) or {}
        self.rules: list[ACLRule] = [ACLRule(r) for r in raw.get("rules", [])]
        self.min_confidence: float = raw.get("min_confidence", 0.8)

    def find_links(
        self,
        new_graph: Graph,
        existing_graph: Graph,
        confidence_threshold: float | None = None,
    ) -> Graph:
        """Return a Graph of linking triples between new and existing entities."""
        threshold = confidence_threshold if confidence_threshold is not None else self.min_confidence
        links = Graph()

        for rule in self.rules:
            if rule.confidence < threshold:
                continue
            pred = URIRef(rule.match_property)
            link_pred = URIRef(rule.link_predicate)

            existing_idx: dict[str, URIRef] = {}
            for s, _, o in existing_graph.triples((None, pred, None)):
                key = _normalize(str(o)) if rule.normalize else str(o)
                if key in existing_idx:
                    logger.debug("ACL: duplicate key %r — overwriting %s with %s", key, existing_idx[key], s)
                existing_idx[key] = s  # type: ignore[assignment]

            for s, _, o in new_graph.triples((None, pred, None)):
                key = _normalize(str(o)) if rule.normalize else str(o)
                if key in existing_idx and s != existing_idx[key]:
                    links.add((s, link_pred, existing_idx[key]))

        return links
