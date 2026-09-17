"""상태 커밋 메시지에 실린 실행 계수기를 세는 자(scripts/commit_notes.py)를 검증합니다.

이 도구가 내는 숫자로 '403이 삭제인가 차단인가'를 판단하게 됩니다. 그래서 세는 자가
**틀렸을 때 틀렸다고 말하는지**를 먼저 봅니다 — 만드는 동안 실제로 한 번 통과했습니다.
눈금 숫자를 전부 한 자리로 적어 두는 바람에 `\\d+`를 `\\d`로 줄여 놓아도 자체 점검이
통과했습니다(두 자리 값을 섞어서 고쳤습니다).

**그 고침이 한 칸에만 들었습니다.** 2026-09-17에 고장 15개를 다시 넣어 보니 눈금이
**8개를 그대로 통과**시켰습니다 — 재출품 칸에만 두 자리를 섞어 뒀던 터라 `조회실패`와
`다음페이지 N회`는 여전히 `\\d`로 줄여도 통과했고, 누적(`+=`)을 덮어쓰기(`=`)로 바꿔도,
'꼬리표 첫 커밋 시각'을 망가뜨려도, `--first-parent`를 빼도, 비율의 분모를 바꿔도
통과했습니다. 아래 `CalibrationCatchesFaultsTests`가 **그 고장들을 CI에서 계속 넣어
봅니다** — 눈금이 있다는 것과 잡는다는 것은 다릅니다.
"""
import contextlib
import copy
import importlib
import io
import re
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

mercapi_stub = types.ModuleType("mercapi")
mercapi_stub.Mercapi = object
sys.modules.setdefault("mercapi", mercapi_stub)

commit_notes = importlib.import_module("commit_notes")


class ParseTests(unittest.TestCase):
    def test_a_plain_state_commit_has_no_note(self):
        """꼬리표 없음은 **None**입니다. 빈 dict로 돌려주면 '실을 것이 0건이었다'와
        '이 구간에는 계수기가 없었다'가 같은 모양이 됩니다."""
        self.assertIsNone(commit_notes.parse_note("queue Mercari alerts"))
        self.assertIsNone(commit_notes.parse_note("record Mercari alert delivery"))

    def test_a_merge_commit_with_parentheses_is_not_a_note(self):
        self.assertIsNone(
            commit_notes.parse_note("Merge pull request #38 from owner/branch (x)")
        )

    def test_a_relist_note_is_read_back_whole(self):
        note = commit_notes.parse_note(
            "queue Mercari alerts (재출품 12: 있음5/없음6/못물어봄1)"
        )
        self.assertEqual(
            note["relist"],
            {"asked": 12, "targets": 12, "alive": 5, "gone": 6, "unknown": 1, "blocked": []},
        )

    def test_asked_and_targets_are_kept_apart(self):
        """'물어본 수'와 '물어볼 수'가 다르면 못 물어본 것이 있다는 뜻입니다."""
        note = commit_notes.parse_note(
            "queue Mercari alerts (재출품 3/9: 있음1/없음1/못물어봄1 눈금⛔일반+숍스)"
        )
        self.assertEqual(note["relist"]["asked"], 3)
        self.assertEqual(note["relist"]["targets"], 9)
        self.assertEqual(note["relist"]["blocked"], ["일반", "숍스"])

    def test_next_page_and_near_miss_are_different_signals(self):
        fired = commit_notes.parse_note("queue Mercari alerts (다음페이지 2회 신규112)")
        near = commit_notes.parse_note("queue Mercari alerts (다음페이지 근접 신규93)")
        self.assertEqual(fired["next_page"], {"fired": 2, "fresh": 112})
        self.assertNotIn("near_miss", fired)
        self.assertEqual(near["near_miss"], 93)
        self.assertNotIn("next_page", near)

    def test_nothing_happened_stays_empty(self):
        """`Counter`는 0을 더해도 키가 남습니다. '아무 일도 없었다'가 빈 결과여야
        거짓 양성 점검이 의미를 가집니다."""
        tally = commit_notes.Tally()
        for _ in range(5):
            tally.add(1_700_000_000, commit_notes.parse_note("queue Mercari alerts"))
        self.assertEqual(tally.commits, 5)
        self.assertEqual(tally.with_note, 0)
        self.assertFalse(tally.relist)
        self.assertFalse(tally.next_page)


class WriterReaderAgreementTests(unittest.TestCase):
    """봇이 쓰는 쪽과 도구가 읽는 쪽이 갈리면 숫자가 조용히 틀립니다."""

    def setUp(self):
        self.check_mercari = importlib.import_module("check_mercari")
        self.check_mercari.reset_run_counters()
        self.addCleanup(self.check_mercari.reset_run_counters)

    def note(self) -> str:
        return "queue Mercari alerts" + self.check_mercari.run_note_text()

    def test_a_run_with_nothing_to_report_writes_no_note(self):
        self.assertEqual(self.check_mercari.run_note_text(), "")

    def test_a_two_digit_count_survives_the_round_trip(self):
        for key, value in (("relist_targets", 124), ("relist_asked", 124),
                           ("relist_alive", 57), ("relist_gone", 55), ("relist_unknown", 12)):
            self.check_mercari.count_event(key, value)
        note = commit_notes.parse_note(self.note())
        self.assertEqual(note["relist"]["unknown"], 12)
        self.assertEqual(note["relist"]["alive"], 57)
        self.assertEqual(note["relist"]["asked"], 124)

    def test_the_tools_own_calibration_passes(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(commit_notes.calibrate(), 0)


class CalibrationCatchesFaultsTests(unittest.TestCase):
    """눈금에 **고장을 넣어 보고** 1로 끝나는지 봅니다.

    이 클래스가 이 파일의 요점입니다. 눈금을 넣어 둔 것만으로는 부족하다는 것을
    이 저장소가 두 번 겪었습니다(2026-09-15에 한 번, 2026-09-17에 여덟 번).
    """

    def assert_caught(self, what: str):
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            result = commit_notes.calibrate()
        self.assertEqual(result, 1, f"눈금이 '{what}' 고장을 그대로 통과시켰습니다")
        self.assertIn("눈금 실패", printed.getvalue())

    def patch(self, name, value):
        original = getattr(commit_notes, name)
        setattr(commit_notes, name, value)
        self.addCleanup(setattr, commit_notes, name, original)

    def test_a_single_digit_search_failure_pattern_is_caught(self):
        """`조회실패 45`가 4로 세지는 고장. 눈금 값이 한 자리뿐이면 안 잡힙니다."""
        self.patch("SEARCH_FAIL_PATTERN", re.compile(r"조회실패 (?P<failures>\d)"))
        self.assert_caught("조회실패 자릿수")

    def test_a_single_digit_next_page_pattern_is_caught(self):
        self.patch("NEXT_PAGE_PATTERN",
                   re.compile(r"다음페이지 (?P<fired>\d)회 신규(?P<fresh>\d+)"))
        self.assert_caught("다음페이지 발동 횟수 자릿수")

    def test_losing_accumulation_is_caught(self):
        """여러 판의 값을 더하지 않고 덮어쓰는 고장. 눈금에 그 칸을 한 판만 두면
        합이 같아서 안 잡힙니다."""

        class DoesNotAccumulate(commit_notes.Tally):
            def add(self, timestamp, note):
                super().add(timestamp, note)
                if note and note.get("search_failures"):
                    self.search_failures = note["search_failures"]

        self.patch("Tally", DoesNotAccumulate)
        self.assert_caught("조회실패 누적")

    def test_losing_the_first_note_timestamp_is_caught(self):
        """'꼬리표 첫 커밋 시각'은 **'0건'과 '아직 측정이 없음'을 가르는 값**입니다."""

        class ForgetsFirstNote(commit_notes.Tally):
            def add(self, timestamp, note):
                super().add(timestamp, note)
                self.first_note_at = self.last_note_at

        self.patch("Tally", ForgetsFirstNote)
        self.assert_caught("꼬리표 첫 커밋 시각")

    def test_ignoring_the_producer_invariant_is_caught(self):
        """있음+없음+못물어봄 = 물어본 수. 봇이 그렇게 세므로 깨지면 파서가 틀린 것입니다."""

        class IgnoresInvariant(commit_notes.Tally):
            def add(self, timestamp, note):
                super().add(timestamp, note)
                self.inconsistent = 0

        self.patch("Tally", IgnoresInvariant)
        self.assert_caught("생산자 불변식")

    def test_walking_without_first_parent_is_caught(self):
        """`--first-parent`를 빼면 PR 브랜치의 옛 꼬리표가 섞여 들어옵니다."""
        original = commit_notes.walk

        def walk_all(rev_args, repo):
            return original(["--all", *rev_args] if rev_args == ["HEAD"] else rev_args, repo)

        self.patch("walk", walk_all)
        self.assert_caught("--first-parent")

    def test_reading_the_ratio_against_the_wrong_denominator_is_caught(self):
        """'못 물어봄 비율'의 분모는 **물어본 것**입니다. 물어볼 자리로 나누면
        상한에 걸린 판이 섞여 비율이 낮게 나옵니다 — 이 요청의 결론 숫자입니다."""
        original = commit_notes.report

        def report_with_wrong_denominator(tally, by_hour=False):
            swapped = copy.copy(tally)
            swapped.relist = commit_notes.Counter(tally.relist)
            swapped.relist["asked"] = tally.relist["targets"]
            original(swapped, by_hour=by_hour)

        self.patch("report", report_with_wrong_denominator)
        self.assert_caught("못 물어봄 비율의 분모")


if __name__ == "__main__":
    unittest.main()
