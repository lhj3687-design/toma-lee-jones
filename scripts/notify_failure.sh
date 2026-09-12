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
# 어느 워크플로가 실패했는지 구분해서 알립니다(알림 봇 본체 / 이력 압축).
if [ "$workflow_file" = "mercari-check.yml" ]; then
  headline="⚠️ 메루카리 알림봇 실행 실패
알림이 멈춰 있을 수 있습니다."
else
  headline="⚠️ 메루카리 저장소 이력 압축 실패
알림 자체에는 영향이 없지만 저장소 용량 정리가 밀립니다."
fi
text="${headline}
${run_url}"

# 전송 성공은 종료 코드로 판단하면 안 됩니다.
#
# curl은 --fail 없이는 HTTP 401/400에도 종료 코드 0을 돌려줍니다. 그래서 예전에는
# **토큰이 바뀌었거나 채팅 ID가 틀렸을 때**(README가 대표 사례로 꼽는 바로 그 상황)
# 아무것도 전송되지 않았는데 "보냈습니다"라고 로그에 적혔습니다. 이건 알림 경로가
# 막혔는지 확인할 마지막 수단까지 거짓말을 하는 것이라, 실제 상태와 로그가 어긋난
# 채로 조용히 지나갑니다.
#
# 그래서 HTTP 상태와 텔레그램의 ok 필드를 둘 다 봅니다.
response_file="$(mktemp)"
status="$(curl -sS -o "$response_file" -w '%{http_code}' -X POST \
  "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
  --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
  --data-urlencode "text=${text}" 2>"${response_file}.err")"
body="$(head -c 300 "$response_file" 2>/dev/null)"
transport_error="$(head -c 200 "${response_file}.err" 2>/dev/null)"
rm -f "$response_file" "${response_file}.err"

if [ "$status" = "200" ] && printf '%s' "$body" | grep -q '"ok":true'; then
  echo "실패 알림을 텔레그램으로 보냈습니다"
else
  echo "[경고] 실패 알림이 전송되지 않았습니다 (HTTP ${status:-?}) ${body}${transport_error}" >&2
fi

exit 0
