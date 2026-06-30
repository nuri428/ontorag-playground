#!/usr/bin/env bash
# KMDB 일별 자동 수집 스크립트
# crontab: 10 0 * * * /Users/nuri/dev/git/ws/ontorag/ontorag-playground/daily_collect.sh

set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
BASE="/Users/nuri/dev/git/ws/ontorag/ontorag-playground"
cd "$BASE"

LOG_DIR="data/logs"
mkdir -p "$LOG_DIR"

# ── 당일 중복 실행 방지 ────────────────────────────────────────────────────────
TODAY=$(date +%Y%m%d)
DONE_FLAG="$LOG_DIR/.done_${TODAY}"
if [ -f "$DONE_FLAG" ]; then
    exit 0  # 오늘 이미 실행됨
fi

LOG="$LOG_DIR/collect_${TODAY}_$(date +%H%M%S).log"
exec >> "$LOG" 2>&1

echo "=== $(date '+%Y-%m-%d %H:%M:%S') 수집 시작 ==="

CMD="uv run python domains/movie/connector.py"
BUDGET=70

# ── 상태 판별 (Python one-liner) ─────────────────────────────────────────────
done_count() { python3 -c "
import json, os
f='$1'
print(len(json.load(open(f)).get('done',[])) if os.path.exists(f) else 0)
"; }

state_year() { python3 -c "
import json, os
f='$1'
print(json.load(open(f)).get('year', 0) if os.path.exists(f) else 0)
"; }

# ── Phase 1: 조연 필모그래피 (2020-2025) 마무리 ──────────────────────────────
SUPP_DONE=$(done_count data/supporting_state.json)
if [ "$SUPP_DONE" -lt 270 ]; then
    echo "[Phase 1] 조연 필모그래피 (${SUPP_DONE}/270명 완료)"
    $CMD pull-filmography \
        --min-avg-position 3.0 --max-avg-position 6.0 \
        --out data/supporting_filmography.jsonl \
        --state data/supporting_state.json \
        --max-calls $BUDGET
    $CMD load --jsonl data/supporting_filmography.jsonl
    echo "[Phase 1] ✓ $(date '+%H:%M:%S') 완료"
    touch "$DONE_FLAG"; exit 0
fi
echo "[Phase 1] ✓ 이미 완료 (270/270)"

# ── Phase 2: 2000-2019 기본 수집 ─────────────────────────────────────────────
YEAR_2019=$(state_year data/pull_state_2000_2019.json)
if [ "$YEAR_2019" -lt 2020 ]; then
    echo "[Phase 2] 2000-2019 기본 수집 (${YEAR_2019}년부터)"
    $CMD pull-incremental \
        --year-from 2000 --year-to 2019 \
        --out data/movies_2000_2019.jsonl \
        --state data/pull_state_2000_2019.json \
        --max-calls $BUDGET
    $CMD load --jsonl data/movies_2000_2019.jsonl
    echo "[Phase 2] ✓ $(date '+%H:%M:%S') 완료"
    touch "$DONE_FLAG"; exit 0
fi
echo "[Phase 2] ✓ 이미 완료 (2000-2019)"

# ── Phase 3: 2000-2019 주연 배우 필모그래피 ──────────────────────────────────
# pull-filmography 자체가 완료 여부를 내부 상태로 판단 — 완료 시 즉시 종료 후 다음 phase
PHASE3_OUTPUT=$($CMD pull-filmography \
    --source-jsonl data/movies_2000_2019.jsonl \
    --out data/filmography_2000_2019.jsonl \
    --state data/filmography_2000_2019_state.json \
    --max-calls $BUDGET 2>&1)
echo "$PHASE3_OUTPUT"

if ! echo "$PHASE3_OUTPUT" | grep -q "수집 완료\|모든 주연"; then
    $CMD load --jsonl data/filmography_2000_2019.jsonl
    echo "[Phase 3] ✓ $(date '+%H:%M:%S') 로딩 완료"
    touch "$DONE_FLAG"; exit 0
fi
echo "[Phase 3] ✓ 이미 완료"

# ── Phase 4: 2000-2019 조연 배우 필모그래피 ──────────────────────────────────
PHASE4_OUTPUT=$($CMD pull-filmography \
    --source-jsonl data/movies_2000_2019.jsonl \
    --min-avg-position 3.0 --max-avg-position 6.0 \
    --out data/supporting_filmography_2000_2019.jsonl \
    --state data/supporting_state_2000_2019.json \
    --max-calls $BUDGET 2>&1)
echo "$PHASE4_OUTPUT"

if ! echo "$PHASE4_OUTPUT" | grep -q "수집 완료\|모든 조연"; then
    $CMD load --jsonl data/supporting_filmography_2000_2019.jsonl
    echo "[Phase 4] ✓ $(date '+%H:%M:%S') 로딩 완료"
fi

echo ""
echo "✅ 모든 수집 단계 완료! ($(date '+%Y-%m-%d'))"
echo "   2000-2025 전체 한국 극영화 + 주연/조연 필모그래피 수집 종료."

# 완료 플래그 — 오늘은 더 이상 실행 안 함
touch "$DONE_FLAG"
