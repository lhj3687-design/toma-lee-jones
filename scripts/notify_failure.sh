#!/usr/bin/env bash
# 워크플로 실행이 실패했을 때 텔레그램으로 한 번 알립니다.
#
# 왜 '한 번'인가:
#   봇은 1분마다 돌기 때문에, 실패할 때마다 알리면 하루 1400건이 넘는 알림 폭탄이 됩니다.
#   그래서 '직전 실행은 성공했는데 이번에 실패한' 전환 시점에만 알립니다.
#   고장이 이어지는 동안에는 조용하고, 고쳐졌다가 다시 깨지면 그때 또 알립니다.
#
# 이 스크립트는 실패해도 워크플로에 영향을 주지 않아야 하므로 set -e를 쓰지 않습니다.
# (이미 실패한 실행에서 도는 스크립트가 상황을 더 나쁘게 만들면 안 됩니다.)
set -uo pipefail

workflow_file="${WORKFLOW_FILE:-mercari-check.yml}"

if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${TELEGRAM_CHAT_ID:-}" ]; then
  echo "[실패 알림 생략] 텔레그램 설정이 없습니다"
  exit 0
fi

# 지금 실행 중인 이 run은 아직 completed가 아니므로, 아래 조회는 지난 실행들을 돌려줍니다.
#
# cancelled는 건너뜁니다. 1분 주기로 돌리면 실행이 겹칠 때마다 대기 중이던 run이
# 취소되는데(실측 약 9%), 이걸 '정상이 아님'으로 세면 취소 직후에 생긴 진짜 실패가
# 조용히 묻혀 버립니다. 성공/실패로 끝난 가장 최근 실행만 기준으로 삼습니다.
previous="$(gh api \
  "repos/${GITHUB_REPOSITORY}/actions/workflows/${workflow_file}/runs?status=completed&per_page=20" \
  --jq '[.workflow_runs[].conclusion | select(. == "success" or . == "failure")][0]' \
  2>/dev/null || true)"

if [ "$previous" = "failure" ]; then
  echo "[실패 알림 생략] 직전 실행도 실패했습니다. 중복 알림을 보내지 않습니다."
  exit 0
fi

if [ -z "$previous" ] || [ "$previous" = "null" ]; then
  echo "[실패 알림 진행] 직전 실행 결과를 확인하지 못했습니다. 놓치는 것보다 낫도록 알립니다."
fi

run_url="${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}"
text="⚠️ 메루카리 알림봇 실행 실패
워크플로가 실패했습니다. 알림이 멈춰 있을 수 있습니다.
${run_url}"

if curl -sS -X POST \
  "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
  --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
  --data-urlencode "text=${text}" >/dev/null; then
  echo "실패 알림을 텔레그램으로 보냈습니다"
else
  echo "[경고] 실패 알림 전송에 실패했습니다" >&2
fi

exit 0
