"""push 충돌 시 상태 병합(merge_seen.py)이 알림을 잃거나 되살리지 않는지 검증합니다."""
import importlib
import json
import sys
import tempfile
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

    def test_pending_entries_without_a_caption_are_dropped(self):
        # 본문 없는 항목은 전송 단계에서 터지고, 터지면 대기열에 남아 다음 실행도 터집니다.
        # check_mercari.py와 기준이 다르면 충돌 병합 때마다 그 항목이 되살아납니다.
        merged = merge.merge_pending(
            [{"alert_id": "new:broken"}, {"alert_id": "new:blank", "caption": "  "}],
            [{"alert_id": "new:ok", "caption": "정상"}],
            sent_alerts=[],
        )
        self.assertEqual([e["alert_id"] for e in merged], ["new:ok"])

    def test_unreadable_local_state_aborts_the_merge(self):
        """/tmp/mine.json은 방금 이 실행이 모은 결과입니다.

        읽지 못했을 때 빈 dict로 갈음하면 원격 상태만 남은 파일을 '병합 결과'라며
        push해서, 이번 실행의 알림과 가격 기록이 소리 없이 사라집니다.
        병합을 실패시키면 push_state.sh가 실패로 받아 상태를 보존합니다.
        """
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as broken:
            broken.write("{이건 JSON이 아닙니다")
        with self.assertRaises(SystemExit):
            merge.read_json(broken.name, required=True)
        with self.assertRaises(SystemExit):
            merge.read_json("/존재하지/않는/경로.json", required=True)
        # 원격 상태(theirs)는 원격에 파일이 없을 때 정상적으로 비어 있을 수 있어 관대합니다.
        self.assertEqual(merge.read_json("/존재하지/않는/경로.json"), {})

    def test_fingerprints_with_unexpected_values_are_dropped(self):
        # 값이 dict가 아니면 check_mercari.py가 조회 도중 죽습니다. 병합에서도 같이 걸러야
        # 충돌이 날 때마다 되살아나지 않습니다.
        merged = merge.prune_fingerprints({"seller:1:t": {"item_id": "m1"}, "seller:2:t": "m2"})
        self.assertEqual(sorted(merged), ["seller:1:t"])

    def test_unsupported_fingerprint_formats_are_dropped(self):
        merged = merge.prune_fingerprints(
            {"seller:1:t": {}, "title:t:100": {}, "seller-photo:1:t:url": {}}
        )
        self.assertEqual(sorted(merged), ["seller:1:t", "title:t:100"])

    def test_unknown_seller_fingerprints_are_dropped(self):
        # check_mercari.py와 같은 기준이어야 합니다. 한쪽만 정리하면 충돌 병합 때 되살아납니다.
        merged = merge.prune_fingerprints({"seller:0:shop": {}, "seller:9:real": {}})
        self.assertEqual(sorted(merged), ["seller:9:real"])

    def test_caps_match_the_bot(self):
        """상한이 양쪽에 따로 적혀 있어 어긋나기 쉽습니다.

        merge_seen.py 쪽이 더 작으면 push 충돌 병합이 일어날 때마다 상태가 조용히
        깎여 나가고, 더 크면 상한이 무의미해집니다. 값이 어긋나면 여기서 잡습니다.
        """
        import importlib, sys, types

        stub = types.ModuleType("mercapi")
        stub.Mercapi = object
        sys.modules.setdefault("mercapi", stub)
        bot = importlib.import_module("check_mercari")

        for name in ("MAX_SEEN_ITEMS", "MAX_RELIST_FINGERPRINTS", "MAX_SENT_ALERTS", "MAX_PENDING_ALERTS"):
            self.assertEqual(getattr(merge, name), getattr(bot, name), name)
        self.assertEqual(merge.SUPPORTED_FINGERPRINT_PREFIXES, bot.SUPPORTED_FINGERPRINT_PREFIXES)
        self.assertEqual(merge.UNKNOWN_SELLER_IDS, bot.UNKNOWN_SELLER_IDS)

        # 지문 정리 기준도 양쪽이 같아야 합니다(한쪽만 정리하면 충돌 병합 때 되살아납니다).
        samples = {
            "seller:1:t": {"item_id": "m1"},
            "seller:0:shop": {"item_id": "m2"},
            "seller:3:t": "dict가 아님",
            "unknown:1:t": {"item_id": "m3"},
        }
        self.assertEqual(
            sorted(merge.prune_fingerprints(dict(samples))),
            sorted(bot.prune_fingerprints(dict(samples))),
        )

    def test_sent_alerts_are_deduplicated_and_capped(self):
        merged = merge.unique_recent(["a", "b", "a", "c"], limit=2)
        self.assertEqual(merged, ["b", "c"])


if __name__ == "__main__":
    unittest.main()
