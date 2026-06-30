"""Domain-neutral source-to-RDF mapper driven by external mapping.yaml.

No domain-specific field names in this file — all knowledge lives in mapping.yaml.

v1.1: 중간 노드(intermediate_entities) 지원 추가.
      관계 속성명처럼 관계 자체에 속성이 필요할 때 IntermNode 같은 중간 노드를 생성한다.
"""
from __future__ import annotations

import datetime
import logging
import re
import uuid
from pathlib import Path
from typing import Any

import yaml
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF, RDFS, XSD

logger = logging.getLogger(__name__)

INGESTED_AT = URIRef("urn:pg:ingestedAt")


def _slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", str(value).strip())


def _now_iso() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class IntermediateEntityConfig:
    """중간 노드 설정 — 하나의 소스 배열 항목마다 별도 노드를 생성한다.

    예: 소스 배열 → IntermNode 노드 (관계 속성명, 관계 속성 보유)
    """

    def __init__(self, raw: dict[str, Any]):
        self.source_field: str = raw["source_field"]
        self.namespace: str = raw["namespace"]
        self.class_uri: str = raw["class_uri"]
        self.id_strategy: str = raw.get("id_strategy", "concat")
        # id_fields: 중간 노드 URI에 사용할 소스 부모 + 항목 필드
        self.id_fields: list[str] = raw.get("id_fields", ["id"])
        # link_from_parent: 부모 노드에서 이 노드로 연결하는 predicate
        self.link_from_parent: str = raw["link_from_parent"]
        # link_to_ref: 이 노드에서 참조 노드로 연결하는 predicate (예: performedBy)
        self.link_to_ref: str | None = raw.get("link_to_ref")
        # ref_namespace: 참조 노드 namespace (예: Person namespace)
        self.ref_namespace: str | None = raw.get("ref_namespace")
        # ref_id_field: 참조 노드 ID 필드 (예: refId or entityId)
        self.ref_id_field: str | None = raw.get("ref_id_field")
        # ref_label_field: 참조 노드 label 필드
        self.ref_label_field: str | None = raw.get("ref_label_field")
        # ref_class_uri: 참조 노드 클래스
        self.ref_class_uri: str | None = raw.get("ref_class_uri")
        # item_fields: 중간 노드 자체 속성 매핑 (항목 필드 → predicate)
        self.item_fields: dict[str, str] = raw.get("item_fields", {})
        # item_label_field: 중간 노드 rdfs:label (없으면 부여 안 함)
        self.item_label_field: str | None = raw.get("item_label_field")


class MappingConfig:
    """Loaded from mapping.yaml. Carries zero domain-specific logic."""

    def __init__(self, path: str | Path):
        with open(path) as f:
            raw = yaml.safe_load(f)
        self.namespace: str = raw["namespace"]
        self.class_uri: str = raw["class_uri"]
        self.id_field: str = raw.get("id_field", "id")
        self.label_field: str | None = raw.get("label_field")
        self.fields: dict[str, str] = raw.get("fields", {})
        self.reference_fields: dict[str, dict] = raw.get("reference_fields", {})
        self.intermediate_entities: list[IntermediateEntityConfig] = [
            IntermediateEntityConfig(e)
            for e in raw.get("intermediate_entities", [])
        ]

    @property
    def class_ref(self) -> URIRef:
        return URIRef(self.class_uri)


class DomainMapper:
    """Transforms source dicts → rdflib.Graph using MappingConfig."""

    def __init__(self, config: MappingConfig):
        self.cfg = config
        self._ts = _now_iso()

    def record_uri(self, record: dict[str, Any]) -> URIRef:
        raw_id = str(record.get(self.cfg.id_field, uuid.uuid4()))
        return URIRef(f"{self.cfg.namespace}{_slug(raw_id)}")

    def map_record(self, record: dict[str, Any]) -> Graph:
        g = Graph()
        s = self.record_uri(record)
        g.add((s, RDF.type, self.cfg.class_ref))
        g.add((s, INGESTED_AT, Literal(self._ts, datatype=XSD.dateTime)))

        if self.cfg.label_field:
            val = record.get(self.cfg.label_field)
            if val is not None:
                g.add((s, RDFS.label, Literal(str(val))))

        # 단순 필드 매핑
        for src_field, pred_uri in self.cfg.fields.items():
            val = record.get(src_field)
            if val is None:
                continue
            pred = URIRef(pred_uri)
            for item in (val if isinstance(val, list) else [val]):
                g.add((s, pred, Literal(str(item))))

        # 참조 필드 매핑 (단순 노드 링크)
        for src_field, ref_cfg in self.cfg.reference_fields.items():
            val = record.get(src_field)
            if val is None:
                continue
            ref_ns = ref_cfg.get("namespace", self.cfg.namespace)
            pred = URIRef(ref_cfg["predicate"])
            id_key = ref_cfg.get("id_key", "id")
            label_key = ref_cfg.get("label_key")
            for item in (val if isinstance(val, list) else [val]):
                if isinstance(item, dict):
                    ref_id = item.get(id_key)
                    if ref_id is None:
                        logger.warning("ref_id missing for field '%s', skipping intermediate entity", id_key)
                        continue
                    ref_uri = URIRef(f"{ref_ns}{_slug(str(ref_id))}")
                    g.add((s, pred, ref_uri))
                    if label_key and item.get(label_key):
                        g.add((ref_uri, RDFS.label, Literal(str(item[label_key]))))
                else:
                    g.add((s, pred, URIRef(f"{ref_ns}{_slug(str(item))}")))

        # 중간 노드 생성 (관계 속성명 같은 관계 속성)
        for ie in self.cfg.intermediate_entities:
            items = record.get(ie.source_field)
            if not items:
                continue
            if not isinstance(items, list):
                items = [items]
            parent_id = str(record.get(self.cfg.id_field, ""))
            for idx, item in enumerate(items):
                if not isinstance(item, dict):
                    continue
                g += self._map_intermediate(s, parent_id, idx, item, ie)

        return g

    def _map_intermediate(
        self,
        parent_uri: URIRef,
        parent_id: str,
        idx: int,
        item: dict[str, Any],
        ie: IntermediateEntityConfig,
    ) -> Graph:
        g = Graph()

        # 중간 노드 URI 결정 (부모 ID + 참조 ID + 순서)
        ref_id_val = str(item.get(ie.ref_id_field, idx)) if ie.ref_id_field else str(idx)
        node_uri = URIRef(f"{ie.namespace}{_slug(parent_id)}_{_slug(ref_id_val)}")

        g.add((node_uri, RDF.type, URIRef(ie.class_uri)))
        g.add((node_uri, INGESTED_AT, Literal(self._ts, datatype=XSD.dateTime)))

        if ie.item_label_field and item.get(ie.item_label_field):
            g.add((node_uri, RDFS.label, Literal(str(item[ie.item_label_field]))))

        # 부모 → 중간 노드
        g.add((parent_uri, URIRef(ie.link_from_parent), node_uri))

        # 중간 노드 속성
        for src_f, pred_uri in ie.item_fields.items():
            val = item.get(src_f)
            if val is not None:
                g.add((node_uri, URIRef(pred_uri), Literal(str(val))))

        # 중간 노드 → 참조 노드 (예: IntermNode → Person)
        if ie.link_to_ref and ie.ref_namespace and ie.ref_id_field:
            ref_raw_id = item.get(ie.ref_id_field, ref_id_val)
            ref_uri = URIRef(f"{ie.ref_namespace}{_slug(str(ref_raw_id))}")
            g.add((node_uri, URIRef(ie.link_to_ref), ref_uri))
            if ie.ref_class_uri:
                g.add((ref_uri, RDF.type, URIRef(ie.ref_class_uri)))
            if ie.ref_label_field and item.get(ie.ref_label_field):
                g.add((ref_uri, RDFS.label, Literal(str(item[ie.ref_label_field]))))

        return g

    def map_records(self, records: list[dict[str, Any]]) -> Graph:
        out = Graph()
        for r in records:
            out += self.map_record(r)
        return out
