"""push 충돌 시 상태 병합(merge_seen.py)이 알림을 잃거나 되살리지 않는지 검증합니다."""
import importlib
import contextlib
import io
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

    def test_the_baseline_price_never_goes_up_through_a_merge(self):
        """운영에서 실제로 되돌아간 자리입니다.

        `last_alert_price`는 '실제로 알림을 보낸 역대 최저가'라 내려가기만 해야 합니다.
        그런데 병합이 양쪽에 있는 키를 mine 으로 덮는데, 내 실행이 **먼저 시작했으면**
        그 값은 상대가 이미 내려놓은 기준을 모르는 옛날 값입니다.

        m47315804624: 인하 알림 `30000 -> 28888`이 나간 뒤, 기준가가 28888에서 30000으로
        되돌아갔습니다(2026-09-13 17:44, 17:46 두 번). 실측 구간에서 이렇게 올라간 자리가
        245건이고 그중 210건이 '알림으로 내려간 값에서 그 직전 값으로 정확히 되돌아감'
        이었습니다.
        """
        theirs = {"m47315804624": {"last_alert_price": 28888, "last_seen_price": 28888}}
        mine = {"m47315804624": {"last_alert_price": 30000, "last_seen_price": 30000}}
        merged = merge.merge_ordered(theirs, mine, limit=10,
                                     merge_values=merge.merge_price_record)
        self.assertEqual(merged["m47315804624"]["last_alert_price"], 28888)
        # 참고용 값과 순서는 그대로 mine 을 따릅니다.
        self.assertEqual(merged["m47315804624"]["last_seen_price"], 30000)

    def test_a_lower_baseline_from_my_side_still_wins(self):
        """내 쪽이 더 낮으면 내 쪽이 맞습니다 — 방향이 하나뿐인 규칙입니다."""
        theirs = {"x": {"last_alert_price": 30000, "last_seen_price": 30000}}
        mine = {"x": {"last_alert_price": 28888, "last_seen_price": 28888}}
        merged = merge.merge_ordered(theirs, mine, limit=10,
                                     merge_values=merge.merge_price_record)
        self.assertEqual(merged["x"]["last_alert_price"], 28888)

    def test_a_baseline_that_only_one_side_has_is_kept(self):
        """한쪽만 기준가를 세웠으면 그것을 씁니다. 기준이 없는 쪽이 이기면 안 됩니다."""
        theirs = {"x": {"last_alert_price": 5000, "last_seen_price": 5000}}
        mine = {"x": {"last_alert_price": None, "last_seen_price": None}}
        merged = merge.merge_ordered(theirs, mine, limit=10,
                                     merge_values=merge.merge_price_record)
        self.assertEqual(merged["x"]["last_alert_price"], 5000)

    def test_legacy_plain_price_entries_still_merge(self):
        """예전 상태 파일은 매물마다 dict 가 아니라 가격 하나만 들고 있었습니다."""
        merged = merge.merge_ordered({"x": 28888}, {"x": 30000}, limit=10,
                                     merge_values=merge.merge_price_record)
        self.assertEqual(merged["x"], 28888)

    def test_fingerprints_carry_the_same_rule(self):
        """지문이 들고 있는 기준가는 재출품이 그대로 물려받습니다.

        여기서 올라가면 그 값이 새 ID로 옮겨 가 '역대 최저가'를 비싸게 만듭니다.
        """
        theirs = {"seller:A:t": {"item_id": "m1", "last_alert_price": 9000,
                                 "last_seen_price": 9000}}
        mine = {"seller:A:t": {"item_id": "m1", "last_alert_price": 12000,
                               "last_seen_price": 12000}}
        merged = merge.merge_ordered(theirs, mine, limit=10,
                                     merge_values=merge.merge_price_record)
        self.assertEqual(merged["seller:A:t"]["last_alert_price"], 9000)
        self.assertEqual(merged["seller:A:t"]["item_id"], "m1")

    def test_merge_without_a_value_rule_keeps_the_old_behaviour(self):
        merged = merge.merge_ordered({"a": 1}, {"a": 2}, limit=10)
        self.assertEqual(merged["a"], 2)

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

        for name in ("MAX_SEEN_ITEMS", "MAX_RELIST_FINGERPRINTS", "MAX_SENT_ALERTS",
                     "MAX_PENDING_ALERTS", "MAX_PENDING_RELISTS"):
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

    def test_merge_keeps_the_earlier_disappearance_clock(self):
        """재출품 판정 보류는 '언제부터 안 보였나'를 세는 시계입니다.

        실행이 겹칠 때마다 늦게 시작한 쪽으로 덮어쓰면 시계가 0으로 되돌아가,
        확인이 영영 끝나지 않습니다.
        """
        theirs = {"m-new": {"matched_id": "m-old", "since": 1000, "checks": 5, "fresh": True}}
        mine = {"m-new": {"matched_id": "m-old", "since": 1400, "checks": 2, "fresh": False}}
        merged = merge.merge_pending_relists(theirs, mine, seen={})
        self.assertEqual(merged["m-new"]["since"], 1000)
        self.assertEqual(merged["m-new"]["checks"], 5)
        self.assertTrue(merged["m-new"]["fresh"])

    def test_merge_restarts_the_clock_when_the_fingerprint_owner_changed(self):
        # 지문의 주인이 바뀌었다면 다른 질문을 확인하고 있는 것이므로 합치면 안 됩니다.
        theirs = {"m-new": {"matched_id": "m-old", "since": 1000, "checks": 5}}
        mine = {"m-new": {"matched_id": "m-other", "since": 1400, "checks": 1}}
        merged = merge.merge_pending_relists(theirs, mine, seen={})
        self.assertEqual(merged["m-new"], mine["m-new"])

    def test_merge_drops_pending_relists_already_judged_by_the_other_run(self):
        # 상대 실행이 먼저 판정을 끝내 seen에 들어갔다면 보류 기록은 의미가 없습니다.
        merged = merge.merge_pending_relists(
            {"m-new": {"matched_id": "m-old", "since": 1000}},
            {},
            seen={"m-new": {"last_alert_price": 1}},
        )
        self.assertEqual(merged, {})

    def test_sent_alerts_are_deduplicated_and_capped(self):
        merged = merge.unique_recent(["a", "b", "a", "c"], limit=2)
        self.assertEqual(merged, ["b", "c"])


    def test_dropping_pending_alerts_at_the_cap_is_reported(self):
        # 상한을 넘겨 알림이 버려지는 상황을 여기서만 조용히 넘기면,
        # 보낸 적도 없는 알림이 말없이 사라집니다(check_mercari는 이미 알립니다).
        over = merge.MAX_PENDING_ALERTS + 3
        theirs = [{"alert_id": f"new:t{i}", "caption": str(i)} for i in range(over)]

        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            result = merge.merge_pending(theirs, [], [])

        self.assertEqual(len(result), merge.MAX_PENDING_ALERTS)
        self.assertIn("상한", captured.getvalue())


if __name__ == "__main__":
    unittest.main()
