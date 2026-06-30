"""Domain-neutral inference extractor driven by inference_rules.yaml.

어떤 분류 작업이든 외부 규칙 파일로 제어한다.
engine/ 코드에는 "시대", "장르" 같은 도메인 개념이 없다.

흐름:
  1. 규칙 파일 로드 (InferenceRule 목록)
  2. 각 규칙마다: graph store에서 candidate_class 인스턴스를 읽어 선택지 구성
  3. LLM에 source_property 텍스트 + 선택지 전달 → 분류
  4. 결과를 output_graph named graph에 prov 트리플과 함께 기록

ontorag API 확인:
  store.find_entities(class_uri) → list[EntityResult]   (EntityResult.uri, .label, .properties)
  store.describe_entity(uri)     → EntityResult
  store.load_rdf(path, mode, ontology)  — ontology는 slug(^[a-zA-Z0-9_-]+$)만 허용
  llm.complete(messages, tools)  → _CompletionMessage
"""
from __future__ import annotations

import datetime
import json
import logging
import re
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import PROV, RDF, XSD

logger = logging.getLogger(__name__)

_GENERATED_AT = URIRef("urn:pg:generatedAt")
_CONFIDENCE    = URIRef("urn:pg:confidence")
_EVIDENCE_TEXT = URIRef("urn:pg:evidenceText")
_RULE_NAME     = URIRef("urn:pg:ruleApplied")


@dataclass
class InferenceRule:
    """inference_rules.yaml의 규칙 하나."""
    name: str
    source_property: str
    candidate_class: str
    result_property: str
    output_graph: str           # named graph URI (표시용; load_rdf에는 slug 사용)
    min_confidence: float = 0.7
    prompt_context: str = ""
    task_description: str = "category"
    fallback_label: str = ""
    max_source_chars: int = 800
    label_property: str | None = None

    @property
    def ontology_slug(self) -> str:
        """output_graph URI에서 load_rdf용 slug를 추출한다.

        "urn:pg:inference" → "pg-inference"
        """
        last = self.output_graph.rstrip("/").split(":")[-1].split("/")[-1]
        slug = re.sub(r"[^a-zA-Z0-9_-]", "-", last).strip("-") or "inference"
        return slug


def load_rules(path: str | Path) -> list[InferenceRule]:
    """inference_rules.yaml에서 InferenceRule 목록 로드.

    YAML의 알 수 없는 키(description 등)는 무시한다.
    """
    import dataclasses
    _known = {f.name for f in dataclasses.fields(InferenceRule)}
    with open(path) as f:
        raw = yaml.safe_load(f)
    return [
        InferenceRule(**{k: v for k, v in r.items() if k in _known})
        for r in raw.get("rules", [])
    ]


class InferenceExtractor:
    """LLM으로 텍스트를 온톨로지 인스턴스로 분류한다.

    완전히 설정 파일로 제어 — 도메인 지식 없음.

    Parameters
    ----------
    llm:    ontorag LLMProvider (AnthropicProvider / OpenAIProvider / OllamaProvider)
    store:  GraphStore
    rules:  InferenceRule 목록 (load_rules()로 생성)
    """

    def __init__(self, llm: Any, store: Any, rules: list[InferenceRule]):
        self._llm = llm
        self._store = store
        self._rules = {r.name: r for r in rules}
        self._candidate_cache: dict[str, dict[str, str]] = {}

    async def _load_candidates(self, rule: InferenceRule) -> dict[str, str]:
        """Graph store에서 candidate_class 인스턴스 읽기 → {label → uri} 인덱스.

        find_entities()는 list[EntityResult]를 반환한다.
        EntityResult.uri, .label, .properties 로 접근.
        """
        if rule.name in self._candidate_cache:
            return self._candidate_cache[rule.name]

        try:
            entities = await self._store.find_entities(class_uri=rule.candidate_class)
        except Exception as exc:
            logger.warning("Cannot load candidates for rule %s: %s", rule.name, exc)
            return {}

        from rdflib.namespace import RDFS
        label_pred = rule.label_property or str(RDFS.label)

        index: dict[str, str] = {}
        for e in entities:
            uri = e.uri
            if not uri:
                continue
            if e.label:
                index[e.label] = uri
            # 추가 label 값 (한국어 등) — properties dict에서 label_pred로 탐색
            extra_labels = e.properties.get(label_pred, [])
            if isinstance(extra_labels, str):
                extra_labels = [extra_labels]
            for v in (extra_labels if isinstance(extra_labels, list) else []):
                if isinstance(v, str) and v:
                    index[v] = uri

        self._candidate_cache[rule.name] = index
        return index

    async def _call_llm(self, prompt: str) -> str:
        """LLM 호출 → 텍스트 반환.

        llm.complete(messages, tools) → _CompletionMessage
        content는 list[_TextBlock | _ToolUseBlock].
        """
        try:
            result = await self._llm.complete(
                messages=[{"role": "user", "content": prompt}],
                tools=[],
            )
            return "".join(
                b.text for b in result.content if getattr(b, "type", "") == "text"
            )
        except Exception as exc:
            logger.warning("LLM call failed: %s", exc)
            return ""

    def _build_prompt(
        self,
        rule: InferenceRule,
        source_text: str,
        candidate_labels: list[str],
    ) -> str:
        ctx = rule.prompt_context + "\n\n" if rule.prompt_context else ""
        return (
            f"{ctx}"
            f"텍스트:\n{source_text[:rule.max_source_chars]}\n\n"
            f"{rule.task_description}을 아래 목록 중 하나로만 답하라. "
            f"목록에 없으면 '{rule.fallback_label or '기타'}'를 선택하라.\n"
            f"JSON 형식으로만 답하라:\n"
            f'{{"{rule.task_description}": "<목록 중 하나>", '
            f'"evidence": "<근거 문장>", '
            f'"confidence": <0.0~1.0>}}\n\n'
            f"선택 목록: {candidate_labels}"
        )

    def _parse_response(
        self,
        response: str,
        rule: InferenceRule,
    ) -> tuple[str, str, float]:
        start = response.find("{")
        if start == -1:
            return "", "", 0.0
        depth, end = 0, -1
        for i, ch in enumerate(response[start:], start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end == -1:
            return "", "", 0.0
        try:
            payload = json.loads(response[start:end + 1])
        except json.JSONDecodeError:
            return "", "", 0.0
        label = payload.get(rule.task_description, "")
        evidence = payload.get("evidence", "")
        try:
            confidence = float(payload.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        return label, evidence, confidence

    async def extract_for_entity(
        self,
        entity_uri: str,
        rule_name: str,
    ) -> Graph:
        """단일 엔티티에 규칙 적용 → inference Graph 반환."""
        rule = self._rules.get(rule_name)
        if rule is None:
            raise ValueError(f"Unknown rule: {rule_name!r}")

        candidates = await self._load_candidates(rule)
        if not candidates:
            logger.warning("No candidates for rule %s", rule_name)
            return Graph()

        # describe_entity()는 EntityResult를 반환. .properties는 dict[str, Any].
        try:
            entity = await self._store.describe_entity(uri=entity_uri)
            raw_val = entity.properties.get(rule.source_property, "")
            source_text = (
                " ".join(raw_val) if isinstance(raw_val, list) else str(raw_val or "")
            ).strip()
        except Exception as exc:
            logger.warning("Cannot describe entity %s: %s", entity_uri, exc)
            return Graph()

        if not source_text:
            return Graph()

        prompt = self._build_prompt(rule, source_text, list(candidates.keys()))
        response = await self._call_llm(prompt)
        label, evidence, confidence = self._parse_response(response, rule)

        if confidence < rule.min_confidence or not label:
            logger.info(
                "Rule %s: low confidence %.2f or empty label for %s",
                rule_name, confidence, entity_uri,
            )
            return Graph()

        result_uri_str = candidates.get(label)
        if not result_uri_str and rule.fallback_label:
            result_uri_str = candidates.get(rule.fallback_label)
        if not result_uri_str:
            return Graph()

        return self._build_inference_graph(
            entity_uri, result_uri_str, rule, evidence, confidence
        )

    def _build_inference_graph(
        self,
        entity_uri: str,
        result_uri: str,
        rule: InferenceRule,
        evidence: str,
        confidence: float,
    ) -> Graph:
        g = Graph()
        s = URIRef(entity_uri)
        o = URIRef(result_uri)
        now = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        prov_id = URIRef(f"urn:pg:prov:{uuid.uuid4().hex[:12]}")

        g.add((s, URIRef(rule.result_property), o))
        g.add((prov_id, RDF.type, PROV.Activity))
        g.add((prov_id, PROV.wasAssociatedWith, s))
        g.add((prov_id, _GENERATED_AT, Literal(now, datatype=XSD.dateTime)))
        g.add((prov_id, _CONFIDENCE, Literal(confidence, datatype=XSD.decimal)))
        g.add((prov_id, _EVIDENCE_TEXT, Literal(evidence)))
        g.add((prov_id, _RULE_NAME, Literal(rule.name)))
        g.add((o, PROV.wasGeneratedBy, prov_id))
        return g

    async def process_entities(
        self,
        entity_uris: list[str],
        rule_name: str,
    ) -> int:
        """여러 엔티티 일괄 처리 → inference named graph(slug)에 저장."""
        rule = self._rules.get(rule_name)
        if rule is None:
            raise ValueError(f"Unknown rule: {rule_name!r}")

        written = 0
        for uri in entity_uris:
            tmp_path = None
            try:
                g = await self.extract_for_entity(uri, rule_name)
                if len(g) == 0:
                    continue
                with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".ttl", delete=False, encoding="utf-8"
                ) as tmp:
                    tmp.write(g.serialize(format="turtle"))
                    tmp_path = tmp.name
                # ontology 파라미터는 slug만 허용 — URI에서 slug를 추출
                await self._store.load_rdf(
                    tmp_path, mode="data", ontology=rule.ontology_slug
                )
                written += 1
            except Exception as exc:
                logger.warning("Failed for %s: %s", uri, exc)
            finally:
                if tmp_path:
                    Path(tmp_path).unlink(missing_ok=True)
        return written
