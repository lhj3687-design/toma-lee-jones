#!/usr/bin/env bash
# seen_items.json을 원격 저장소에 반영합니다. 드문 push 충돌도 상태 병합 후 재시도합니다.
set -euo pipefail

# 어느 위치에서 호출되든 항상 저장소 루트 기준으로 동작하도록 고정합니다.
cd "$(git rev-parse --show-toplevel)"

commit_message="${1:-update Mercari alert state}"

# 브랜치를 main으로 박아 두면, 다른 브랜치에서 돌렸을 때 reset --hard가 엉뚱한 브랜치를
# 덮어써 버립니다. 지금 체크아웃된 브랜치를 그대로 씁니다(detached HEAD면 main으로 대체).
branch="$(git rev-parse --abbrev-ref HEAD)"
if [ "$branch" = "HEAD" ]; then
  branch="main"
fi

for attempt in 1 2 3 4 5; do
  git add seen_items.json
  if git diff --staged --quiet; then
    echo "상태 변경 없음"
    exit 0
  fi

  git commit -m "$commit_message"
  if git push origin "HEAD:$branch"; then
    echo "상태 저장 완료"
    exit 0
  fi

  # push 실패는 대부분 다른 실행과의 충돌이지만, 일시적인 네트워크 오류일 수도 있습니다.
  # 두 경우 모두 원격 상태를 다시 받아 병합하고 재시도하면 안전하게 복구됩니다.
  echo "[재시도 $attempt] push 실패 -> 원격 상태와 병합 후 재시도"
  sleep "$((attempt * 2))"
  cp seen_items.json /tmp/mine.json
  # 이력 압축 직후에는 원격 이력이 통째로 바뀌어 원격추적 갱신이 비-fast-forward가 됩니다.
  # --force를 주어야 그 경우에도 최신 상태를 확실히 받아옵니다.
  if ! git fetch --force origin "$branch"; then
    echo "[재시도 $attempt] fetch 실패 -> 잠시 후 다시 시도"
    continue
  fi
  git show "origin/$branch:seen_items.json" > /tmp/theirs.json 2>/dev/null || echo '{}' > /tmp/theirs.json
  python3 merge_seen.py
  git reset --hard "origin/$branch"
  cp /tmp/merged.json seen_items.json
done

echo "여러 번 재시도했지만 상태 저장에 실패했습니다" >&2
exit 1
