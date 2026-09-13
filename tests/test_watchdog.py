"""scripts/watchdog.py가 멈춘 실행만 끊고 멀쩡한 실행은 건드리지 않는지 검증합니다.

이 감시 도구는 **실전에서 재현할 수 없는** 고장을 다룹니다. "실행이 러너를 못 받고
몇 시간 멈춘 상태"를 마음대로 만들 수 없기 때문입니다. 그래서 판정 함수를 I/O에서
떼어 내고, 2026-09-12~13 사고에서 **실제로 관측한 응답**을 픽스처로 고정했습니다.
아래 상수의 값들은 지어낸 것이 아니라 그때 GitHub API가 돌려준 그대로입니다.

특히 위험한 것은 반대 방향의 실수입니다. 이 도구가 `pending`(concurrency 대기)을
멈춘 것으로 오인하면, 1분마다 도는 봇의 정상 대기 실행을 계속 죽이게 됩니다.
고장을 못 잡는 것보다 나쁩니다. 그래서 그 경계를 양쪽에서 고정합니다.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location("watchdog", ROOT / "scripts" / "watchdog.py")
watchdog = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(watchdog)


def _epoch(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


# ---------------------------------------------------------------------------
# 사고에서 실제로 관측한 응답들
# ---------------------------------------------------------------------------

# 7시간 18분 멈춰 있던 실행. updated_at이 created_at과 초 단위까지 같습니다 -
# GitHub이 이 실행을 다시 건드린 적이 한 번도 없다는 뜻입니다.
STUCK_RUN = {
    "id": 34713912450,
    "run_number": 6758,
    "event": "workflow_dispatch",
    "status": "queued",
    "created_at": "2026-09-12T19:21:06Z",
    "updated_at": "2026-09-12T19:21:06Z",
    "run_started_at": "2026-09-12T19:21:06Z",
}
STUCK_OBSERVED_AT = _epoch("2026-09-13T02:39:05Z")  # 사람이 알아차린 시각

# 그 실행의 잡. runner_id가 **0**이고 runner_name이 **빈 문자열**입니다.
# 키가 없는 게 아닙니다 - 이 구분을 놓치면 러너 검사가 통째로 무력화됩니다.
STUCK_JOB = {
    "id": 103607495724,
    "run_id": 34713912450,
    "name": "check",
    "status": "queued",
    "runner_id": 0,
    "runner_name": "",
    "created_at": "2026-09-12T19:21:07Z",
    "started_at": "2026-09-12T19:21:07Z",
}

# concurrency에 막혀 대기 중이던 정상 실행(같은 사고 중 관측). 이건 건드리면 안 됩니다.
PENDING_RUN = {
    "id": 34733565397,
    "run_number": 7236,
    "status": "pending",
    "created_at": "2026-09-13T02:39:05Z",
    "updated_at": "2026-09-13T02:40:16Z",
    "run_started_at": "2026-09-13T02:39:05Z",
}

# 러너가 붙어 정상 실행된 잡.
HEALTHY_JOB = {
    "id": 103662486959,
    "status": "completed",
    "conclusion": "success",
    "runner_id": 1000006576,
    "runner_name": "GitHub Actions 1000006576",
}


class DecisionTests(unittest.TestCase):
    """판정 함수만 봅니다. 네트워크도 프로세스도 끼지 않습니다."""

    def test_the_run_that_actually_hung_is_picked(self):
        picked = watchdog.stuck_runs([STUCK_RUN], STUCK_OBSERVED_AT)
        self.assertEqual(len(picked), 1)
        run, waited = picked[0]
        self.assertEqual(run["run_number"], 6758)
        self.assertAlmostEqual(waited, 7 * 3600 + 17 * 60 + 59, delta=1)

    def test_a_run_waiting_on_concurrency_is_never_picked(self):
        """`pending`은 봇이 1분마다 도는 데서 오는 정상 역압입니다.

        임계값을 0으로 낮춰도 골라져서는 안 됩니다. 시간이 아니라 상태로 걸러야 합니다.
        """
        self.assertEqual(watchdog.stuck_runs([PENDING_RUN], STUCK_OBSERVED_AT, 0), [])

    def test_a_normal_run_passing_through_queued_is_not_picked(self):
        """정상 실행도 `queued`를 잠깐 지나갑니다(실측 8초 미만)."""
        now = _epoch("2026-09-13T03:21:31Z")
        fresh = dict(STUCK_RUN, run_number=7281, status="queued",
                     run_started_at="2026-09-13T03:21:23Z")
        self.assertEqual(watchdog.stuck_runs([fresh], now), [])

    def test_the_boundary_is_the_threshold_itself(self):
        now = _epoch("2026-09-13T03:00:00Z")
        just_under = dict(STUCK_RUN, run_started_at="2026-09-13T02:45:01Z")
        just_over = dict(STUCK_RUN, run_started_at="2026-09-13T02:45:00Z")
        self.assertEqual(watchdog.stuck_runs([just_under], now), [])
        self.assertEqual(len(watchdog.stuck_runs([just_over], now)), 1)

    def test_in_progress_and_completed_runs_are_ignored(self):
        now = STUCK_OBSERVED_AT
        for status in ("in_progress", "completed", "waiting", "requested"):
            with self.subTest(status=status):
                self.assertEqual(watchdog.stuck_runs([dict(STUCK_RUN, status=status)], now), [])

    def test_a_run_without_usable_timestamps_is_left_alone(self):
        broken = {"id": 1, "run_number": 1, "status": "queued",
                  "run_started_at": None, "created_at": "nonsense"}
        self.assertEqual(watchdog.stuck_runs([broken], STUCK_OBSERVED_AT), [])

    def test_runner_id_zero_means_no_runner(self):
        """0과 빈 문자열로 오는 것이 실제 응답입니다. 키 존재 여부로 보면 안 됩니다."""
        self.assertNotIn("runner_id", {k: v for k, v in STUCK_JOB.items() if v})
        self.assertFalse(watchdog.has_runner([STUCK_JOB]))
        self.assertTrue(watchdog.has_runner([HEALTHY_JOB]))
        self.assertTrue(watchdog.has_runner([STUCK_JOB, HEALTHY_JOB]))
        self.assertFalse(watchdog.has_runner([]))

    def test_staleness_is_measured_from_the_newest_success(self):
        now = _epoch("2026-09-13T03:00:00Z")
        runs = [
            {"updated_at": "2026-09-13T02:30:00Z"},
            {"updated_at": "2026-09-13T02:55:00Z"},
        ]
        self.assertAlmostEqual(watchdog.seconds_since_success(runs, now), 300, delta=1)
        self.assertIsNone(watchdog.seconds_since_success([], now))

    def test_the_seven_hour_outage_would_have_been_called_stale(self):
        runs = [{"updated_at": "2026-09-12T19:20:55Z"}]  # 마지막 성공 #6757
        stale = watchdog.seconds_since_success(runs, STUCK_OBSERVED_AT)
        self.assertGreater(stale, watchdog.STALE_AFTER_SECONDS)


# ---------------------------------------------------------------------------
# 스크립트 전체를 가짜 gh와 함께 실제로 돌립니다
# ---------------------------------------------------------------------------

FAKE_GH = """#!/usr/bin/env bash
if [ "$1" = "issue" ] && [ "$2" = "create" ]; then
  if [ -n "${FAKE_ISSUE_CREATE_FAIL:-}" ]; then echo "gh: simulated failure" >&2; exit 1; fi
  echo "issue-create" >> "$FAKE_GH_LOG"
  shift 2
  while [ $# -gt 0 ]; do
    case "$1" in --title) printf 'title=%s\\n' "$2" >> "$FAKE_GH_LOG"; shift 2;; *) shift;; esac
  done
  echo "https://github.com/o/r/issues/1"
  exit 0
fi

shift                      # "api"
method="GET"; endpoint=""
while [ $# -gt 0 ]; do
  case "$1" in
    -X) method="$2"; shift 2;;
    -*) shift;;
    *) endpoint="$1"; shift;;
  esac
done
echo "$method $endpoint" >> "$FAKE_GH_LOG"

case "$endpoint" in
  *runs\\?status=queued*)
    if [ -n "${FAKE_QUEUED_FAIL:-}" ]; then echo "gh: simulated failure" >&2; exit 1; fi
    cat "$FAKE_QUEUED_FILE";;
  *runs\\?status=success*)
    if [ -n "${FAKE_SUCCESS_FAIL:-}" ]; then echo "gh: simulated failure" >&2; exit 1; fi
    cat "$FAKE_SUCCESS_FILE";;
  *issues\\?state=open*)
    if [ -n "${FAKE_ISSUES_FAIL:-}" ]; then echo "gh: simulated failure" >&2; exit 1; fi
    cat "$FAKE_ISSUES_FILE";;
  */jobs)
    if [ -n "${FAKE_JOBS_FAIL:-}" ]; then echo "gh: simulated failure" >&2; exit 1; fi
    cat "$FAKE_JOBS_FILE";;
  */cancel)
    if [ -n "${FAKE_CANCEL_FAIL:-}" ]; then echo "gh: simulated failure" >&2; exit 1; fi
    echo '{}';;
  *actions/runs/*)
    cat "$FAKE_RUN_FILE";;
  *)
    echo "unexpected endpoint: $endpoint" >&2; exit 1;;
esac
"""


def _iso(seconds_ago):
    stamp = datetime.now(timezone.utc).timestamp() - seconds_ago
    return datetime.fromtimestamp(stamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ScriptTests(unittest.TestCase):
    """가짜 gh를 PATH에 얹고 스크립트를 그대로 실행합니다."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        binary = self.tmp / "bin"
        binary.mkdir()
        fake = binary / "gh"
        fake.write_text(FAKE_GH)
        fake.chmod(0o755)

        self.log = self.tmp / "gh.log"
        self.env = dict(os.environ)
        self.env["PATH"] = f"{binary}:{os.environ['PATH']}"
        self.env["FAKE_GH_LOG"] = str(self.log)

        # 기본값: 멈춘 것 없음 / 방금 성공함 / 열린 이슈 없음
        self.set_queued([])
        self.set_jobs([STUCK_JOB])
        self.set_run(dict(STUCK_RUN, status="queued"))
        self.set_success([{"updated_at": _iso(30)}])
        self.set_issues([])

    def _write(self, name, payload):
        path = self.tmp / name
        path.write_text(json.dumps(payload))
        self.env[name] = str(path)
        return path

    def set_queued(self, runs):
        self._write("FAKE_QUEUED_FILE", {"total_count": len(runs), "workflow_runs": runs})

    def set_success(self, runs):
        self._write("FAKE_SUCCESS_FILE", {"total_count": len(runs), "workflow_runs": runs})

    def set_jobs(self, jobs):
        self._write("FAKE_JOBS_FILE", {"total_count": len(jobs), "jobs": jobs})

    def set_run(self, run):
        self._write("FAKE_RUN_FILE", run)

    def set_issues(self, issues):
        self._write("FAKE_ISSUES_FILE", issues)

    def run_watchdog(self, *extra):
        return subprocess.run(
            ["python3", str(ROOT / "scripts" / "watchdog.py"),
             "--repo", "o/r", "--workflow", "mercari-check.yml", *extra],
            capture_output=True, text=True, env=self.env, cwd=str(ROOT),
        )

    def calls(self):
        return self.log.read_text() if self.log.exists() else ""

    # -- 끊어야 하는 경우 ---------------------------------------------------

    def test_a_run_stuck_for_hours_is_cancelled(self):
        stuck = dict(STUCK_RUN, run_started_at=_iso(7 * 3600), created_at=_iso(7 * 3600))
        self.set_queued([stuck])
        self.set_run(stuck)

        result = self.run_watchdog()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("POST repos/o/r/actions/runs/34713912450/cancel", self.calls())
        self.assertIn("[취소]", result.stdout)

    def test_dry_run_decides_but_cancels_nothing(self):
        stuck = dict(STUCK_RUN, run_started_at=_iso(7 * 3600), created_at=_iso(7 * 3600))
        self.set_queued([stuck])
        self.set_run(stuck)

        result = self.run_watchdog("--dry-run")

        self.assertIn("[예행]", result.stdout)
        self.assertNotIn("/cancel", self.calls())

    # -- 끊으면 안 되는 경우 -----------------------------------------------

    def test_a_pending_run_is_never_cancelled(self):
        """봇이 1분마다 도는 한 이 상태는 늘 있습니다. 죽이면 봇이 멈춥니다."""
        old_pending = dict(PENDING_RUN, run_started_at=_iso(7 * 3600), created_at=_iso(7 * 3600))
        self.set_queued([old_pending])

        result = self.run_watchdog()

        self.assertNotIn("/cancel", self.calls())
        self.assertIn("[정상]", result.stdout)

    def test_a_run_that_started_between_the_two_reads_is_left_alone(self):
        """목록을 읽은 뒤 상태가 바뀌는 일이 실측됐습니다(queued <-> pending, 그리고 시작)."""
        stuck = dict(STUCK_RUN, run_started_at=_iso(7 * 3600), created_at=_iso(7 * 3600))
        self.set_queued([stuck])
        self.set_run(dict(stuck, status="in_progress"))   # 재확인 때는 이미 시작됨

        result = self.run_watchdog()

        self.assertNotIn("/cancel", self.calls())
        self.assertIn("[건너뜀]", result.stdout)

    def test_a_run_whose_job_already_has_a_runner_is_left_alone(self):
        stuck = dict(STUCK_RUN, run_started_at=_iso(7 * 3600), created_at=_iso(7 * 3600))
        self.set_queued([stuck])
        self.set_run(stuck)
        self.set_jobs([HEALTHY_JOB])

        result = self.run_watchdog()

        self.assertNotIn("/cancel", self.calls())
        self.assertIn("러너가 이미 붙어", result.stdout)

    def test_total_count_is_not_trusted_only_the_array_is(self):
        """GitHub이 total_count > 0 인데 배열은 비워서 주는 것을 실측했습니다.

        2026-09-13 03:21~03:34 UTC에 112회 관측하는 동안 5회 그랬습니다. 개수를 믿고
        움직이면 있지도 않은 실행을 근거로 판단하게 됩니다.
        """
        path = self.tmp / "FAKE_QUEUED_FILE"
        path.write_text('{"total_count": 1, "workflow_runs": []}')

        result = self.run_watchdog()

        self.assertNotIn("/cancel", self.calls())
        self.assertIn("[정상]", result.stdout)

    def test_nothing_is_cancelled_when_the_listing_cannot_be_read(self):
        """조회가 흔들릴 때 멀쩡한 실행을 죽이는 것보다 한 주기 더 기다리는 편이 낫습니다."""
        self.env["FAKE_QUEUED_FAIL"] = "1"

        result = self.run_watchdog()

        self.assertEqual(result.returncode, 0)
        self.assertNotIn("/cancel", self.calls())

    def test_a_failed_listing_is_never_reported_as_all_clear(self):
        """확인하지 못한 것을 "이상 없음"으로 말하면, 이 도구를 볼 이유가 사라집니다.

        push_state.sh와 notify_failure.sh에서 이미 같은 부류의 문제가 나왔습니다.
        """
        self.env["FAKE_QUEUED_FAIL"] = "1"

        result = self.run_watchdog()

        self.assertIn("[보류]", result.stdout)
        self.assertNotIn("[정상] 러너를 기다리다 멈춘 실행이 없습니다", result.stdout)

    def test_a_failed_cancel_does_not_crash_the_watchdog(self):
        stuck = dict(STUCK_RUN, run_started_at=_iso(7 * 3600), created_at=_iso(7 * 3600))
        self.set_queued([stuck])
        self.set_run(stuck)
        self.env["FAKE_CANCEL_FAIL"] = "1"

        result = self.run_watchdog()

        self.assertEqual(result.returncode, 0)
        self.assertIn("[실패]", result.stdout + result.stderr)

    # -- 정지 감지 ---------------------------------------------------------

    def test_a_long_silence_opens_an_issue(self):
        self.set_success([{"updated_at": _iso(3 * 3600)}])

        result = self.run_watchdog()

        self.assertIn("issue-create", self.calls())
        self.assertIn(f"title={watchdog.ISSUE_MARKER}", self.calls())
        self.assertIn("[정지 의심]", result.stdout)

    def test_a_recent_success_opens_nothing(self):
        self.set_success([{"updated_at": _iso(30)}])

        result = self.run_watchdog()

        self.assertNotIn("issue-create", self.calls())
        self.assertIn("[정상]", result.stdout)

    def test_an_already_open_issue_is_not_duplicated(self):
        self.set_success([{"updated_at": _iso(3 * 3600)}])
        self.set_issues([{"title": f"{watchdog.ISSUE_MARKER} (mercari-check.yml)",
                          "pull_request": None}])

        result = self.run_watchdog()

        self.assertNotIn("issue-create", self.calls())
        self.assertIn("[이슈 생략]", result.stdout)

    def test_a_pull_request_with_the_same_title_does_not_suppress_the_issue(self):
        """issues 목록에는 PR도 섞여 옵니다. 제목이 비슷한 PR 때문에 막히면 안 됩니다."""
        self.set_success([{"updated_at": _iso(3 * 3600)}])
        self.set_issues([{"title": f"{watchdog.ISSUE_MARKER} (mercari-check.yml)",
                          "pull_request": {"url": "..."}}])

        self.run_watchdog()

        self.assertIn("issue-create", self.calls())

    def test_an_unreadable_issue_list_fails_open(self):
        self.set_success([{"updated_at": _iso(3 * 3600)}])
        self.env["FAKE_ISSUES_FAIL"] = "1"

        self.run_watchdog()

        self.assertIn("issue-create", self.calls())

    def test_an_unreadable_success_listing_opens_nothing(self):
        """API가 잠깐 흔들릴 때마다 이슈가 생기면 안 됩니다."""
        self.env["FAKE_SUCCESS_FAIL"] = "1"

        result = self.run_watchdog()

        self.assertEqual(result.returncode, 0)
        self.assertNotIn("issue-create", self.calls())
        self.assertIn("[보류]", result.stdout)

    def test_the_watchdog_never_fails_its_own_run(self):
        """감시 도구가 빨간불이 되면 그것대로 잡음이 됩니다. 모든 경로에서 0으로 끝나야 합니다."""
        for broken in ("FAKE_QUEUED_FAIL", "FAKE_SUCCESS_FAIL", "FAKE_ISSUES_FAIL",
                       "FAKE_JOBS_FAIL", "FAKE_CANCEL_FAIL", "FAKE_ISSUE_CREATE_FAIL"):
            with self.subTest(broken=broken):
                self.setUp()
                stuck = dict(STUCK_RUN, run_started_at=_iso(7 * 3600), created_at=_iso(7 * 3600))
                self.set_queued([stuck])
                self.set_run(stuck)
                self.set_success([{"updated_at": _iso(3 * 3600)}])
                self.env[broken] = "1"
                self.assertEqual(self.run_watchdog().returncode, 0)


if __name__ == "__main__":
    unittest.main()
