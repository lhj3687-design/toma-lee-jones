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

# "보낼 것이 없다"와 "커밋은 했는데 push를 못 했다"를 구분하기 위한 장치입니다.
#
# 아래 루프는 스테이지에 변화가 없으면 보낼 것도 없다고 판단하는데, 커밋까지 해 둔 뒤
# push만 실패한 상태도 똑같이 '변화 없음'으로 보입니다. 그래서 push 거부 직후 fetch가
# 한 번 실패하면(일시적인 네트워크 오류) 다음 바퀴에서 "상태 변경 없음"이라며 exit 0으로
# 끝나 버렸습니다 — 원격에는 아무것도 올라가지 않았는데 봇은 저장에 성공했다고 믿는,
# 가장 위험한 상태입니다. 전송 단계는 이 성공 신호를 "전송 기록이 원격에 확정됐다"는
# 뜻으로 읽고 다음 알림을 계속 보내기 때문에, 실행이 중간에 끊기면 이미 보낸 알림이
# 다음 실행에서 다시 나갑니다.
#
# 두 가지로 확인합니다.
#   unpushed      : 이번 호출에서 만든 커밋이 아직 원격에 없음
#   unpushed_commits(): 직전 호출이 push하지 못한 채 남겨 둔 커밋이 있음
#                   (워크플로는 저장 단계를 여러 번 부르므로 호출 사이에도 남을 수 있습니다)
unpushed=0

unpushed_commits() {
  # 원격추적 ref가 없으면(아직 한 번도 받아오지 않은 저장소) 판단할 근거가 없습니다.
  if ! git rev-parse --quiet --verify "refs/remotes/origin/$branch" >/dev/null 2>&1; then
    echo 0
    return
  fi
  git rev-list --count "refs/remotes/origin/$branch..HEAD" 2>/dev/null || echo 0
}

for attempt in 1 2 3 4 5; do
  git add seen_items.json
  if git diff --staged --quiet; then
    if [ "$unpushed" -eq 0 ] && [ "$(unpushed_commits)" -eq 0 ]; then
      echo "상태 변경 없음"
      exit 0
    fi
    # 커밋은 이미 있고 push만 실패한 상태입니다. 커밋 단계를 건너뛰고 push만 다시 시도합니다.
  else
    git commit -m "$commit_message"
    unpushed=1
  fi

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
  # reset으로 위에서 만든 커밋이 사라지므로, 병합 결과를 다시 커밋해야 합니다.
  git reset --hard "origin/$branch"
  unpushed=0
  cp /tmp/merged.json seen_items.json
done

echo "여러 번 재시도했지만 상태 저장에 실패했습니다" >&2
exit 1
