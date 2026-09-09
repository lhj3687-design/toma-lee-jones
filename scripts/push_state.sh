#!/usr/bin/env bash
# seen_items.json을 원격 저장소에 반영합니다. 드문 push 충돌도 상태 병합 후 재시도합니다.
set -euo pipefail

commit_message="${1:-update Mercari alert state}"

for attempt in 1 2 3 4 5; do
  git add seen_items.json
  if git diff --staged --quiet; then
    echo "상태 변경 없음"
    exit 0
  fi

  git commit -m "$commit_message"
  if git push; then
    echo "상태 저장 완료"
    exit 0
  fi

  echo "[재시도 $attempt] 원격 상태와 충돌 -> 병합 후 재시도"
  cp seen_items.json /tmp/mine.json
  git fetch origin main
  git show origin/main:seen_items.json > /tmp/theirs.json 2>/dev/null || echo '{}' > /tmp/theirs.json
  python3 merge_seen.py
  git reset --hard origin/main
  cp /tmp/merged.json seen_items.json
done

echo "여러 번 재시도했지만 상태 저장에 실패했습니다" >&2
exit 1
