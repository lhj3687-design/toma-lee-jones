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
if [ -n "${FAKE_GH_FAIL:-}" ]; then
  echo "gh: simulated failure" >&2
  exit 1
fi
expr=""
while [ $# -gt 0 ]; do
  case "$1" in
    --jq) expr="$2"; shift 2;;
    *) shift;;
  esac
done
jq -r "$expr" "$FAKE_RUNS_FILE"
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
        self.curl_log = base / "curl.log"
        self.curl_log.write_text("")

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
                "FAKE_CURL_LOG": str(self.curl_log),
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

    def test_missing_telegram_settings_are_skipped_quietly(self):
        result = self.run_script(["success"], TELEGRAM_BOT_TOKEN="")

        self.assertFalse(self.telegram_called())
        self.assertEqual(result.returncode, 0)

    def test_the_squash_workflow_gets_its_own_wording(self):
        # 알림 봇 본체가 멈춘 것과 이력 압축이 밀린 것은 심각도가 다릅니다.
        self.run_script(["success"], WORKFLOW_FILE="squash-history.yml")

        sent = self.curl_log.read_text()
        self.assertIn("이력 압축 실패", sent)
        self.assertNotIn("알림봇 실행 실패", sent)

    def test_the_script_never_fails_the_run_it_reports_on(self):
        # 이미 실패한 실행에서 도는 스크립트가 상황을 더 나쁘게 만들면 안 됩니다.
        for overrides in ({}, {"FAKE_TELEGRAM_STATUS": "401"}, {"FAKE_TRANSPORT_ERROR": "1"}):
            with self.subTest(상황=overrides or "정상"):
                result = self.run_script(["success"], **overrides)
                self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
