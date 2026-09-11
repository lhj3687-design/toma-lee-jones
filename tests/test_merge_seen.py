"""push 충돌 시 상태 병합(merge_seen.py)이 알림을 잃거나 되살리지 않는지 검증합니다."""
import importlib
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
merge = importlib.import_module("merge_seen")


class MergeSeenTests(unittest.TestCase):
    def test_recently_observed_items_move_to_the_end(self):
        # 상한으로 잘라낼 때 오래된 것부터 잘려야 합니다. 양쪽에 다 있는 매물은
        # 최근에 본 쪽(mine)의 순서를 따라 뒤로 가야 합니다.
        theirs = {"a": 1, "b": 2, "c": 3}
        mine = {"b": 20, "d": 4}
        merged = merge.merge_ordered(theirs, mine, limit=10)
        self.assertEqual(list(merged), ["a", "c", "b", "d"])
        self.assertEqual(merged["b"], 20)  # 최신 값 우선

    def test_merge_ordered_drops_the_oldest_when_over_the_limit(self):
        merged = merge.merge_ordered({"a": 1, "b": 2}, {"c": 3}, limit=2)
        self.assertEqual(list(merged), ["b", "c"])

    def test_already_sent_alerts_are_never_resurrected(self):
        # 한쪽에서 이미 보낸 알림이 다른 쪽 대기열에 남아 있어도 다시 보내면 안 됩니다.
        theirs = [{"alert_id": "new:a", "caption": "a"}, {"alert_id": "new:b", "caption": "b"}]
        mine = [{"alert_id": "new:b", "caption": "b"}]
        merged = merge.merge_pending(theirs, mine, sent_alerts=["new:a"])
        self.assertEqual([e["alert_id"] for e in merged], ["new:b"])

    def test_pending_from_both_sides_is_preserved_without_duplicates(self):
        theirs = [{"alert_id": "new:a", "caption": "a"}]
        mine = [{"alert_id": "new:b", "caption": "b"}, {"alert_id": "new:a", "caption": "a"}]
        merged = merge.merge_pending(theirs, mine, sent_alerts=[])
        self.assertEqual([e["alert_id"] for e in merged], ["new:a", "new:b"])

    def test_legacy_entries_without_alert_id_get_one(self):
        merged = merge.merge_pending([{"caption": "옛날 알림"}], [], sent_alerts=[])
        self.assertEqual(merged[0]["alert_id"], "legacy:옛날 알림")

    def test_keyword_checkpoints_keep_the_later_time(self):
        merged = merge.merge_checked_at({"k1": 100, "k2": 500}, {"k1": 300, "k3": 700})
        self.assertEqual(merged, {"k1": 300, "k2": 500, "k3": 700})

    def test_keyword_checkpoints_never_go_backwards(self):
        # 뒤로 가면 이미 알린 매물을 다시 신규로 볼 수 있습니다.
        merged = merge.merge_checked_at({"k1": 900}, {"k1": 100})
        self.assertEqual(merged, {"k1": 900})

    def test_unsupported_fingerprint_formats_are_dropped(self):
        merged = merge.prune_fingerprints(
            {"seller:1:t": {}, "title:t:100": {}, "seller-photo:1:t:url": {}}
        )
        self.assertEqual(sorted(merged), ["seller:1:t", "title:t:100"])

    def test_sent_alerts_are_deduplicated_and_capped(self):
        merged = merge.unique_recent(["a", "b", "a", "c"], limit=2)
        self.assertEqual(merged, ["b", "c"])


if __name__ == "__main__":
    unittest.main()
