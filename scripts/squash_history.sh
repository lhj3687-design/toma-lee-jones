#!/usr/bin/env bash
# git 이력을 커밋 하나로 압축합니다. 봇의 상태 파일 '내용'은 전혀 건드리지 않습니다.
#
# 왜 필요한가:
#   seen_items.json을 매 실행마다 커밋하기 때문에 git 이력이 계속 쌓입니다
#   (커밋당 약 1.6KB, 1분 주기 기준 1년에 약 1.2GB).
#   파일 자체는 상한이 있어 2MB 근처에서 멈추므로, 정리 대상은 파일이 아니라 '이력'입니다.
#
# 어떻게 안전을 보장하는가:
#   1) git commit-tree로 '현재 트리를 그대로 가리키는 부모 없는 커밋'을 만듭니다.
#      파일을 복사하거나 다시 쓰지 않고 트리 객체를 그대로 재사용하므로,
#      상태 파일이 달라질 여지가 구조적으로 없습니다.
#   2) --force-with-lease로 밀어냅니다. 읽은 시점 이후 봇이 상태를 갱신했다면
#      푸시가 거부되어 아무것도 잃지 않습니다. 그 경우 최신 상태로 다시 시도합니다.
#   3) 끝까지 실패해도 저장소는 원래대로입니다. 다음 예약 실행에서 다시 시도합니다.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

branch="${1:-main}"
attempts="${SQUASH_ATTEMPTS:-8}"
retry_delay="${SQUASH_RETRY_DELAY:-5}"

export GIT_AUTHOR_NAME="${GIT_AUTHOR_NAME:-mercari-alert-bot}"
export GIT_AUTHOR_EMAIL="${GIT_AUTHOR_EMAIL:-actions@github.com}"
export GIT_COMMITTER_NAME="$GIT_AUTHOR_NAME"
export GIT_COMMITTER_EMAIL="$GIT_AUTHOR_EMAIL"

for attempt in $(seq 1 "$attempts"); do
  # 지금 이 순간의 원격 상태를 읽습니다(이력은 필요 없으므로 최신 커밋 하나만).
  git fetch --force --depth=1 origin "$branch"
  target="$(git rev-parse FETCH_HEAD)"
  tree="$(git rev-parse "${target}^{tree}")"

  # 같은 트리를 가리키는 부모 없는 커밋을 만듭니다(작업 트리를 건드리지 않습니다).
  squashed="$(git commit-tree "$tree" -m "chore: git 이력 압축 (봇 상태 파일은 그대로 유지)")"

  # 방어적 확인: 트리가 정말 동일하고 부모가 없어야 합니다.
  if [ "$(git rev-parse "${squashed}^{tree}")" != "$tree" ]; then
    echo "[중단] 압축본의 트리가 원본과 다릅니다" >&2
    exit 1
  fi
  # 부모가 없는 뿌리 커밋이라면 여기서부터 닿는 커밋은 자기 자신 하나뿐입니다.
  if [ "$(git rev-list --count "$squashed")" -ne 1 ]; then
    echo "[중단] 압축본에 이전 이력이 남아 있습니다" >&2
    exit 1
  fi

  # 읽은 이후 봇이 상태를 갱신했다면 여기서 거부됩니다(상태 유실 없음).
  if git push --force-with-lease="${branch}:${target}" origin "${squashed}:refs/heads/${branch}"; then
    echo "이력 압축 완료 (기준 커밋 ${target}, 새 뿌리 커밋 ${squashed})"
    exit 0
  fi

  echo "[재시도 ${attempt}/${attempts}] 압축하는 사이 봇이 상태를 갱신했습니다. 최신 상태로 다시 시도합니다."
  sleep "$retry_delay"
done

echo "봇이 계속 상태를 갱신 중이라 이번에는 압축하지 못했습니다. 다음 예약 실행에서 다시 시도합니다." >&2
exit 1
