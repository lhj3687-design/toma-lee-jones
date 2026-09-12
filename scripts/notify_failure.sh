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

# 텔레그램이 막혔을 때 쓰는 두 번째 경로입니다. 이 접두사로 열려 있는 이슈가 있으면
# 새로 만들지 않습니다(고장이 반복돼도 이슈가 쌓이지 않게).
ISSUE_MARKER="⚠️ 알림 경로가 막혔습니다"

open_outage_issue() {
  # 텔레그램과 독립된 경로입니다. GitHub이 저장소 소유자에게 메일을 보내 주므로,
  # 토큰이 바뀌거나 채팅 ID가 틀려 텔레그램이 통째로 막혀도 사람에게 닿습니다.
  #
  # 이 함수는 '성공 -> 실패로 바뀐 시점'에만 불립니다(아래 직전 실행 확인 참고).
  # 그래서 고장이 이어져도 한 번만 돌고, 이슈가 분당 하나씩 쌓이지 않습니다.
  local reason="$1"
  local existing

  existing="$(gh api "repos/${GITHUB_REPOSITORY}/issues?state=open&per_page=100" \
    --jq "[.[] | select(.pull_request == null) | select(.title | startswith(\"${ISSUE_MARKER}\"))] | length" \
    2>/dev/null || echo "")"

  # 조회에 실패하면(빈 값) 만드는 쪽으로 갑니다. 어차피 이 경로는 전환 시점에만
  # 도는지라 한 번을 넘길 수 없고, 놓치는 것보다 하나 더 만드는 편이 낫습니다.
  if [ -n "$existing" ] && [ "$existing" != "0" ]; then
    echo "[이슈 생략] 이미 열려 있는 알림 경로 이슈가 ${existing}건 있습니다"
    return 0
  fi

  local issue_body
  issue_body="$(cat <<ISSUE_BODY
텔레그램으로 고장을 알리지 못했습니다. 그래서 이 이슈로 대신 알립니다.

- 실패한 워크플로: \`${workflow_file}\`
- 실행 로그: ${run_url}
- 텔레그램이 막힌 이유: ${reason}

이 봇의 고장 알림은 전부 텔레그램으로 나갑니다. 그래서 텔레그램 자체가 막히면
알릴 수단도 같이 막히고, 알림이 멈춘 사실조차 조용히 지나갑니다.
이 이슈가 그 마지막 경로입니다.

확인할 것:

1. \`TELEGRAM_BOT_TOKEN\` / \`TELEGRAM_CHAT_ID\` 시크릿이 아직 유효한지
2. 위 실행 로그에서 실제 실패 원인
3. 대기열(\`seen_items.json\`의 \`pending\`)에 알림이 쌓여 있는지
   — 보존돼 있으므로 텔레그램이 복구되면 그대로 나갑니다

**고치신 뒤 이 이슈를 닫아 주세요.** 열려 있는 동안에는 같은 이슈를 또 만들지 않습니다.
ISSUE_BODY
)"

  if gh api "repos/${GITHUB_REPOSITORY}/issues" \
    -f "title=${ISSUE_MARKER} (${workflow_file})" \
    -f "body=${issue_body}" >/dev/null 2>&1; then
    echo "텔레그램이 막혀 GitHub 이슈로 알렸습니다"
  else
    echo "[경고] GitHub 이슈 생성도 실패했습니다. 사람에게 닿는 경로가 남아 있지 않습니다" >&2
  fi
}

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

# 시크릿이 비어 있으면 텔레그램은 영영 안 됩니다. 예전에는 여기서 조용히 끝냈는데,
# 그건 '알릴 수단이 아예 없는 상태'를 아무에게도 알리지 않는 것과 같습니다.
# 이 확인을 직전 실행 확인 뒤로 옮겨 뒀으므로, 이슈도 전환 시점에 한 번만 만들어집니다.
if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${TELEGRAM_CHAT_ID:-}" ]; then
  echo "[텔레그램 생략] 설정이 없습니다. GitHub 이슈로 알립니다"
  open_outage_issue "TELEGRAM_BOT_TOKEN 또는 TELEGRAM_CHAT_ID 시크릿이 비어 있습니다"
  exit 0
fi

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
  # 텔레그램이 막혔습니다. 같은 채널로 다시 시도해 봐야 소용없으므로,
  # 독립된 경로인 GitHub 이슈로 알립니다.
  open_outage_issue "HTTP ${status:-?} ${body}${transport_error}"
fi

exit 0
