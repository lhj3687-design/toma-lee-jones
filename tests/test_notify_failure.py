"""scripts/notify_failure.sh가 '알렸다'고 거짓 보고하지 않는지 검증합니다.

이 스크립트는 알림 경로의 **마지막 수단**입니다. 검색 실패·키워드 정체·등록 시각
방어선 같은 고장은 전부 대기열을 거쳐 텔레그램으로 나가는데, 텔레그램 자체가 막히면
그 경로가 통째로 죽습니다. 그때 남는 건 "실행을 실패로 끝내고 이 스크립트가 별도의
curl 경로로 한 번 더 시도한다"는 것뿐입니다.

그래서 여기서 거짓 성공을 보고하면, 알림이 멈춘 사실을 확인할 마지막 수단까지
사라집니다. push_state.sh에서 겪었던 것과 같은 종류의 문제입니다.
"""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# gh api ... --jq EXPR 흉내. 실제 jq로 같은 표현식을 픽스처에 적용하므로,
# 스크립트가 쓰는 jq 표현식(취소된 실행 건너뛰기)까지 함께 검증됩니다.
FAKE_GH = """#!/usr/bin/env bash
# gh api 를 세 갈래로 흉내 냅니다: 지난 실행 조회 / 열린 이슈 조회 / 이슈 생성.
# --jq 는 실제 jq로 적용하므로, 스크립트가 쓰는 표현식(취소된 실행 건너뛰기,
# 목록에서 PR 걸러내기)까지 함께 검증됩니다.
endpoint=""
expr=""
creating=""
while [ $# -gt 0 ]; do
  case "$1" in
    api) shift;;
    --jq) expr="$2"; shift 2;;
    -f) printf '%s\n' "$2" >> "$FAKE_ISSUE_LOG"; creating=1; shift 2;;
    -*) shift;;
    *) [ -z "$endpoint" ] && endpoint="$1"; shift;;
  esac
done

case "$endpoint" in
  *actions/workflows*)
    [ -n "${FAKE_GH_FAIL:-}" ] && { echo "gh: simulated failure" >&2; exit 1; }
    jq -r "$expr" "$FAKE_RUNS_FILE"
    ;;
  *issues\?state=open*)
    [ -n "${FAKE_ISSUE_LIST_FAIL:-}" ] && { echo "gh: simulated failure" >&2; exit 1; }
    jq -r "$expr" "$FAKE_ISSUES_FILE"
    ;;
  *)
    if [ -n "$creating" ]; then
      [ -n "${FAKE_ISSUE_CREATE_FAIL:-}" ] && { echo "gh: simulated failure" >&2; exit 1; }
      echo "created" >> "$FAKE_ISSUE_LOG"
      exit 0
    fi
    exit 1
    ;;
esac
"""

# 실제 curl의 -o/-w 동작만 흉내 냅니다. 중요한 성질 하나를 그대로 지킵니다:
# --fail 없이는 HTTP 401/400에도 **종료 코드 0**을 돌려준다는 것.
FAKE_CURL = """#!/usr/bin/env bash
echo "$@" >> "$FAKE_CURL_LOG"
out=""
prev=""
for arg in "$@"; do
  if [ "$prev" = "-o" ]; then out="$arg"; fi
  prev="$arg"
done
if [ -n "${FAKE_TRANSPORT_ERROR:-}" ]; then
  echo "curl: (7) Failed to connect" >&2
  printf '000'
  exit 0
fi
if [ -n "$out" ]; then printf '%s' "${FAKE_TELEGRAM_BODY}" > "$out"; fi
printf '%s' "${FAKE_TELEGRAM_STATUS}"
exit 0
"""

OK_BODY = '{"ok":true,"result":{"message_id":1}}'
UNAUTHORIZED_BODY = '{"ok":false,"error_code":401,"description":"Unauthorized"}'


@unittest.skipIf(shutil.which("jq") is None, "jq가 없는 환경")
class NotifyFailureTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        base = Path(self.directory.name)

        self.fake_bin = base / "bin"
        self.fake_bin.mkdir()
        for name, body in (("gh", FAKE_GH), ("curl", FAKE_CURL)):
            path = self.fake_bin / name
            path.write_text(body)
            path.chmod(0o755)

        self.runs_file = base / "runs.json"
        self.issues_file = base / "issues.json"
        self.issues_file.write_text("[]")  # 기본: 열려 있는 알림 이슈 없음
        self.curl_log = base / "curl.log"
        self.curl_log.write_text("")
        self.issue_log = base / "issue.log"
        self.issue_log.write_text("")

        self.env = dict(os.environ)
        self.env.update(
            {
                "PATH": os.pathsep.join([str(self.fake_bin), os.environ["PATH"]]),
                "TELEGRAM_BOT_TOKEN": "token",
                "TELEGRAM_CHAT_ID": "chat",
                "GITHUB_REPOSITORY": "owner/repo",
                "GITHUB_SERVER_URL": "https://github.com",
                "GITHUB_RUN_ID": "12345",
                "FAKE_RUNS_FILE": str(self.runs_file),
                "FAKE_ISSUES_FILE": str(self.issues_file),
                "FAKE_CURL_LOG": str(self.curl_log),
                "FAKE_ISSUE_LOG": str(self.issue_log),
                "FAKE_TELEGRAM_STATUS": "200",
                "FAKE_TELEGRAM_BODY": OK_BODY,
            }
        )

    def run_script(self, conclusions, **overrides):
        """직전 실행들의 결론을 주고 스크립트를 돌립니다(최신 순)."""
        self.runs_file.write_text(
            '{"workflow_runs": [' + ", ".join(f'{{"conclusion": "{c}"}}' for c in conclusions) + "]}"
        )
        env = dict(self.env, **{k: str(v) for k, v in overrides.items()})
        return subprocess.run(
            ["bash", str(ROOT / "scripts" / "notify_failure.sh")],
            env=env,
            capture_output=True,
            text=True,
        )

    def telegram_called(self) -> bool:
        return bool(self.curl_log.read_text().strip())

    def issue_created(self) -> bool:
        return "created" in self.issue_log.read_text()

    def issue_text(self) -> str:
        return self.issue_log.read_text()

    def test_a_first_failure_after_a_success_is_reported(self):
        result = self.run_script(["success", "success"])

        self.assertTrue(self.telegram_called())
        self.assertIn("실패 알림을 텔레그램으로 보냈습니다", result.stdout)
        self.assertIn("12345", self.curl_log.read_text())  # 실행 로그 링크가 들어갑니다

    def test_a_repeated_failure_is_not_reported_again(self):
        # 1분 주기라 실패할 때마다 알리면 하루 1400건이 넘습니다.
        result = self.run_script(["failure", "success"])

        self.assertFalse(self.telegram_called())
        self.assertIn("직전 실행도 실패했습니다", result.stdout)

    def test_cancelled_runs_do_not_hide_a_real_failure(self):
        # 1분 주기에서는 실행이 겹칠 때마다 취소가 생깁니다(실측 약 9%).
        # 취소를 '정상 아님'으로 세면 취소 직후의 진짜 실패가 묻힙니다.
        result = self.run_script(["cancelled", "cancelled", "success"])

        self.assertTrue(self.telegram_called())
        self.assertIn("실패 알림을 텔레그램으로 보냈습니다", result.stdout)

    def test_an_unknown_history_still_reports(self):
        # 조회에 실패하면 놓치는 쪽보다 한 번 더 알리는 쪽이 낫습니다.
        result = self.run_script(["success"], FAKE_GH_FAIL="1")

        self.assertTrue(self.telegram_called())
        self.assertIn("확인하지 못했습니다", result.stdout)

    def test_a_rejected_telegram_call_is_never_reported_as_sent(self):
        """토큰이 바뀌었거나 채팅 ID가 틀린 상황입니다.

        curl은 --fail 없이는 HTTP 401에도 종료 코드 0을 돌려줍니다. 종료 코드만 보면
        아무것도 전송되지 않았는데 '보냈습니다'라고 로그에 남고, 알림이 멈춘 사실을
        확인할 마지막 수단까지 거짓말을 하게 됩니다.
        """
        result = self.run_script(
            ["success"], FAKE_TELEGRAM_STATUS="401", FAKE_TELEGRAM_BODY=UNAUTHORIZED_BODY
        )

        self.assertNotIn("실패 알림을 텔레그램으로 보냈습니다", result.stdout)
        self.assertIn("전송되지 않았습니다", result.stderr)
        self.assertIn("401", result.stderr)

    def test_a_network_failure_is_never_reported_as_sent(self):
        result = self.run_script(["success"], FAKE_TRANSPORT_ERROR="1")

        self.assertNotIn("실패 알림을 텔레그램으로 보냈습니다", result.stdout)
        self.assertIn("전송되지 않았습니다", result.stderr)

    def test_missing_telegram_settings_fall_back_to_a_github_issue(self):
        """시크릿이 비어 있으면 텔레그램은 영영 안 됩니다.

        예전에는 여기서 조용히 끝냈는데, 그건 '알릴 수단이 아예 없는 상태'를
        아무에게도 알리지 않는 것과 같습니다.
        """
        result = self.run_script(["success"], TELEGRAM_BOT_TOKEN="")

        self.assertFalse(self.telegram_called())
        self.assertTrue(self.issue_created())
        self.assertIn("시크릿이 비어 있습니다", self.issue_text())
        self.assertEqual(result.returncode, 0)

    def test_a_delivered_telegram_alert_does_not_open_an_issue(self):
        # 텔레그램이 멀쩡하면 이슈는 군더더기입니다.
        self.run_script(["success"])

        self.assertTrue(self.telegram_called())
        self.assertFalse(self.issue_created())

    def test_a_blocked_telegram_falls_back_to_a_github_issue(self):
        """텔레그램이 막히면 고장 여섯 가지가 전부 같은 구멍으로 빠집니다.

        같은 채널로 다시 시도해 봐야 소용없으므로, GitHub이 소유자에게 메일을 보내 주는
        독립된 경로로 알립니다.
        """
        result = self.run_script(
            ["success"], FAKE_TELEGRAM_STATUS="401", FAKE_TELEGRAM_BODY=UNAUTHORIZED_BODY
        )

        self.assertTrue(self.issue_created())
        self.assertIn("GitHub 이슈로 알렸습니다", result.stdout)
        created = self.issue_text()
        self.assertIn("401", created)  # 막힌 이유가 이슈에 남습니다
        self.assertIn("12345", created)  # 실행 로그 링크도
        self.assertEqual(result.returncode, 0)

    def test_an_already_open_issue_is_not_duplicated(self):
        # 고장이 반복돼도 이슈가 쌓이면 안 됩니다.
        self.issues_file.write_text(
            '[{"title": "⚠️ 알림 경로가 막혔습니다 (mercari-check.yml)", "pull_request": null}]'
        )

        result = self.run_script(["success"], FAKE_TELEGRAM_STATUS="401")

        self.assertFalse(self.issue_created())
        self.assertIn("이미 열려 있는", result.stdout)

    def test_a_pull_request_is_not_mistaken_for_an_open_issue(self):
        # GitHub의 issues 목록에는 PR도 섞여 옵니다. 걸러내지 않으면 제목이 비슷한 PR
        # 하나 때문에 이슈가 영영 안 만들어집니다.
        self.issues_file.write_text(
            '[{"title": "⚠️ 알림 경로가 막혔습니다 수정", "pull_request": {"url": "x"}}]'
        )

        self.run_script(["success"], FAKE_TELEGRAM_STATUS="401")

        self.assertTrue(self.issue_created())

    def test_an_unreadable_issue_list_still_opens_an_issue(self):
        # 조회에 실패해도 놓치는 것보다 하나 더 만드는 편이 낫습니다.
        # 이 경로는 전환 시점에만 돌기 때문에 한 번을 넘길 수 없습니다.
        self.run_script(["success"], FAKE_TELEGRAM_STATUS="401", FAKE_ISSUE_LIST_FAIL="1")

        self.assertTrue(self.issue_created())

    def test_a_failed_issue_creation_is_reported_not_swallowed(self):
        # 여기까지 실패하면 사람에게 닿는 경로가 남아 있지 않습니다. 로그에는 남겨야 합니다.
        result = self.run_script(
            ["success"], FAKE_TELEGRAM_STATUS="401", FAKE_ISSUE_CREATE_FAIL="1"
        )

        self.assertIn("이슈 생성도 실패", result.stderr)
        self.assertEqual(result.returncode, 0)

    def test_the_squash_workflow_gets_its_own_wording(self):
        # 알림 봇 본체가 멈춘 것과 이력 압축이 밀린 것은 심각도가 다릅니다.
        self.run_script(["success"], WORKFLOW_FILE="squash-history.yml")

        sent = self.curl_log.read_text()
        self.assertIn("이력 압축 실패", sent)
        self.assertNotIn("알림봇 실행 실패", sent)

    def test_the_script_never_fails_the_run_it_reports_on(self):
        # 이미 실패한 실행에서 도는 스크립트가 상황을 더 나쁘게 만들면 안 됩니다.
        for overrides in (
            {},
            {"FAKE_TELEGRAM_STATUS": "401"},
            {"FAKE_TRANSPORT_ERROR": "1"},
            {"FAKE_TELEGRAM_STATUS": "401", "FAKE_ISSUE_CREATE_FAIL": "1"},
            {"TELEGRAM_BOT_TOKEN": ""},
        ):
            with self.subTest(상황=overrides or "정상"):
                result = self.run_script(["success"], **overrides)
                self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
