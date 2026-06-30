"""Unit tests — no live backend required.

Stage 1 done criteria:
  ✓ mapper: 소스 레코드 → RDF, 올바른 타입 + ingestedAt

Stage 1.1 (Casting 노드):
  ✓ mapper: intermediate_entities(배우 배열) → Casting 노드 생성
  ✓ Casting 노드가 Movie→Casting, Casting→Person 링크를 포함

Stage 4 done criteria:
  ✓ ACL linker: 동명 엔티티 → owl:sameAs
  ✓ confidence threshold 준수
  ✓ _build_existing_graph: 스토어 → existing_graph 빌드 (Stage 4 버그 수정 검증)

Stage 5 done criteria:
  ✓ query router: STATE / INCREMENTAL / MULTI_HOP 올바르게 분류

Domain-neutrality:
  ✓ engine/ Python 로직에 도메인 고유명사 없음
"""
import pytest
from unittest.mock import AsyncMock, MagicMock
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF, RDFS, OWL

from engine.ingest.mapper import DomainMapper, MappingConfig, INGESTED_AT
from engine.acl.linker import EntityLinker
from engine.query.router import QueryType, route_question

MAPPING_PATH = "domains/movie/mapping.yaml"
RULES_PATH   = "domains/movie/acl_rules.yaml"

MV = "https://playground.ontorag.dev/movie/"

# ── 샘플 레코드 (KMDB 정규화 후 형태) ───────────────────────────────────────

# KMDB 정규화 후 형태 (connector.normalize_movie() 출력과 동일한 구조)
PARASITE = {
    "id":           "20190001",        # movieSeq (PK)
    "movie_seq":    "20190001",
    "title":        "기생충",
    "release_year": 2019,
    "genre":        ["드라마", "스릴러"],
    "plot":         "전원 백수인 기택 가족. 장남 기우가 부잣집 딸의 과외 선생님이 된다.",
    "directors":    [{"directorNm": "봉준호", "directorId": "D00031234"}],
    "actors": [
        {"actorNm": "송강호", "actorEnNm": "Song Kang-ho", "actorId": "P00012345"},
        {"actorNm": "이선균", "actorEnNm": "Lee Sun-kyun",  "actorId": "P00023456"},
        {"actorNm": "조여정", "actorEnNm": "Cho Yeo-jeong", "actorId": "P00034567"},
        {"actorNm": "최우식", "actorEnNm": "Choi Woo-shik", "actorId": "P00045678"},
    ],
}


# ── mapper 기본 ─────────────────────────────────────────────────────────────

def test_mapper_triples_count():
    cfg = MappingConfig(MAPPING_PATH)
    g = DomainMapper(cfg).map_records([PARASITE])
    assert len(g) >= 10


def _movie_uri() -> URIRef:
    return URIRef(f"{MV}movie/20190001")


def test_mapper_rdf_type():
    cfg = MappingConfig(MAPPING_PATH)
    g = DomainMapper(cfg).map_record(PARASITE)
    assert (_movie_uri(), RDF.type, URIRef(f"{MV}Movie")) in g


def test_mapper_label():
    cfg = MappingConfig(MAPPING_PATH)
    g = DomainMapper(cfg).map_record(PARASITE)
    labels = list(g.objects(_movie_uri(), RDFS.label))
    assert any("기생충" in str(l) for l in labels)


def test_mapper_ingested_at():
    cfg = MappingConfig(MAPPING_PATH)
    g = DomainMapper(cfg).map_record(PARASITE)
    ts_vals = list(g.objects(_movie_uri(), INGESTED_AT))
    assert len(ts_vals) == 1
    assert "T" in str(ts_vals[0])  # valid ISO 8601


def test_mapper_director_link():
    cfg = MappingConfig(MAPPING_PATH)
    g = DomainMapper(cfg).map_record(PARASITE)
    director_pred = URIRef(f"{MV}directedBy")
    directors = list(g.objects(_movie_uri(), director_pred))
    assert len(directors) == 1
    # directorId 기반 URI가 생성되어야 함 (이름 슬러그 아님)
    bong_uri = directors[0]
    assert "D00031234" in str(bong_uri)
    assert list(g.objects(bong_uri, RDFS.label))  # "봉준호" label


def test_mapper_idempotent():
    cfg = MappingConfig(MAPPING_PATH)
    mapper = DomainMapper(cfg)
    g1 = mapper.map_record(PARASITE)
    g2 = mapper.map_record(PARASITE)
    assert len(g1) == len(g2)


# ── Casting 노드 (배역명 = 관계의 속성) ─────────────────────────────────────

def test_casting_nodes_created():
    """배우 4명 → Casting 노드 4개 생성."""
    cfg = MappingConfig(MAPPING_PATH)
    g = DomainMapper(cfg).map_record(PARASITE)
    casting_class = URIRef(f"{MV}Casting")
    castings = list(g.subjects(RDF.type, casting_class))
    assert len(castings) == 4


def test_casting_has_no_role_name():
    """KMDB wisenut API는 actorRole 미제공 — roleName 트리플 없어야 한다."""
    cfg = MappingConfig(MAPPING_PATH)
    g = DomainMapper(cfg).map_record(PARASITE)
    role_pred = URIRef(f"{MV}roleName")
    roles = list(g.triples((None, role_pred, None)))
    assert len(roles) == 0


def test_casting_links_to_movie():
    """Movie -hasCasting-> Casting 링크 확인."""
    cfg = MappingConfig(MAPPING_PATH)
    g = DomainMapper(cfg).map_record(PARASITE)
    has_casting = URIRef(f"{MV}hasCasting")
    castings = list(g.objects(_movie_uri(), has_casting))
    assert len(castings) == 4


def test_casting_links_to_person():
    """Casting -performedBy-> Person 링크 확인."""
    cfg = MappingConfig(MAPPING_PATH)
    g = DomainMapper(cfg).map_record(PARASITE)
    performed_by = URIRef(f"{MV}performedBy")
    persons = [str(o) for _, _, o in g.triples((None, performed_by, None))]
    assert len(persons) == 4
    # actorId(P0001234x) 기반 URI 확인
    assert any("P000" in p for p in persons)


# ── ACL linker ────────────────────────────────────────────────────────────────

def _person_graph(uri: str, label: str) -> Graph:
    g = Graph()
    s = URIRef(uri)
    g.add((s, RDF.type, URIRef(f"{MV}Person")))
    g.add((s, RDFS.label, Literal(label)))
    return g


def test_linker_same_person():
    linker = EntityLinker(RULES_PATH)
    new = _person_graph("urn:a", "송강호")
    existing = _person_graph("urn:b", "송강호")
    links = linker.find_links(new, existing)
    assert (URIRef("urn:a"), OWL.sameAs, URIRef("urn:b")) in links


def test_linker_case_insensitive():
    linker = EntityLinker(RULES_PATH)
    new = _person_graph("urn:a", "봉준호")
    existing = _person_graph("urn:b", "봉준호")
    links = linker.find_links(new, existing)
    assert len(links) == 1


def test_linker_no_self_link():
    linker = EntityLinker(RULES_PATH)
    new = _person_graph("urn:x", "Same")
    existing = _person_graph("urn:x", "Same")
    links = linker.find_links(new, existing)
    assert len(links) == 0


def test_linker_threshold():
    linker = EntityLinker(RULES_PATH)
    new = _person_graph("urn:a", "A")
    existing = _person_graph("urn:b", "A")
    links = linker.find_links(new, existing, confidence_threshold=0.99)
    assert len(links) == 0


# ── query router ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("q,expected", [
    # STATE
    ("봉준호가 감독한 영화는?",                    QueryType.STATE),
    ("기생충의 줄거리는?",                          QueryType.STATE),
    ("송강호의 배역명은?",                          QueryType.STATE),
    # INCREMENTAL
    ("최근 추가된 영화는?",                         QueryType.INCREMENTAL),
    ("이번 주 새로 들어온 작품",                    QueryType.INCREMENTAL),
    ("recently added movies",                       QueryType.INCREMENTAL),
    # MULTI_HOP — Stage 2 acceptance test 형태
    ("봉준호 영화에 나온 배우가 출연한 다른 영화",  QueryType.MULTI_HOP),
    ("A와 B가 같이 출연한 영화의 여주인공이 주연인 작품", QueryType.MULTI_HOP),
    ("movies that starred the same actor through another director", QueryType.MULTI_HOP),
])
def test_router(q, expected):
    assert route_question(q) == expected


# ── Stage 4 fix: _build_existing_graph ───────────────────────────────────────

async def test_build_existing_graph_populates_labels():
    """스토어에서 가져온 엔티티가 existing_graph에 rdfs:label로 들어와야 한다.

    버그 수정 회귀 테스트: 이전에는 RDFGraph()가 넘어가 링크가 0개였음.
    """
    from engine.api.routes.ingest import _build_existing_graph
    from engine.acl.linker import EntityLinker

    linker = EntityLinker(RULES_PATH)

    # 스토어 mock: Person 클래스 조회 시 "송강호" 반환
    mock_entity = MagicMock()
    mock_entity.uri = "urn:existing:person:songkangho"
    mock_entity.label = "송강호"
    mock_entity.properties = {}

    mock_store = MagicMock()
    mock_store.find_entities = AsyncMock(return_value=[mock_entity])

    # new_graph에 Person 클래스 엔티티 하나 (새로 ingest되는 "송강호")
    new_graph = Graph()
    person_class = URIRef(f"{MV}Person")
    new_graph.add((URIRef("urn:new:person:songkangho"), RDF.type, person_class))
    new_graph.add((URIRef("urn:new:person:songkangho"), RDFS.label, Literal("송강호")))

    existing = await _build_existing_graph(mock_store, new_graph, linker.rules)

    # 기존 엔티티 label이 existing_graph에 들어있어야 한다
    labels = list(existing.objects(URIRef("urn:existing:person:songkangho"), RDFS.label))
    assert len(labels) == 1
    assert str(labels[0]) == "송강호"


async def test_build_existing_graph_links_same_person():
    """_build_existing_graph → find_links 파이프라인 통합 확인.

    기존 "송강호"(urn:b) + 신규 "송강호"(urn:a) → owl:sameAs 링크 생성.
    """
    from engine.api.routes.ingest import _build_existing_graph
    from engine.acl.linker import EntityLinker

    linker = EntityLinker(RULES_PATH)

    mock_entity = MagicMock()
    mock_entity.uri = "urn:b"
    mock_entity.label = "송강호"
    mock_entity.properties = {}

    mock_store = MagicMock()
    mock_store.find_entities = AsyncMock(return_value=[mock_entity])

    new_graph = _person_graph("urn:a", "송강호")
    new_graph.add((URIRef("urn:a"), RDF.type, URIRef(f"{MV}Person")))

    existing = await _build_existing_graph(mock_store, new_graph, linker.rules)
    links = linker.find_links(new_graph, existing)

    assert (URIRef("urn:a"), OWL.sameAs, URIRef("urn:b")) in links


async def test_build_existing_graph_store_error_skips_gracefully():
    """스토어 조회 실패 시 예외 없이 빈 그래프를 반환해야 한다."""
    from engine.api.routes.ingest import _build_existing_graph
    from engine.acl.linker import EntityLinker

    linker = EntityLinker(RULES_PATH)

    mock_store = MagicMock()
    mock_store.find_entities = AsyncMock(side_effect=RuntimeError("store down"))

    new_graph = _person_graph("urn:a", "테스트")
    new_graph.add((URIRef("urn:a"), RDF.type, URIRef(f"{MV}Person")))

    existing = await _build_existing_graph(mock_store, new_graph, linker.rules)
    assert len(existing) == 0


# ── domain-neutrality ────────────────────────────────────────────────────────

def test_domain_neutrality():
    """engine/ Python 로직(코드 라인)에 도메인 고유명사 없음."""
    import subprocess
    result = subprocess.run(
        ["grep", "-r", "--include=*.py", "-n",
         "-E",
         r"^\s*(from|import|[a-zA-Z_].*=|return|yield|raise|assert|if|elif|while|for)\b"
         r".*\b(movie|actor|director|tmdb|kmdb|genre|casting)\b",
         "engine/"],
        capture_output=True, text=True,
    )
    hits = [l for l in result.stdout.splitlines() if l.strip()]
    assert hits == [], f"Domain words in engine/ logic:\n" + "\n".join(hits)
