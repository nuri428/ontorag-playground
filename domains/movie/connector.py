"""KMDB (한국영화데이터베이스) → 정규화 JSONL 커넥터.

이 파일이 KMDB API 필드명을 아는 유일한 곳.
engine/ 에는 KMDB 지식이 없다.

KMDB Open API v2:
  endpoint: https://api.koreafilm.or.kr/openapi-data2/wapi/searchMovieList
  docs:     https://kmdb.or.kr/info/api/apiDetail
  인증: koreafilm.or.kr 가입 → 서비스키 발급 (심의 있음, 개발계정 일 1,000건)

⚠️ 사용 전 체크리스트:
  1. KMDB_SERVICE_KEY 환경변수 설정
  2. `uv run python domains/movie/connector.py sample`으로 응답 1건 확인
  3. mapping.yaml 검증 (필드명 불일치 시 수정)

Usage:
  uv run python domains/movie/connector.py pull --pages 3
  uv run python domains/movie/connector.py load --jsonl data/movies.jsonl
  uv run python domains/movie/connector.py sample   # 응답 1건 raw 출력
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

import httpx
import typer
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent.parent / ".env")

logger = logging.getLogger(__name__)
app = typer.Typer(help="KMDB 영화 커넥터")

_BASE = "http://api.koreafilm.or.kr/openapi-data2/wisenut/search_api"


def _service_key() -> str:
    key = os.environ.get("KMDB_SERVICE_KEY", "")
    if not key:
        raise ValueError(
            "KMDB_SERVICE_KEY 환경변수가 설정되지 않았습니다.\n"
            "koreafilm.or.kr에서 서비스키를 발급받아 .env에 추가하세요."
        )
    return key


def _client() -> httpx.Client:
    return httpx.Client(base_url=_BASE, timeout=30)


_ENDPOINT = "/search_json2.jsp"


# ── KMDB 응답 구조 ─────────────────────────────────────────────────────────
# endpoint: /openapi-data2/wisenut/search_api/search_json2.jsp
# Data[0].Result[]
#   └─ 영화 1건: {DOCID, movieSeq, title, titleEng, prodYear, directors, actors, genre, plots, ...}
#       directors.director[]: {directorNm, directorEnNm, directorId}
#       actors.actor[]:       {actorNm, actorEnNm, actorId}  (actorRole 미제공)
# ─────────────────────────────────────────────────────────────────────────────

def _extract_list(nested: Any, outer_key: str, inner_key: str) -> list[dict]:
    """KMDB 중첩 배열 추출: {outer_key: {inner_key: [...]}} → [...]"""
    if not nested:
        return []
    outer = nested.get(outer_key, {})
    if not outer:
        return []
    items = outer.get(inner_key, [])
    if isinstance(items, dict):
        items = [items]
    return items if isinstance(items, list) else []


def _strip_highlight(text: str) -> str:
    """KMDB 제목의 !HS / !HE 하이라이트 마크업 제거."""
    return re.sub(r"!H[SE]", "", text or "").strip()


def normalize_movie(raw: dict[str, Any]) -> dict[str, Any]:
    """KMDB 영화 1건 → mapping.yaml이 기대하는 평탄화 구조로 변환.

    확인된 필드 기준:
      movieSeq → id (PK)
      title    → title (마크업 제거)
      prodYear → release_year
      genre    → genre (쉼표 → 리스트)
      plots.plot[0].plotText → plot
      directors.director[] → {directorNm, directorId}
      actors.actor[]       → {actorNm, actorId, actorRole(미확인)}
    """
    # 연도
    year: int | None = None
    prod_year = raw.get("prodYear", "") or ""
    if prod_year and re.match(r"^\d{4}", prod_year):
        try:
            year = int(prod_year[:4])
        except ValueError:
            pass

    # 감독 목록 — directorId가 있으면 사용 (고유 ID)
    directors_raw = _extract_list(raw, "directors", "director")
    director_list: list[dict[str, str]] = []
    for d in directors_raw:
        if isinstance(d, dict) and d.get("directorNm"):
            director_list.append({
                "directorNm": _strip_highlight(d.get("directorNm", "")),
                "directorId": d.get("directorId", d.get("directorNm", "")),
            })

    # 배우 목록 — actorId 사용 (actorRole은 KMDB wisenut API 미제공)
    actors_raw = _extract_list(raw, "actors", "actor")
    actor_list: list[dict[str, str]] = []
    for a in actors_raw:
        if isinstance(a, dict) and a.get("actorNm"):
            actor_list.append({
                "actorNm":   _strip_highlight(a.get("actorNm", "")),
                "actorEnNm": a.get("actorEnNm", ""),
                "actorId":   a.get("actorId", a.get("actorNm", "")),
            })

    # 장르
    genre_str = raw.get("genre", "") or ""
    genres = [g.strip() for g in genre_str.split(",") if g.strip()]

    # 시놉시스: plots.plot[].plotText (확인된 구조)
    plot = ""
    plot_items = _extract_list(raw, "plots", "plot")
    if plot_items:
        first = plot_items[0]
        if isinstance(first, dict):
            plot = first.get("plotText", "")
        elif isinstance(first, str):
            plot = first

    return {
        "id":           raw.get("movieSeq", raw.get("DOCID", "")),
        "movie_seq":    raw.get("movieSeq", ""),
        "title":        _strip_highlight(raw.get("title", "")),
        "release_year": year,
        "genre":        genres,
        "plot":         plot,
        "directors":    director_list,
        "actors":       actor_list,
    }


def fetch_movies(
    pages: int = 5,
    query: str = "",
    director: str = "",
    actor: str = "",
    release_from: str = "",
    release_to: str = "",
    movie_type: str = "극영화",
) -> list[dict[str, Any]]:
    """KMDB API에서 영화 목록을 가져와 정규화 JSONL 형태로 반환.

    Args:
        pages: 페이지 수 (페이지당 20건)
        query: 영화 제목 검색어
        director: 감독명
        actor: 배우명
        release_from: 개봉일 시작 (YYYYMMDD)
        release_to: 개봉일 종료 (YYYYMMDD)
        movie_type: 유형 (극영화/애니메이션/다큐멘터리/... 기본=극영화)
    """
    results: list[dict[str, Any]] = []
    with _client() as c:
        for page in range(1, pages + 1):
            params: dict[str, Any] = {
                "collection": "kmdb_new2",
                "ServiceKey": _service_key(),
                "nation": "대한민국",
                "listCount": 20,
                "startCount": (page - 1) * 20,
            }
            if movie_type:
                params["type"] = movie_type
            if query:
                params["title"] = query
            if director:
                params["director"] = director
            if actor:
                params["actor"] = actor
            if release_from:
                params["releaseDts"] = release_from
            if release_to:
                params["releaseDte"] = release_to

            resp = c.get(_ENDPOINT, params=params)
            resp.raise_for_status()
            data = resp.json()

            movie_list = data.get("Data", [{}])[0].get("Result", [])
            if not movie_list:
                break

            for raw in movie_list:
                normalized = normalize_movie(raw)
                if normalized["id"] and normalized["title"]:
                    results.append(normalized)

            time.sleep(0.25)  # API rate limit 준수

    return results


# ── CLI ───────────────────────────────────────────────────────────────────────

@app.command()
def sample():
    """KMDB API 응답 1건을 raw JSON으로 출력한다. mapping.yaml 검증용."""
    logging.basicConfig(level=logging.INFO)
    with _client() as c:
        params = {
            "collection": "kmdb_new2",
            "ServiceKey": _service_key(),
            "nation": "대한민국",
            "type": "극영화",
            "listCount": 1,
            "startCount": 0,
            "title": "기생충",
        }
        resp = c.get(_ENDPOINT, params=params)
        resp.raise_for_status()
        raw_list = resp.json().get("Data", [{}])[0].get("Result", [])
        typer.echo("=== RAW KMDB 응답 1건 ===")
        typer.echo(json.dumps(raw_list[0] if raw_list else {}, ensure_ascii=False, indent=2))
        typer.echo("\n=== 정규화 결과 ===")
        typer.echo(json.dumps(normalize_movie(raw_list[0]) if raw_list else {}, ensure_ascii=False, indent=2))


@app.command()
def pull(
    pages: int = typer.Option(5, help="페이지 수 (페이지당 20건)"),
    query: str = typer.Option("", help="제목 검색어"),
    director: str = typer.Option("", help="감독명"),
    actor: str = typer.Option("", help="배우명"),
    release_from: str = typer.Option("", help="개봉일 시작 YYYYMMDD"),
    release_to: str = typer.Option("", help="개봉일 종료 YYYYMMDD"),
    movie_type: str = typer.Option("극영화", help="유형 (극영화/애니메이션/다큐멘터리/...)"),
    out: Path = typer.Option(Path("data/movies.jsonl"), help="출력 JSONL 경로"),
):
    """KMDB에서 영화 데이터를 긁어 JSONL로 저장."""
    logging.basicConfig(level=logging.INFO)
    movies = fetch_movies(
        pages=pages, query=query, director=director, actor=actor,
        release_from=release_from, release_to=release_to, movie_type=movie_type,
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        for m in movies:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")
    typer.echo(f"✓ {len(movies)}편 저장 → {out}")


_DEFAULT_STATE = Path("data/pull_state.json")
_DEFAULT_OUT   = Path("data/movies.jsonl")


@app.command()
def pull_incremental(
    max_calls: int = typer.Option(90, help="이번 실행에서 최대 API 호출 횟수"),
    year_from: int = typer.Option(2020, help="수집 시작 연도"),
    year_to:   int = typer.Option(2025, help="수집 종료 연도"),
    out:   Path = typer.Option(_DEFAULT_OUT,   help="누적 JSONL 경로"),
    state: Path = typer.Option(_DEFAULT_STATE, help="진행 상태 파일"),
):
    """연도별로 90건/일 제한 안에서 점진적 수집. 내일 실행하면 이어서 진행."""
    logging.basicConfig(level=logging.INFO)

    # ── 상태 로드 ──────────────────────────────────────────────────────────
    if state.exists():
        s = json.loads(state.read_text(encoding="utf-8"))
    else:
        s = {"year": year_from, "page": 0, "total": 0}

    cur_year: int = s["year"]
    cur_page: int = s["page"]
    total:    int = s["total"]

    if cur_year > year_to:
        typer.echo(f"✓ 수집 완료 (총 {total}편). 재수집하려면 state 파일 삭제 후 실행.")
        return

    out.parent.mkdir(parents=True, exist_ok=True)
    calls = 0

    with _client() as c, open(out, "a", encoding="utf-8") as f:
        while calls < max_calls and cur_year <= year_to:
            params = {
                "collection": "kmdb_new2",
                "ServiceKey": _service_key(),
                "nation":     "대한민국",
                "type":       "극영화",
                "releaseDts": f"{cur_year}0101",
                "releaseDte": f"{cur_year}1231",
                "listCount":  20,
                "startCount": cur_page * 20,
            }
            resp = c.get(_ENDPOINT, params=params)
            resp.raise_for_status()
            calls += 1

            results = resp.json().get("Data", [{}])[0].get("Result", [])
            if not results:
                # 해당 연도 소진 → 다음 연도로
                typer.echo(f"  {cur_year}년 완료")
                cur_year += 1
                cur_page = 0
            else:
                for raw in results:
                    nm = normalize_movie(raw)
                    if nm["id"] and nm["title"]:
                        f.write(json.dumps(nm, ensure_ascii=False) + "\n")
                        total += 1
                cur_page += 1

            state.write_text(
                json.dumps({"year": cur_year, "page": cur_page, "total": total},
                           ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            time.sleep(0.3)

    remaining = "완료" if cur_year > year_to else f"{cur_year}년 {cur_page}페이지부터 재개"
    typer.echo(f"✓ 오늘 {calls}건 호출 | 누적 {total}편 | 다음: {remaining}")


_FILMOGRAPHY_STATE = Path("data/filmography_state.json")
_FILMOGRAPHY_OUT   = Path("data/filmography.jsonl")
_SUPPORTING_STATE  = Path("data/supporting_state.json")
_SUPPORTING_OUT    = Path("data/supporting_filmography.jsonl")


@app.command()
def pull_filmography(
    max_calls: int = typer.Option(90, help="이번 실행 최대 API 호출 횟수"),
    min_appearances: int = typer.Option(2, help="최소 출연 편수"),
    min_avg_position: float = typer.Option(0.0, help="최소 평균 배우 순위(포함)"),
    max_avg_position: float = typer.Option(3.0, help="최대 평균 배우 순위(미만)"),
    source_jsonl: Path = typer.Option(Path("data/movies.jsonl"), help="기준 영화 JSONL"),
    out: Path = typer.Option(_FILMOGRAPHY_OUT, help="출력 JSONL 경로"),
    state: Path = typer.Option(_FILMOGRAPHY_STATE, help="진행 상태 파일"),
):
    """배우 필모그래피 수집. 매일 실행하면 이어서 진행.

    주연: min_avg_position=0.0 max_avg_position=3.0 (기본값)
    조연: min_avg_position=3.0 max_avg_position=6.0
    """
    logging.basicConfig(level=logging.INFO)

    # ── 배우 목록 추출 ─────────────────────────────────────────────────────
    from collections import defaultdict
    with open(source_jsonl, encoding="utf-8") as _f:
        movies = [json.loads(l) for l in _f if l.strip()]
    actor_info: dict[str, dict] = defaultdict(lambda: {"count": 0, "pos_sum": 0, "nm": ""})
    for m in movies:
        for pos, a in enumerate(m.get("actors", [])):
            aid, nm = a.get("actorId", ""), a.get("actorNm", "")
            if not aid or not nm:
                continue
            actor_info[aid]["nm"] = nm
            actor_info[aid]["count"] += 1
            actor_info[aid]["pos_sum"] += pos

    candidates = [
        (aid, info["nm"])
        for aid, info in actor_info.items()
        if info["count"] >= min_appearances
        and min_avg_position <= (info["pos_sum"] / info["count"]) < max_avg_position
    ]
    candidates.sort(key=lambda x: -actor_info[x[0]]["count"])

    # ── 상태 로드 ──────────────────────────────────────────────────────────
    if state.exists():
        s = json.loads(state.read_text(encoding="utf-8"))
        done_ids: set[str] = set(s.get("done", []))
    else:
        done_ids = set()

    remaining = [(aid, nm) for aid, nm in candidates if aid not in done_ids]
    role_label = "조연" if min_avg_position >= 3.0 else "주연"
    typer.echo(f"{role_label} 후보 {len(candidates)}명 | 완료 {len(done_ids)}명 | 남은 {len(remaining)}명")

    if not remaining:
        typer.echo(f"✓ 모든 {role_label} 배우 필모그래피 수집 완료!")
        return

    # 기존 영화 ID 중복 방지
    with open(source_jsonl, encoding="utf-8") as _f:
        existing_ids = {json.loads(l).get("id") for l in _f if l.strip()}
    if out.exists():
        with open(out, encoding="utf-8") as _f:
            existing_ids |= {json.loads(l).get("id") for l in _f if l.strip()}

    out.parent.mkdir(parents=True, exist_ok=True)
    calls = 0
    # 이미 저장된 파일 라인 수에서 누적 카운트 복원
    if out.exists():
        with open(out, encoding="utf-8") as _f:
            new_movies = sum(1 for _ in _f if _.strip())
    else:
        new_movies = 0

    with _client() as c, open(out, "a", encoding="utf-8") as f:
        for aid, nm in remaining:
            if calls >= max_calls:
                break

            params = {
                "collection": "kmdb_new2",
                "ServiceKey": _service_key(),
                "nation": "대한민국",
                "type": "극영화",
                "actor": nm,
                "listCount": 20,
                "startCount": 0,
            }
            resp = c.get(_ENDPOINT, params=params)
            resp.raise_for_status()
            calls += 1

            results = resp.json().get("Data", [{}])[0].get("Result", [])
            added = 0
            for raw in results:
                nm_movie = normalize_movie(raw)
                if nm_movie["id"] and nm_movie["id"] not in existing_ids:
                    f.write(json.dumps(nm_movie, ensure_ascii=False) + "\n")
                    existing_ids.add(nm_movie["id"])
                    added += 1
                    new_movies += 1

            done_ids.add(aid)
            typer.echo(f"  [{calls}/{max_calls}] {nm}: +{added}편 신규")

            state.write_text(
                json.dumps({"done": list(done_ids), "total_new": new_movies}, ensure_ascii=False),
                encoding="utf-8",
            )
            time.sleep(0.3)

    next_actor = next((nm for aid, nm in remaining if aid not in done_ids), "완료")
    typer.echo(f"✓ {calls}건 호출 | 신규 {new_movies}편 | 다음: {next_actor}")


@app.command()
def load(
    jsonl: Path = typer.Option(Path("data/movies.jsonl"), help="로드할 JSONL 파일"),
    endpoint: str = typer.Option("http://localhost:8200", help="playground 서버 URL"),
):
    """저장된 JSONL을 playground의 ingest 엔드포인트로 전송."""
    with open(jsonl, encoding="utf-8") as _f:
        records = [json.loads(l) for l in _f if l.strip()]
    r = httpx.post(f"{endpoint}/api/ingest/data", json={"records": records}, timeout=120)
    r.raise_for_status()
    typer.echo(r.json())


if __name__ == "__main__":
    app()
