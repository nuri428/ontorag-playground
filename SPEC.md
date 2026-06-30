> # ontorag-playground
>
> > 도메인 중립 온톨로지 챗봇 엔진. 영화는 첫 번째 검증 데이터셋(reference dataset)일 뿐이다.
> > 이 문서는 Claude Code가 단계별로 자가 검증하며 진행하기 위한 실행 명세다.
>
> ---
>
> ## 0. 이 프로젝트가 무엇이고 무엇이 아닌가
>
> **무엇이다**
>
> - 사용자가 스키마(TBox)와 데이터(ABox)를 넣으면, 온톨로지/지식그래프를 구성하고, 그 위에서 LLM이 대화로 답하는 **앱 단위 playground**.
> - 신규 데이터가 들어오면 기존 그래프에 자동으로 엮이고(flow ACL), "최근 추가된 것"을 시간 기준으로 조회할 수 있다.
> - 기존 `ontorag` / `ontorag-flow` / `ontorag-memory` 자산을 통합해 하나의 돌아가는 앱으로 만든다.
>
> **무엇이 아니다**
>
> - "영화 챗봇"이 아니다. 영화는 엔진이 도는지 보여주는 첫 데이터셋이다.
> - 상용 제품이 아니다. 공개 OSS / personal playground / WIP다. README에 그렇게 명시한다.
> - 완성을 약속하는 물건이 아니다. 각 단계가 독립적으로 완결이며, 어디서 멈춰도 그 시점의 스냅샷으로 유효하다.
>
> **성공 정의**
>
> > 영화 데이터로 엔진이 깨끗하게 돌고 난 뒤, **TBox 파일과 매핑 파일만 교체하면** 다른 도메인(R&D 과제, 특허 등)으로 갈아끼울 수 있는 상태. 도메인 지식이 코드에 새지 않은 상태.
>
> ---
>
> ## 1. 절대 규칙 — 도메인 중립성 (testbed 자격 조건)
>
> 이 네 군데에 "영화"라는 지식이 코드로 새면 testbed 실격이다. 모든 단계에서 이 규칙을 위반하지 않았는지 검증한다.
>
> 1. **TBox(온톨로지 스키마)**: 코드가 아니라 외부 `.ttl` 파일. 도메인 교체 = 이 파일 교체.
> 2. **ingest 매핑**: "이 소스 필드 → 이 프로퍼티" 매핑을 if-else로 코드에 박지 않는다. 외부 **매핑 규칙 파일**(YAML/JSON)로 둔다.
> 3. **ACL 결선 규칙**: "이 프로퍼티 값이 같으면 병합/연결" 같은 **도메인 중립 규칙**으로 표현한다. "감독 이름 같으면" 같은 영화 전용 if를 박지 않는다.
> 4. **LLM 프롬프트**: "너는 영화 전문가" 류 하드코딩 금지. 현재 로드된 TBox를 읽어 동적으로 컨텍스트를 구성한다.
>
> > 검증법: 코드 전체에서 영화 고유명사("director", "movie", "actor", "genre" 등)를 grep 한다. 엔진 코어(`engine/`, `core/`)에서 0건이어야 한다. 영화 단어는 오직 `domains/movie/` 같은 데이터/설정 디렉토리에만 존재한다.
>
> ---
>
> ## 2. 기술 스택 (확정)
>
> - **그래프 스토어**: Apache Jena Fuseki (기본), Neo4j 어댑터는 기존 GraphStore Protocol 추상화 위에서 선택.
> - **LLM 레이어**: OpenAI 호환 클라이언트 하나. `base_url` / `model` / `api_key` 설정으로 cloud(OpenAI 등)와 local(Ollama `http://localhost:11434/v1`)을 동시 지원. 코드는 한 벌.
> - **임베딩**: 단일 모델로 고정(bge-m3). LLM과 분리. 토글하지 않는다(인덱스 일관성 때문).
> - **structured 출력**: native JSON mode에 의존하지 않는다. "프롬프트로 JSON 유도 + 파싱 실패 시 재시도" 패턴(provider-agnostic). local/cloud 양쪽에서 동작해야 하기 때문.
> - **백엔드**: FastAPI + fastapi-mcp (엔드포인트가 MCP 도구로 자동 노출). SSE로 스트리밍.
> - **프론트**: 최소 표면. 채팅 화면 + 그래프 라이브뷰(Cytoscape.js, 기존 자산 재활용) 두 개만. 나머지(스키마 편집기, 매핑 UI, ACL 설정 UI)는 V1에서 만들지 않는다 — 파일/설정으로 우회.
> - **스케줄러**: Celery (기존 자산). 단, 수동 ingest가 검증된 뒤에만 켠다.
>
> ---
>
> ## 3. 시간성 모델 (영화 도메인에 맞춘 정의)
>
> 영화 데이터는 **증분 성장(append-only)**이지 상태 변화(mutation)가 아니다. 따라서:
>
> - 복잡한 bi-temporal / validity window 모델은 **쓰지 않는다**.
> - 모든 노드·엣지에 `ingestedAt` 타임스탬프 하나만 박는다.
> - "최근 변동" 질의 = "최근 N일 내 `ingestedAt`된 노드/엣지" 조회.
> - upsert/덮어쓰기 문제가 발생하지 않는다(지울 게 없으므로). 새 트리플 INSERT만.
>
> > 단, 도메인 교체를 대비해 `ingestedAt` 부여 로직은 엔진 코어에 둔다(도메인 중립). 상태 변화가 있는 미래 도메인(R&D 갱신 등)을 위해 validity window는 **확장 지점(extension point)**으로만 설계에 남겨두고 V1에서 구현하지 않는다.
>
> ---
>
> ## 4. 컴포넌트 경계
>
> ```
> repo: ontorag-playground (모노레포)
> ├── engine/                  # 도메인 중립 코어. 영화 단어 0건.
> │   ├── graphstore/          # Fuseki/Neo4j 어댑터 (GraphStore Protocol)
> │   ├── ingest/              # 매핑 파일을 읽어 소스→ABox 변환 (populate-structured 재사용)
> │   ├── acl/                 # 도메인 중립 결선 규칙 엔진 (flow 흡수)
> │   ├── llm/                 # OpenAI 호환 클라이언트, 동적 프롬프트 구성
> │   ├── query/               # 자연어 → SPARQL, 질의 유형 라우팅
> │   └── api/                 # FastAPI + fastapi-mcp + SSE
> ├── domains/
> │   └── movie/               # 영화 = 첫 reference dataset. 영화 지식은 전부 여기.
> │       ├── schema.ttl       # 영화 TBox
> │       ├── mapping.yaml     # TMDB 필드 → TBox 프로퍼티 매핑
> │       ├── acl_rules.yaml   # 결선 규칙 (도메인 중립 문법으로 기술)
> │       └── connector.py     # TMDB API 호출 → 정규화 JSONL
> ├── web/                     # 채팅 + 그래프뷰 (2개 표면만)
> └── README.md                # "personal playground / WIP" 명시
> ```
>
> > 핵심: `domains/movie/`를 통째로 `domains/rnd/`로 바꾸면 도메인이 교체된다. `engine/`은 한 줄도 안 바뀐다. 이게 검증의 최종 기준.
>
> ---
>
> ## 5. 단계별 백로그 (각 단계 = 독립 완결 + done 기준)
>
> 각 단계는 "이게 되면 끝(done)"이 명시돼 있다. Claude Code는 done 기준을 통과할 때까지 그 단계를 반복한다. done을 통과하면 커밋하고 다음 단계로.
>
> ### Stage 0 — 골격 + LLM 채팅
>
> 기존 ontorag playground에 채팅 입력 하나 + LLM 응답.
>
> - LLM 레이어: OpenAI 호환 클라이언트, 설정으로 cloud/local 토글.
> - **done**: `web`에서 텍스트를 보내면 설정된 LLM(cloud 또는 Ollama)이 답한다. 설정 파일에서 `base_url`만 바꿔 양쪽 다 응답 확인.
>
> ### Stage 1 — TBox/ABox 적재 + 그래프 기반 응답
>
> `.ttl` 스키마와 데이터를 적재하고, 그 그래프에 근거해 답한다.
>
> - `engine/ingest`가 `domains/movie/schema.ttl`(TBox)을 로드.
> - 영화 ABox 소수(예: 영화 50편 + 관련 인물)를 `populate-structured`로 로드.
> - `engine/query`가 자연어 질문 → SPARQL → 답변.
> - **done**: "봉준호가 감독한 영화는?" 류 단일홉 질문에 그래프 근거로 정답. 답변에 어느 노드를 근거로 썼는지(provenance) 포함.
> - **검증**: `engine/`에 영화 고유명사 grep 0건.
>
> ### Stage 2 — 그래프 라이브뷰 + multi-hop
>
> 채팅 옆에 그래프가 보이고, 답할 때 사용한 노드가 하이라이트된다.
>
> - Cytoscape.js로 현재 그래프 렌더.
> - 답변 시 근거 노드/엣지 하이라이트.
> - multi-hop 질의 지원("봉준호 영화에 나온 배우가 출연한 다른 감독의 작품").
> - **done**: 2홉 이상 질문에 정답 + 화면에서 경로가 하이라이트된다. (이게 데모의 "와 포인트". 여기까지가 공개 가능한 1차 스냅샷.)
> - **acceptance test** (이 두 질문에 답하면 Stage 2 통과):
>   - ① "전지현이 나온 영화 중 일제강점기가 배경인 영화에서 배역명은?" — 속성 필터 + 관계 속성(배역명). **단, 이 시점엔 시대배경이 없으므로 배역명까지만 답하고 "시대배경 데이터 없음"으로 빈 필터가 정상**. 일제강점기 필터는 Stage 7 이후에 작동(아래 Before/After 참고).
>   - ② "A와 B가 같이 출연한 영화의 여주인공이 주연인 작품의 시대 배경은?" 류 4홉 traversal.
>
> ### Stage 3 — 수동 OpenAPI ingest (커넥터)
>
> KMDB(한국영화데이터베이스) Open API에서 데이터를 긁어 그래프에 넣는다. **스케줄러 없이 수동 트리거.**
>
> - 소스: KMDB Open API (한국영상자료원). 제명·제작년도·제작사·크레딧·줄거리·장르·키워드 제공. 개발계정 일 1,000건 제한. koreafilm.or.kr 가입 → 서비스키 발급(심의 있음).
> - `domains/movie/connector.py`: KMDB 호출 → `mapping.yaml` 기준으로 정규화 JSONL.
>   - **핵심 작업**: KMDB 응답은 평탄하지 않다. 크레딧(감독·배우)이 중첩 배열로 온다. "영화 1편 → 영화 노드 + 인물 노드들 + 출연 관계"로 풀어내는 정규화가 Stage 3 작업량의 대부분.
>   - **배역명 = 관계의 속성**. 배우-영화 단순 엣지가 아니라 중간 `Casting`/`Role` 노드(배역명, 주연여부)를 둔다. KMDB 크레딧이 배우+배역명 쌍으로 오므로 매핑이 자연스럽다. (이건 사실 데이터이므로 ACL 2차 가공 아님 — 그냥 매핑.)
>   - **인물 식별자 확인**: KMDB가 인물 고유 ID를 주면 동명이인 처리가 공짜(Stage 4 쉬워짐). 이름만 주면 ACL 결선이 까다로워진다. 샘플 응답 1건으로 확인 후 결정.
> - 그 JSONL을 `engine/ingest`의 `populate-structured`에 물린다.
> - 각 노드/엣지에 `ingestedAt` 부여.
> - **시대배경은 여기서 채우지 않는다.** KMDB 원본엔 구조화된 `시대배경` 필드가 없다. 일부러 비워두고 Stage 7에서 ACL 2차 가공으로 생산한다(아래 Before/After 데모의 핵심).
> - **done**: "지금 긁어" 한 번으로 신규 영화 N편이 그래프에 들어가고 채팅으로 질의된다. 같은 영화 두 번 넣어도 중복 노드 안 생긴다(id 기준 멱등).
> - **검증**: 매핑이 `mapping.yaml`에 있고 `connector.py` 외 `engine/`엔 KMDB/영화 지식 0건.
>
> > ⚠️ 순서: Stage 3 작업 전 KMDB 서비스키 발급 → 영화 1편 **샘플 응답을 실제로 받아본 뒤** mapping.yaml과 Casting 정규화를 설계한다. 추측으로 매핑 박지 않는다.
>
> ### Stage 4 — flow ACL 자동 결선
>
> 신규 데이터가 기존 그래프에 자동으로 엮인다. **이게 이 프로젝트만의 차별점.**
>
> - `engine/acl`이 `acl_rules.yaml`을 읽어 도메인 중립 규칙으로 결선.
>   - 예: "프로퍼티 X 값이 기존 노드와 같으면 그 노드에 연결" (이름 정규화/동명이인 처리 포함).
> - 신규 결선 엣지는 다른 색/메타로 표시(추론된 연결임을 구분, confidence 부여).
> - **done**: 신작 1편 ingest → 기존 감독/배우/장르 노드에 자동으로 엮이고, 라이브뷰에서 새 엣지가 구분되어 보인다. 잘못 엮인 경우 confidence threshold로 걸러진다.
> - **검증**: 결선 규칙이 `acl_rules.yaml`에 도메인 중립 문법으로 있고, `engine/acl`에 영화 if-else 0건.
>
> ### Stage 5 — 시간 기준 조회 (증분 성장 질의)
>
> "최근 추가된 것"을 시간으로 묻는다.
>
> - `engine/query`가 질의 유형을 구분: "상태 질의"(지금 X는?) vs "증분 질의"(최근 추가된 X는?).
> - 증분 질의는 `ingestedAt` 기준 SPARQL.
> - **done**: "이번 주 새로 추가된 봉준호 관련 작품은?" 류에 정답. 질의 유형 라우팅이 두 종류를 구분.
>
> ### Stage 6 (선택) — 스케줄러 자동화
>
> Stage 3 수동 ingest가 충분히 안정된 뒤에만.
>
> - Celery로 `connector.py`를 긴 주기(예: 주 1회)로 자동 호출.
> - 실패/중복/스키마 변경에 대한 로깅·알림.
> - **done**: 크론이 돌고, 깨지면 로그로 드러난다. 방치돼도 그래프에 쓰레기 안 쌓인다(Stage 3 멱등성 덕분).
>
> ### Stage 7 (선택, V2급) — ACL 2차 가공: "없던 메타를 생산해 검색 가능하게"
>
> **이 프로젝트의 가장 강력한 데모 서사.** 원본에 없던 메타를 시스템이 스스로 만들어내고, 그 메타로 검색까지 된다. 정적 영화 DB는 못 하는 것.
>
> **영화 reference 시연 — 시대배경 (Before / After)**
>
> - **Before**: "전지현 나온 영화 중 일제강점기 배경?" → 빈 결과. KMDB 원본에 `시대배경` 필드가 없으므로.
> - **ACL 2차 가공 실행**: 시놉시스 텍스트("때는 1940년대 경성...")를 LLM이 훑어 시대 메타 생산.
>   - **추출**: 시놉시스 → "1940년대, 경성" + 근거가 된 시놉시스 문장.
>   - **정규화(핵심)**: "1940년대"/"일제 치하"/"해방 직전" 등 제각각 표현을 **사전 정의된 시대 온톨로지**(구한말 → 일제강점기(1910~1945) → 해방기 → 한국전쟁 → ...)의 노드로 매핑. LLM이 라벨을 자유롭게 짓게 두지 않고 기존 시대 노드에 매핑하도록 강제 → 표현이 달라도 같은 노드로 수렴. (이게 일반 RAG가 못 하는, 온톨로지 기반 시스템의 차별점. myKG의 `--base-schema` 철학과 동일: LLM은 추출만, 권위 노드는 시스템이 강제.)
>   - 연도가 명확히 뽑히면 숫자로도 저장(보조). 시대 매핑이 우선.
> - **After**: 같은 질문 → 답 나옴. "*모델이 일제강점기로 분류한* 전지현 출연작: ○○○, 배역명 △△△."
>   - 배역명은 검증된 **사실**, 일제강점기는 모델의 **추론** — 한 답에 섞이므로 레이어 분리가 필수.
>
> **Stage 7 공통 규칙** (모든 도메인에 적용)
>
> - **반드시** 사실 레이어(`:facts`)와 추론 레이어(`:inference`)를 named graph로 분리. 시대 노드는 `:inference`에 들어간다.
> - 파생 노드마다 `prov:wasGeneratedBy`(모델), `generatedAt`, `confidence`, **근거 문장**(provenance). 근거 문장이 있어야 "왜 일제강점기로 분류됐지?" 검증이 되고 환각을 걸러낸다.
> - 답변 시 사실과 의견을 언어적으로 구분("배역명은 X(사실)" vs "모델이 일제강점기로 분류한(추론)").
> - 인과("왜?")는 구현하지 않는다(환각 위험). "근거 노드/문장 나열"로 대체.
> - **done**: 위 Before/After가 실제로 시연된다. 파생 노드가 `:inference`에 들어가고, 답변이 사실/추론을 구분해 말하며, 근거 문장으로 검증 가능하다.
>
> ---
>
> ## 6. 도메인 교체 리허설 (최종 검증)
>
> Stage 5까지 끝나면, testbed 자격을 실제로 시험한다.
>
> - `domains/movie/`를 복사해 `domains/demo2/`를 만들고, 작은 다른 도메인(예: 책-저자-출판사, 또는 R&D 과제 축소판)의 `schema.ttl` + `mapping.yaml` + `acl_rules.yaml` + `connector.py`로 교체.
> - **done**: `engine/` 코드를 한 줄도 고치지 않고 새 도메인이 적재되고 질의된다. 이게 통과하면 "범용 온톨로지 챗봇 엔진"이 증명된 것.
>
> > 이 리허설이 통과하는 순간, 영화는 "포기한 도메인"이 아니라 "엔진을 증명한 첫 데이터셋"이 되고, R&D/특허 등 진짜 관심 도메인으로 가는 비용이 "디렉토리 하나 교체"로 줄어든다.
>
> ---
>
> ## 7. 스코프 가드 (perfectionist-then-disengage 방지)
>
> Claude Code와 작업자(나) 모두 이 가드를 지킨다.
>
> - **한 번에 한 Stage.** done 기준 통과 전에 다음 Stage 기능을 미리 당겨오지 않는다.
> - **Stage 2까지가 공개 가능한 최소 완결물.** 여기서 멈춰도 부끄럽지 않은 상태로 만든다.
> - **새 아이디어는 코드가 아니라 이 문서의 "백로그" 섹션에 적는다.** 즉시 구현하지 않는다.
> - **범용 커넥터 만들지 않는다.** "어떤 API든 꽂으면 되게" 추상화는 V1 금지(끝없는 배관). 영화 커넥터 하나만 제대로.
> - **둘 다 지원 욕심 금지.** LLM은 OpenAI 호환 한 벌로 이미 둘 다 됨. 임베딩은 한 종류 고정.
>
> ---
>
> ## 8. 백로그 (지금 구현 안 함, 잊지 않기 위해 적어둠)
>
> - **데이터셋 시퀀스 (난이도 오름차순, testbed 강도 증가)**: 영화(깨끗, append-only) → 드라마/예능(작품유형 확장) → KPOP(이름/시간 지옥). 각 단계가 §6 도메인 교체 리허설의 실전 케이스.
>   - 드라마/예능 확장 시 TBox: 최상위 `Work` 클래스 + `Film`/`Drama`/`Show` subclass. 인물·출연 관계는 `Work` 레벨에 둔다 → "영화든 드라마든 출연" 한 프로퍼티로 통일, 작품유형 넘나드는 traversal 가능.
>   - KPOP은 그룹↔멤버 다대다 + 다중 이름(본명/활동명/일본명) + **그룹 멤버십이 진짜 상태 변화**(탈퇴/해체/이적). → §3의 validity window 확장 지점을 실제로 발동시키는 첫 도메인.
> - 상태 변화(mutation) 도메인을 위한 validity window / bi-temporal 모델 (Graphiti식). 영화엔 불필요(append-only), KPOP·R&D 갱신엔 필요. §3 확장 지점으로 설계엔 남겨둠, V1 구현 안 함.
> - 도메인 교체: R&D 과제(NTIS/data.go.kr) — 과제→기관→연구자→특허→논문 그래프. 기존 KIPRIS/patent_board 자산 재활용. NSF Award + PatentsView 연계 아이디어의 한국판. (관심 최대지만 규모·도메인지식 리스크 → 엔진 증명 후로 미룸.)
> - "상시 운영 서비스"(영화/드라마/예능/KPOP 출연진 검색 서비스): testbed가 증명된 뒤 **별개로** 결정. testbed에 운영 부담을 묶지 않는다.
> - 스키마 편집기 / 매핑 UI / ACL 설정 UI (V1에서 파일로 우회한 것들의 시각화).
> - LoCoMo / LongMemEval 류 메모리 벤치마크로 ontorag-memory 검증 (Zep/Mem0 대비 점수).
> - 인과 추론 노드 (강한 confidence threshold + "추정" 라벨).
> - 범용 커넥터 추상화 (영화 커넥터가 잘 돈 다음).
>
> ---
>
> ## 9. README에 박을 한 줄
>
> > ontorag-playground is a domain-neutral, ontology-grounded knowledge-graph chatbot engine.
> > You bring a schema (TBox) and data (ABox); it builds the graph, auto-links new data, and answers over it with an LLM (local or cloud).
> > Movies are just the first reference dataset — swap the `domains/` folder to point it at anything.
> > Status: personal playground / WIP. Not a product.
