"""scripts/merge_audit.py가 '병합에서 뒤집힌 값'을 실제로 집어내는지 검증합니다.

이 스크립트도 **결론이 되는 숫자**를 내는 자리입니다. 세는 법이 한 군데 틀리면 숫자가
통째로 달라지고, 그 숫자로 다음 라운드의 작업이 정해집니다. 만드는 동안 실제로 두 번
틀렸고, 둘 다 스크립트 자신의 자체 점검이 잡았습니다.

  - `collections.Counter`는 **0을 더해도 키가 생깁니다.** 그래서 아무것도 안 바뀐 판을
    넣었는데 "신호 있음"으로 읽혔습니다.
  - 구간 첫 판은 비교 상대가 없어 **경계에서 한 건이 조용히 빠집니다.** 눈금이 245건
    대신 244건으로 나왔고, 씨앗 한 판을 앞에서 가져와야 맞았습니다.
"""
import importlib
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
merge_audit = importlib.import_module("merge_audit")


def state(**overrides) -> dict:
    base = {
        "seen": {"m1": {"last_alert_price": 5000, "last_seen_price": 5000}},
        "pending": [],
        "sent_alerts": ["new:m1"],
        "relist_fingerprints": {"seller:A:t": {"item_id": "m1", "last_alert_price": 5000,
                                               "last_seen_price": 5000}},
        "keyword_checked_at": {"kw": 1000},
        "pending_relists": {"m2": {"matched_id": "m1", "since": 900, "checks": 2,
                                   "checked_at": 950, "fresh": True}},
    }
    base.update(overrides)
    return base


class CompareTests(unittest.TestCase):
    def test_an_unchanged_pair_raises_no_signal(self):
        """거짓 양성이 하나라도 있으면 실제 신호를 그 안에서 못 찾습니다."""
        self.assertEqual(dict(merge_audit.compare(state(), state())), {})

    def test_a_rising_price_floor_is_counted(self):
        found = merge_audit.compare(
            state(), state(seen={"m1": {"last_alert_price": 6000, "last_seen_price": 6000}}))
        self.assertEqual(found["seen 기준가 상승"], 1)

    def test_a_falling_price_floor_is_not_counted(self):
        """기준가는 내려가는 것이 정상입니다 — 그것까지 세면 숫자가 통째로 부풉니다."""
        found = merge_audit.compare(
            state(), state(seen={"m1": {"last_alert_price": 4000, "last_seen_price": 4000}}))
        self.assertEqual(found["seen 기준가 상승"], 0)

    def test_a_fingerprint_holding_another_items_floor_is_counted(self):
        """지문은 `seen[item_id]`에서 그대로 베껴 쓰므로 값이 다르면 밖에서 고쳐 쓴 것입니다."""
        found = merge_audit.compare(state(), state(
            relist_fingerprints={"seller:A:t": {"item_id": "m9", "last_alert_price": 5000,
                                                "last_seen_price": 9000}},
            seen={"m1": {"last_alert_price": 5000, "last_seen_price": 5000},
                  "m9": {"last_alert_price": 9000, "last_seen_price": 9000}}))
        self.assertEqual(found["지문 기준가 어긋남(지문<매물)"], 1)

    def test_price_keyed_fingerprints_are_left_alone(self):
        """`title:` 지문은 키에 가격이 박혀 있어 가격이 바뀌면 옛 키가 옛 값을 들고 남습니다.

        정당한 어긋남이라 세면 안 됩니다. 세면 이 신호가 잡음에 묻힙니다.
        """
        found = merge_audit.compare(state(), state(
            relist_fingerprints={"title:t:5000": {"item_id": "m1", "last_alert_price": 1000,
                                                  "last_seen_price": 1000}}))
        self.assertEqual(found["지문 기준가 어긋남(지문<매물)"], 0)

    def test_a_checkpoint_going_backwards_is_counted(self):
        found = merge_audit.compare(state(), state(keyword_checked_at={"kw": 500}))
        self.assertEqual(found["조회 시각 후퇴"], 1)

    def test_a_lost_delivery_record_is_counted(self):
        self.assertEqual(merge_audit.compare(state(), state(sent_alerts=[]))["전송 기록 유실"], 1)

    def test_an_already_sent_alert_back_in_the_queue_is_counted(self):
        found = merge_audit.compare(
            state(), state(pending=[{"alert_id": "new:m1", "caption": "다시"}]))
        self.assertEqual(found["대기열 부활"], 1)

    def test_a_pending_relist_clock_going_backwards_is_counted(self):
        found = merge_audit.compare(state(), state(
            pending_relists={"m2": {"matched_id": "m1", "since": 900, "checks": 1,
                                    "checked_at": 950, "fresh": True}}))
        self.assertEqual(found["보류 시계 후퇴"], 1)

    def test_an_owner_change_that_keeps_the_staler_record_is_counted(self):
        """주인이 바뀌면 시계를 다시 세는 것은 맞습니다. 옛 기록이 이기는 것이 문제입니다."""
        found = merge_audit.compare(state(), state(
            pending_relists={"m2": {"matched_id": "m8", "since": 800, "checks": 0,
                                    "checked_at": 800, "fresh": True}}))
        self.assertEqual(found["보류 시계 후퇴"], 1)

    def test_an_owner_change_that_keeps_the_fresher_record_is_not_counted(self):
        found = merge_audit.compare(state(), state(
            pending_relists={"m2": {"matched_id": "m8", "since": 1200, "checks": 0,
                                    "checked_at": 1200, "fresh": True}}))
        self.assertEqual(found["보류 시계 후퇴"], 0)

    def test_losing_the_first_sight_freshness_is_counted(self):
        found = merge_audit.compare(state(), state(
            pending_relists={"m2": {"matched_id": "m1", "since": 900, "checks": 2,
                                    "checked_at": 950, "fresh": False}}))
        self.assertEqual(found["fresh 소실"], 1)


class SelfTestTests(unittest.TestCase):
    def test_the_scripts_own_self_test_passes(self):
        """'못 잡는 도구로 이상 없다고 하면 안 된다'를 스크립트 안에 넣어 둔 자리입니다."""
        import contextlib
        import io

        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(merge_audit.self_test())


if __name__ == "__main__":
    unittest.main()
