#!/usr/bin/env python3
"""러너를 못 받고 멈춘 봇 실행을 밖에서 끊어 주고, 오래 성공이 없으면 이슈로 알립니다.

왜 이 파일이 있는가
-------------------
2026-09-12 19:21 UTC, 실행 #6758이 `queued` 상태로 7시간 18분 멈췄습니다.
concurrency 관문은 이미 통과한 뒤였고(job 레코드가 만들어져 있었습니다) 그룹을 쥔 채
호스티드 러너 배정만 기다렸는데, GitHub이 끝내 러너를 붙이지도 타임아웃으로 끊지도
않았습니다 - updated_at이 created_at과 초 단위까지 같습니다. 그동안 뒤따른 실행 478개는
대기 슬롯에서 밀려나며 전부 취소됐고, 알림이 7시간 넘게 끊겼습니다.

이건 봇 워크플로 안에서 고칠 수 없습니다. 어떤 스텝을 맨 앞에 두든 그 스텝도 잡이
시작돼야 도는데, 잡이 시작되지 못하는 것 자체가 고장이기 때문입니다. 자기가 갇힌 문을
안쪽에서 여는 구조입니다. timeout-minutes도 소용없습니다 - 그 상한은 잡이 **시작된
뒤부터** 셉니다. 그래서 다른 concurrency 그룹을 쓰는 워크플로가 밖에서 끊어야 합니다.

원인은 저장소 바깥입니다(공개 저장소라 사용량 한도가 없고, 같은 시각 Dependabot 실행
4건은 정상으로 러너를 받았으며, githubstatus.com에 그날 인시던트가 없었습니다).
그래서 막을 수는 없고 **피해 시간을 제한**하는 것만 할 수 있습니다.

`pending`은 절대 건드리지 않습니다 (가장 중요한 규칙)
-----------------------------------------------------
GitHub API에서 두 상태는 다릅니다.

    pending : concurrency 차례를 기다리는 중. job 레코드가 아직 없습니다.
    queued  : 관문을 통과해 러너를 기다리는 중. job 레코드가 있습니다.

봇은 1분마다 도는데 한 실행이 50~90초 걸리므로 `pending`은 **정상 역압**으로 늘
생깁니다. 이걸 취소하면 멀쩡한 실행을 죽이는 것입니다.

실측으로 확인했습니다(2026-09-13 03:21~03:34 UTC, 대기 중인 실행 112회 관측).

    pending 실행이 ?status=queued 결과 배열에 들어온 횟수:  0
    queued  실행이 ?status=queued 결과 배열에 들어온 횟수: 10

같은 관측에서 total_count가 0보다 큰데 배열은 비어 오는 일을 5회 봤습니다.
**개수를 믿으면 안 되고 배열만 봐야 합니다.**

즉 `?status=queued` 필터만으로 둘이 갈립니다. 다만 정상 실행도 `queued`를 잠깐
지나가므로(실측 8초 미만) 시간 임계값이 함께 필요합니다. 또 실행이 `queued`와
`pending`을 오가는 것도 관측됐기 때문에, 취소 직전에 상태를 한 번 더 확인합니다.

무엇을 하는가
-------------
  1. STUCK_AFTER를 넘겨 `queued`에 머문 실행을 취소합니다. 취소하면 대기열이 즉시
     풀립니다. 되살아난 봇은 스스로 cadence_alerts로 "실행 주기 저하"를 텔레그램에
     보냅니다 - 2026-09-13 복구 때 실제로 그렇게 됐습니다(health:cadence-slow 기록).
     그래서 이 스크립트는 텔레그램을 직접 건드리지 않습니다. 이미 검증된 경로에
     맡기는 편이 낫습니다.

  2. STALE_AFTER 동안 성공한 실행이 하나도 없으면 이슈를 엽니다. 이건 끊을 대상조차
     없는 경우 - 외부 cron과 GitHub 스케줄이 함께 멈춘 경우 - 를 위한 것입니다.
     그 상태에서는 봇이 스스로 알릴 방법이 없습니다(README의 구조적 사각지대).
"""
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

# 실행이 `queued`에 이만큼 머물면 러너가 오지 않는 것으로 봅니다.
# 근거: `queued`는 concurrency를 이미 통과한 뒤라 대기 시간이 순전히 러너 배정에만
# 달려 있고, 정상은 실측 8초 미만이었습니다. 15분은 그 100배가 넘습니다.
STUCK_AFTER_SECONDS = 15 * 60

# 이 시간 동안 성공한 실행이 하나도 없으면 이슈를 엽니다.
# 근거: 정상은 1분마다 성공합니다. 밀린 대기열을 비우던 2026-09-13 복구 중에도
# 성공 간격이 가장 벌어졌을 때가 약 5.5분이었습니다.
STALE_AFTER_SECONDS = 30 * 60

# 이 접두사로 열려 있는 이슈가 있으면 새로 만들지 않습니다(고장이 이어져도 하나만).
# notify_failure.sh의 "알림 경로가 막혔습니다"와는 다른 고장이라 제목을 분리합니다.
ISSUE_MARKER = "⚠️ 봇이 멈춰 있습니다"


def epoch(value):
    """GitHub의 ISO8601(Z) 시각을 epoch 초로. 파싱할 수 없으면 None."""
    if not isinstance(value, str) or not value:
        return 0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def waited_seconds(run, now):
    """실행이 대기해 온 시간. 기준 시각이 없으면 None."""
    started = epoch(run.get("run_started_at")) or epoch(run.get("created_at"))
    if started is None:
        return None
    return now - started


def stuck_runs(runs, now, stuck_after=STUCK_AFTER_SECONDS):
    """끊어야 할 실행만 골라 (실행, 대기초) 목록으로 돌려줍니다.

    `queued`가 아닌 것은 전부 뺍니다. 특히 `pending`(concurrency 대기)은 정상 역압이라
    건드리면 안 됩니다 - 이 함수의 존재 이유가 그 구분입니다.
    """
    picked = []
    for run in runs:
        if run.get("status") != "queued":
            continue
        waited = waited_seconds(run, now)
        if waited is None or waited < stuck_after:
            continue
        picked.append((run, waited))
    return picked


def has_runner(jobs):
    """러너가 실제로 붙었는지.

    붙지 않은 잡은 runner_id가 **0이고 runner_name이 빈 문자열**입니다. 키가 없는 게
    아닙니다 - 멈췄던 #6758의 잡을 실제 API로 확인한 값입니다. `"runner_id" not in job`
    으로 쓰면 검사가 통째로 무력화됩니다.
    """
    return any(job.get("runner_id") for job in jobs)


def seconds_since_success(success_runs, now):
    """가장 최근 성공으로부터 흐른 시간. 성공 기록이 없으면 None."""
    newest = None
    for run in success_runs:
        finished = epoch(run.get("updated_at")) or epoch(run.get("run_started_at"))
        if finished is not None and (newest is None or finished > newest):
            newest = finished
    return None if newest is None else now - newest


def gh_json(api, path, method=None):
    """gh api 호출 결과를 파싱해 돌려줍니다. 실패하면 None."""
    raw = api(path, method)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        print(f"[경고] 응답을 파싱하지 못했습니다: {path}", file=sys.stderr)
        return None


def make_api(repo):
    """gh를 감싼 호출기. 실패해도 예외를 올리지 않고 None을 돌려줍니다."""

    def api(path, method=None):
        command = ["gh", "api", f"repos/{repo}/{path}"]
        if method:
            command[2:2] = ["-X", method]
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"[경고] gh 호출 실패({path}): {result.stderr.strip()}", file=sys.stderr)
            return None
        return result.stdout

    return api


def unstick(api, workflow, now, stuck_after, dry_run):
    """멈춘 실행을 찾아 취소합니다.

    취소한 개수를 돌려주되, **조회 자체에 실패하면 None**을 돌려줍니다. 둘 다 0으로
    돌려주면 확인하지 못한 것을 "이상 없음"으로 보고하게 됩니다 - 이 저장소에서
    반복해서 나온 부류의 실수입니다(push_state.sh, notify_failure.sh).
    """
    listing = gh_json(api, f"actions/workflows/{workflow}/runs?status=queued&per_page=50")
    if listing is None:
        # 목록을 못 읽으면 아무것도 취소하지 않습니다. 멀쩡한 실행을 죽이는 것보다
        # 한 번 더 기다리는 편이 안전합니다(다음 주기에 또 봅니다).
        return None

    cancelled = 0
    for run, waited in stuck_runs(listing.get("workflow_runs") or [], now, stuck_after):
        run_id = run.get("id")
        minutes = int(waited // 60)

        # 실행이 queued와 pending을 오가는 것이 실측됐습니다. 목록을 읽은 뒤 상태가
        # 바뀌었을 수 있으므로, 끊기 직전에 한 번 더 확인합니다.
        fresh = gh_json(api, f"actions/runs/{run_id}")
        if fresh is None or fresh.get("status") != "queued":
            print(f"[건너뜀] #{run.get('run_number')}: 다시 보니 queued가 아닙니다")
            continue

        jobs = gh_json(api, f"actions/runs/{run_id}/jobs")
        if jobs is None:
            print(f"[건너뜀] #{run.get('run_number')}: 잡 목록을 읽지 못했습니다")
            continue
        if has_runner(jobs.get("jobs") or []):
            print(f"[건너뜀] #{run.get('run_number')}: 러너가 이미 붙어 있습니다")
            continue

        if dry_run:
            print(f"[예행] #{run.get('run_number')}를 취소했을 것입니다 ({minutes}분 대기)")
            cancelled += 1
            continue

        if api(f"actions/runs/{run_id}/cancel", "POST") is None:
            print(f"[실패] #{run.get('run_number')} 취소에 실패했습니다", file=sys.stderr)
            continue
        print(f"[취소] #{run.get('run_number')}: {minutes}분째 러너를 못 받아 끊었습니다")
        cancelled += 1

    return cancelled


def open_stale_issue(api, repo, workflow, stale, dry_run):
    """오래 성공이 없을 때 이슈를 엽니다. 같은 이슈가 열려 있으면 만들지 않습니다."""
    existing = gh_json(api, "issues?state=open&per_page=100")
    if existing is not None:
        open_titles = [
            issue.get("title") or ""
            for issue in existing
            if isinstance(issue, dict) and issue.get("pull_request") is None
        ]
        if any(title.startswith(ISSUE_MARKER) for title in open_titles):
            print("[이슈 생략] 이미 열려 있는 정지 이슈가 있습니다")
            return False
    # 목록을 못 읽었으면 만드는 쪽으로 갑니다. 놓치는 것보다 하나 더 나은 쪽입니다.

    minutes = int(stale // 60)
    title = f"{ISSUE_MARKER} ({workflow})"
    body = (
        f"`{workflow}`가 최근 **{minutes}분** 동안 한 번도 성공하지 못했습니다.\n\n"
        "이 이슈는 저장소 밖의 cron과 GitHub 스케줄이 함께 멈춰 **실행 자체가 생기지 "
        "않는** 경우를 위한 것입니다. 그 상태에서는 실패한 실행이 없어 봇의 텔레그램 "
        "알림 경로가 하나도 돌지 못합니다.\n\n"
        "확인할 것\n\n"
        f"1. Actions 탭에서 `{workflow}`의 최근 실행 상태\n"
        "2. 외부 cron 서비스가 workflow_dispatch를 계속 호출하고 있는지\n"
        "3. 멈춘 실행이 있다면 취소 (이 워크플로가 자동으로 하지만 실패했을 수 있습니다)\n\n"
        "고치신 뒤 이 이슈를 닫아 주세요. 열려 있는 동안에는 새로 만들지 않습니다.\n"
    )

    if dry_run:
        print(f"[예행] 이슈를 열었을 것입니다: {title}")
        return True

    result = subprocess.run(
        ["gh", "issue", "create", "--repo", repo, "--title", title, "--body", body],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"[실패] 이슈를 만들지 못했습니다: {result.stderr.strip()}", file=sys.stderr)
        return False
    print(f"[이슈] {result.stdout.strip()}")
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workflow", default=os.environ.get("WORKFLOW_FILE", "mercari-check.yml"))
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--stuck-after", type=int, default=STUCK_AFTER_SECONDS)
    parser.add_argument("--stale-after", type=int, default=STALE_AFTER_SECONDS)
    parser.add_argument("--dry-run", action="store_true", help="판단만 하고 아무것도 바꾸지 않습니다")
    args = parser.parse_args(argv)

    if not args.repo:
        print("[중단] --repo 또는 GITHUB_REPOSITORY가 필요합니다", file=sys.stderr)
        return 2

    api = make_api(args.repo)
    now = datetime.now(timezone.utc).timestamp()

    cancelled = unstick(api, args.workflow, now, args.stuck_after, args.dry_run)
    if cancelled is None:
        print("[보류] 대기 중인 실행 목록을 조회하지 못했습니다")
    elif cancelled == 0:
        print("[정상] 러너를 기다리다 멈춘 실행이 없습니다")

    successes = gh_json(api, f"actions/workflows/{args.workflow}/runs?status=success&per_page=1")
    if successes is None:
        # 조회에 실패하면 조용히 넘깁니다. 이 판단만으로 이슈를 열면 GitHub API가
        # 잠깐 흔들릴 때마다 이슈가 생깁니다.
        print("[보류] 최근 성공 실행을 조회하지 못했습니다")
        return 0

    stale = seconds_since_success(successes.get("workflow_runs") or [], now)
    if stale is None:
        print("[보류] 성공 기록이 없습니다")
        return 0

    if stale >= args.stale_after:
        print(f"[정지 의심] 마지막 성공이 {int(stale // 60)}분 전입니다")
        open_stale_issue(api, args.repo, args.workflow, stale, args.dry_run)
    else:
        print(f"[정상] 마지막 성공이 {int(stale // 60)}분 전입니다")

    return 0


if __name__ == "__main__":
    sys.exit(main())
