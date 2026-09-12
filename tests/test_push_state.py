"""scripts/push_state.sh가 '저장했다'고 거짓 보고하지 않는지 검증합니다.

이 스크립트의 성공/실패는 봇에서 단순한 종료 코드가 아닙니다. 전송 단계는 알림 하나를
보낼 때마다 이걸 호출하고, 성공 신호를 "전송 기록이 원격에 확정됐다"는 뜻으로 읽어
다음 알림으로 넘어갑니다. 그래서 push하지 않았는데 성공이라고 하면, 실행이 중간에
끊길 때 이미 보낸 알림이 다음 실행에서 다시 나갑니다.

실제로 그런 경로가 있었습니다. push가 거부된 뒤(1분 주기에서 흔한 일) 원격 상태를
받아오는 fetch가 일시적으로 실패하면, 다음 바퀴에서 커밋은 이미 해 둔 상태라
"변경 없음"으로 보여 exit 0으로 끝났습니다.
"""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 재시도 대기(최대 30초)를 실제로 기다리면 테스트가 봇 실행 주기를 잡아먹습니다.
# sleep만 가짜로 바꿔 두고 나머지 동작은 그대로 확인합니다.
FAKE_SLEEP = "#!/bin/sh\nexit 0\n"

# fetch만 네트워크 오류로 실패시키고 나머지 git 명령은 그대로 통과시킵니다.
FAKE_GIT = """#!/bin/sh
if [ "$1" = "fetch" ]; then
  echo "fatal: unable to access remote: simulated network failure" >&2
  exit 128
fi
exec {git} "$@"
"""


@unittest.skipIf(shutil.which("git") is None, "git이 없는 환경")
class PushStateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        base = Path(self.directory.name)

        # 주변 환경의 git 설정(예: push.negotiate)이 결과를 흔들지 않도록 격리합니다.
        self.env = dict(os.environ)
        self.env.update(
            {
                "GIT_CONFIG_GLOBAL": str(base / "gitconfig"),
                "GIT_CONFIG_SYSTEM": str(base / "gitconfig"),
                "GIT_AUTHOR_NAME": "test",
                "GIT_AUTHOR_EMAIL": "test@example.com",
                "GIT_COMMITTER_NAME": "test",
                "GIT_COMMITTER_EMAIL": "test@example.com",
            }
        )
        (base / "gitconfig").write_text("")

        self.remote = base / "remote.git"
        self.work = base / "work"
        self.git("init", "-q", "--bare", str(self.remote), cwd=base)
        self.git("symbolic-ref", "HEAD", "refs/heads/main", cwd=self.remote)

        self.git("init", "-q", "-b", "main", str(self.work), cwd=base)
        (self.work / "scripts").mkdir()
        shutil.copy(ROOT / "scripts" / "push_state.sh", self.work / "scripts")
        shutil.copy(ROOT / "merge_seen.py", self.work)
        self.write_state('{"seen": {"shared": 1}}')
        self.git("add", "-A")
        self.git("commit", "-qm", "init")
        self.git("remote", "add", "origin", str(self.remote))
        self.git("push", "-q", "-u", "origin", "main")

        # sleep과 fetch를 바꿔 끼울 수 있도록 가짜 실행 파일을 준비합니다.
        self.fake_bin = base / "fakebin"
        self.fake_bin.mkdir()
        self.write_executable(self.fake_bin / "sleep", FAKE_SLEEP)
        self.fake_git_bin = base / "fakegit"
        self.fake_git_bin.mkdir()
        self.write_executable(
            self.fake_git_bin / "git", FAKE_GIT.format(git=shutil.which("git"))
        )

    def write_executable(self, path: Path, body: str) -> None:
        path.write_text(body)
        path.chmod(0o755)

    def write_state(self, text: str) -> None:
        (self.work / "seen_items.json").write_text(text)

    def git(self, *args, cwd=None):
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd or self.work),
            env=self.env,
            capture_output=True,
            text=True,
            check=True,
        )

    def remote_state(self) -> str:
        return self.git("show", "main:seen_items.json", cwd=self.remote).stdout

    def advance_remote(self, text: str) -> None:
        """다른 실행이 먼저 상태를 올린 상황을 만듭니다(push 거부를 유발)."""
        other = Path(self.directory.name) / "other"
        self.git("clone", "-q", str(self.remote), str(other), cwd=self.directory.name)
        (other / "seen_items.json").write_text(text)
        self.git("add", "-A", cwd=other)
        self.git("commit", "-qm", "concurrent run", cwd=other)
        self.git("push", "-q", "origin", "main", cwd=other)

    def push_state(self, message="queue Mercari alerts", break_fetch=False):
        path = [str(self.fake_bin)]
        if break_fetch:
            path.insert(0, str(self.fake_git_bin))
        env = dict(self.env, PATH=os.pathsep.join(path + [self.env["PATH"]]))
        return subprocess.run(
            ["bash", "scripts/push_state.sh", message],
            cwd=str(self.work),
            env=env,
            capture_output=True,
            text=True,
        )

    def test_a_failed_fetch_after_a_rejected_push_is_never_reported_as_saved(self):
        self.advance_remote('{"seen": {"theirs": 1}}')
        self.write_state('{"seen": {"mine": 1}}')

        result = self.push_state(break_fetch=True)

        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("상태 저장 완료", result.stdout)
        # 원격은 그대로이고, 이번 실행이 모은 상태는 작업 트리에 남아 있어야 합니다.
        self.assertNotIn("mine", self.remote_state())
        self.assertIn("mine", (self.work / "seen_items.json").read_text())

    def test_a_second_call_does_not_inherit_a_false_success(self):
        # 워크플로는 저장 단계를 여러 번 부릅니다(수집 직후, 전송 건별, 마지막 안전망).
        # 앞 호출이 push하지 못한 커밋을 남겨 뒀다면 다음 호출도 성공이라고 해서는 안 됩니다.
        self.advance_remote('{"seen": {"theirs": 1}}')
        self.write_state('{"seen": {"mine": 1}}')
        self.push_state(break_fetch=True)

        result = self.push_state("record Mercari alert delivery", break_fetch=True)

        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("상태 변경 없음", result.stdout)

    def test_a_rejected_push_recovers_by_merging_both_sides(self):
        # 고쳐진 뒤에도 평소의 충돌 복구 경로는 그대로 동작해야 합니다.
        self.advance_remote('{"seen": {"shared": 1, "theirs": 1}}')
        self.write_state('{"seen": {"shared": 1, "mine": 1}}')

        result = self.push_state()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("상태 저장 완료", result.stdout)
        merged = self.remote_state()
        self.assertIn("theirs", merged)  # 다른 실행의 결과를 덮어쓰지 않음
        self.assertIn("mine", merged)  # 이번 실행의 결과도 살아 있음

    def test_nothing_to_push_is_still_a_quiet_success(self):
        result = self.push_state()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("상태 변경 없음", result.stdout)

    def test_a_plain_change_is_pushed(self):
        self.write_state('{"seen": {"shared": 1, "fresh": 1}}')

        result = self.push_state()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("fresh", self.remote_state())


if __name__ == "__main__":
    unittest.main()
