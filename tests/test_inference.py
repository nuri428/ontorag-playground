"""InferenceExtractor 단위 테스트 — live backend 불필요.

검증 항목:
  ✓ load_rules()가 inference_rules.yaml을 올바르게 파싱
  ✓ 프롬프트에 candidate labels가 포함됨
  ✓ LLM 응답 파싱 (JSON 정상 / 비정상)
  ✓ confidence 임계값 미달 시 빈 그래프 반환
  ✓ inference 그래프에 PROV 트리플 포함
  ✓ engine/inference/ 에 도메인 단어 없음 (도메인 중립성)
"""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from rdflib import Graph, URIRef
from rdflib.namespace import PROV, RDF

from engine.inference.extractor import (
    InferenceExtractor,
    InferenceRule,
    load_rules,
    _CONFIDENCE,
    _EVIDENCE_TEXT,
    _RULE_NAME,
)

RULES_PATH = "domains/movie/inference_rules.yaml"


# ── load_rules ──────────────────────────────────────────────────────────────

def test_load_rules_returns_list():
    rules = load_rules(RULES_PATH)
    assert len(rules) >= 1


def test_load_rules_era_rule():
    rules = load_rules(RULES_PATH)
    era_rule = next((r for r in rules if r.name == "era_classification"), None)
    assert era_rule is not None
    assert "plot" in era_rule.source_property
    assert "Era" in era_rule.candidate_class
    assert "hasEraSetting" in era_rule.result_property
    assert era_rule.output_graph == "urn:pg:inference"
    assert era_rule.min_confidence > 0


def test_ontology_slug_is_valid():
    """output_graph URI에서 load_rdf용 slug가 올바르게 생성된다."""
    import re
    rules = load_rules(RULES_PATH)
    for rule in rules:
        slug = rule.ontology_slug
        assert re.match(r"^[a-zA-Z0-9_-]+$", slug), f"Invalid slug: {slug!r}"


# ── 프롬프트 빌드 ────────────────────────────────────────────────────────────

def _make_extractor(candidates: dict[str, str] | None = None) -> InferenceExtractor:
    rule = InferenceRule(
        name="test_rule",
        source_property="urn:test:text",
        candidate_class="urn:test:Category",
        result_property="urn:test:hasCategory",
        output_graph="urn:test:inference",
        min_confidence=0.7,
        prompt_context="텍스트를 분류하라.",
        task_description="카테고리",
        fallback_label="기타",
    )
    llm = AsyncMock()
    store = AsyncMock()
    ex = InferenceExtractor(llm=llm, store=store, rules=[rule])
    if candidates is not None:
        ex._candidate_cache["test_rule"] = candidates
    return ex


def test_build_prompt_contains_candidates():
    ex = _make_extractor()
    rule = ex._rules["test_rule"]
    candidates = ["A", "B", "C"]
    prompt = ex._build_prompt(rule, "sample text", candidates)
    assert "A" in prompt and "B" in prompt and "C" in prompt
    assert "카테고리" in prompt
    assert "JSON" in prompt


def test_build_prompt_truncates_source():
    ex = _make_extractor()
    rule = ex._rules["test_rule"]
    rule.max_source_chars = 10
    prompt = ex._build_prompt(rule, "A" * 1000, ["X"])
    assert "A" * 11 not in prompt  # 10자 초과 부분 없음


# ── 응답 파싱 ────────────────────────────────────────────────────────────────

def test_parse_response_valid():
    ex = _make_extractor()
    rule = ex._rules["test_rule"]
    resp = json.dumps({"카테고리": "A", "evidence": "근거", "confidence": 0.9})
    label, evidence, conf = ex._parse_response(resp, rule)
    assert label == "A"
    assert evidence == "근거"
    assert conf == pytest.approx(0.9)


def test_parse_response_invalid_json():
    ex = _make_extractor()
    rule = ex._rules["test_rule"]
    label, evidence, conf = ex._parse_response("not json at all", rule)
    assert label == "" and conf == 0.0


def test_parse_response_wrapped_in_text():
    ex = _make_extractor()
    rule = ex._rules["test_rule"]
    resp = '설명 텍스트\n{"카테고리": "B", "evidence": "x", "confidence": 0.8}\n끝'
    label, _, conf = ex._parse_response(resp, rule)
    assert label == "B"
    assert conf == pytest.approx(0.8)


# ── confidence 임계값 ────────────────────────────────────────────────────────

def _make_entity_result(uri: str, text: str) -> Any:
    """EntityResult 모의 객체 — .uri, .label, .properties."""
    m = MagicMock()
    m.uri = uri
    m.label = None
    m.properties = {"urn:test:text": text}
    return m


@pytest.mark.asyncio
async def test_low_confidence_returns_empty_graph():
    ex = _make_extractor(candidates={"A": "urn:A", "기타": "urn:etc"})

    # llm.complete() → _CompletionMessage 모의
    resp_text = json.dumps({"카테고리": "A", "evidence": "e", "confidence": 0.3})
    mock_msg = MagicMock()
    mock_msg.content = [MagicMock(type="text", text=resp_text)]
    ex._llm.complete = AsyncMock(return_value=mock_msg)
    ex._store.describe_entity = AsyncMock(
        return_value=_make_entity_result("urn:entity:1", "sample")
    )

    g = await ex.extract_for_entity("urn:entity:1", "test_rule")
    assert len(g) == 0  # 0.3 < 0.7 (min_confidence)


# ── inference 그래프 구조 ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_inference_graph_has_prov():
    ex = _make_extractor(candidates={"A": "urn:cat:A", "기타": "urn:cat:etc"})

    resp_text = json.dumps({"카테고리": "A", "evidence": "근거문장", "confidence": 0.9})
    mock_msg = MagicMock()
    mock_msg.content = [MagicMock(type="text", text=resp_text)]
    ex._llm.complete = AsyncMock(return_value=mock_msg)
    ex._store.describe_entity = AsyncMock(
        return_value=_make_entity_result("urn:entity:2", "어떤 텍스트")
    )

    g = await ex.extract_for_entity("urn:entity:2", "test_rule")
    assert len(g) > 0

    # result predicate 기록됨
    assert (URIRef("urn:entity:2"), URIRef("urn:test:hasCategory"), URIRef("urn:cat:A")) in g

    # PROV Activity가 있어야 함
    activities = list(g.subjects(RDF.type, PROV.Activity))
    assert len(activities) == 1
    act = activities[0]

    # confidence, evidence, rule name 기록됨
    assert list(g.objects(act, _CONFIDENCE))
    assert list(g.objects(act, _EVIDENCE_TEXT))
    assert list(g.objects(act, _RULE_NAME))


# ── 도메인 중립성 ────────────────────────────────────────────────────────────

def test_inference_module_is_domain_neutral():
    """engine/inference/ Python 로직에 도메인 고유명사 없음."""
    import subprocess
    result = subprocess.run(
        ["grep", "-r", "--include=*.py", "-n",
         "-E",
         r"^\s*(from|import|[a-zA-Z_].*=|return|yield|raise|assert|if|elif)\b"
         r".*\b(movie|era|actor|director|genre|kmdb|casting|시대)\b",
         "engine/inference/"],
        capture_output=True, text=True,
    )
    hits = [l for l in result.stdout.splitlines() if l.strip()]
    assert hits == [], "Domain words in engine/inference/:\n" + "\n".join(hits)
