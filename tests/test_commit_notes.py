"""상태 커밋 메시지에 실린 실행 계수기를 세는 자(scripts/commit_notes.py)를 검증합니다.

이 도구가 내는 숫자로 '403이 삭제인가 차단인가'를 판단하게 됩니다. 그래서 세는 자가
**틀렸을 때 틀렸다고 말하는지**를 먼저 봅니다 — 만드는 동안 실제로 한 번 통과했습니다.
눈금 숫자를 전부 한 자리로 적어 두는 바람에 `\\d+`를 `\\d`로 줄여 놓아도 자체 점검이
통과했습니다(두 자리 값을 섞어서 고쳤습니다).
"""
import importlib
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
        self.assertEqual(commit_notes.calibrate(), 0)


if __name__ == "__main__":
    unittest.main()
