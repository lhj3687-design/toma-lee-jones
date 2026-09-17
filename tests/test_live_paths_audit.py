"""live_paths_audit.py 가 '자리'와 '울림'을 제대로 세는지, 그리고 **고장을 잡는지**.

이 저장소에서 네 라운드 연속으로 자체 점검이 고장을 그대로 통과시켰습니다.
그래서 여기서는 두 가지를 나눠 봅니다.

  1. 제대로 셀 때 맞는 값이 나오는가          (CountingTests)
  2. 고장을 넣으면 눈금이 **어긋나는가**      (CalibrationCatchesFaultsTests)

2번이 없으면 1번은 '시험이 있다'는 말일 뿐입니다.

합성 판을 쓰는 이유가 하나 더 있습니다. 운영 이력에서는 `recovered` 와 `created-ok`
의 id 집합이 **완전히 같아서**(늘 같은 실행에서 함께 나갔습니다) 둘의 이름표를
뒤바꾸는 고장이 실제 눈금을 그대로 통과합니다. 합성 판에서는 일부러 갈라 둡니다.
"""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import live_paths_audit as audit  # noqa: E402


# ---------------------------------------------------------------------------
# 합성 판 만들기
# ---------------------------------------------------------------------------

def make_blob(seen=None, sent_alerts=(), pending=(), fingerprints=None,
              clocks=None, include_clocks=True, include_pending_relists=True):
    """운영 상태 파일과 **같은 모양**의 바이트열을 만듭니다.

    save_state 가 json.dumps(..., ensure_ascii=False) 를 쓰므로 구분자는 ', ' 와 ': '
    입니다. 이 도구는 그 사이를 문자열로 자르므로 모양이 곧 시험 대상입니다.
    """
    data = {
        "seen": seen if seen is not None else {},
        "pending": list(pending),
        "sent_alerts": list(sent_alerts),
        "relist_fingerprints": fingerprints if fingerprints is not None else {},
        "known_keywords": ["Margiela"],
    }
    if include_clocks:
        table = {"Margiela": 1789300000.0}
        table.update(clocks or {})
        data["keyword_checked_at"] = table
    if include_pending_relists:
        data["pending_relists"] = {}
    return json.dumps(data, ensure_ascii=False).encode()


def seen_of(dicts=0, ints=0, nulls=0):
    """`seen` 의 세 가지 값 모양을 섞어 만듭니다(운영에 전부 있습니다)."""
    table = {}
    for i in range(dicts):
        table[f"d{i:05d}"] = {"last_alert_price": 1000 + i, "last_seen_price": 1000 + i}
    for i in range(ints):
        table[f"i{i:05d}"] = 2000 + i
    for i in range(nulls):
        table[f"n{i:05d}"] = None
    return table


def fingerprints_of(count):
    return {f"title:t{i:05d}:900": {"item_id": f"m{i:09d}",
                                    "last_alert_price": 900, "last_seen_price": 900}
            for i in range(count)}


def clock(search=None, created=None, items=None, cadence=None):
    table = {}
    for key, value in (("__last_search_ok__", search), ("__last_created_ok__", created),
                       ("__last_items_ok__", items), ("__last_cadence_ok__", cadence)):
        if value is not None:
            table[key] = value
    return table


def snapshots(*specs):
    """(시각, 제목, blob) 을 summarize 가 먹는 모양으로 바꿉니다."""
    return [(when, subject, audit.extract(blob)) for when, subject, blob in specs]


BASE = 1789300000.0


# ---------------------------------------------------------------------------
# 1. 제대로 셀 때
# ---------------------------------------------------------------------------

class CountingTests(unittest.TestCase):

    def test_seen_counts_all_three_value_shapes(self):
        """dict / 정수 / null 이 섞여 있습니다. 한 가지만 세면 조용히 빠집니다.

        실측(2026-09-17 HEAD): 21,356 + 1,200 + 125 = 22,681. dict 만 세면 1,325건이
        사라집니다 - 이 도구를 만들면서 실제로 낸 고장입니다.
        """
        blob = make_blob(seen=seen_of(dicts=7, ints=3, nulls=2))
        self.assertEqual(audit.extract(blob)["seen"], 12)

    def test_seen_and_fingerprints_do_not_share_a_formula(self):
        """지문은 항목당 콜론이 4개라 seen 공식을 쓰면 정확히 두 배가 됩니다."""
        blob = make_blob(seen=seen_of(dicts=5, ints=1), fingerprints=fingerprints_of(9))
        values = audit.extract(blob)
        self.assertEqual(values["seen"], 6)
        self.assertEqual(values["fingerprints"], 9)

    def test_an_old_file_without_pending_relists_still_yields_its_clocks(self):
        """끝 경계 키가 없던 옛 판입니다. 여기서 시계를 잃으면 구간이 통째로 빠집니다."""
        blob = make_blob(clocks=clock(search=BASE, items=BASE),
                         include_pending_relists=False)
        values = audit.extract(blob)
        self.assertEqual(values["clocks"]["search_ok"], BASE)
        self.assertEqual(values["notes"], [])

    def test_a_file_without_keyword_checked_at_is_flagged_not_zeroed(self):
        """시계가 아예 없던 더 옛 판. '자리 0'이 아니라 '측정 없음'이어야 합니다."""
        values = audit.extract(make_blob(include_clocks=False))
        self.assertIn("시계없음", values["notes"])
        self.assertEqual(values["clocks"], {})

    def test_an_empty_feed_run_is_counted_as_a_place(self):
        """검색은 성공했는데 매물이 0건이면 __last_items_ok__ 가 전진하지 못합니다."""
        report = audit.summarize(snapshots(
            (BASE, "queue", make_blob(clocks=clock(search=BASE, items=BASE, created=BASE))),
            (BASE + 60, "queue", make_blob(clocks=clock(search=BASE + 60, items=BASE,
                                                        created=BASE + 60))),
        ))
        self.assertEqual(len(report["매물0 자리"]), 1)
        self.assertEqual(len(report["등록시각 자리"]), 0)

    def test_commits_from_one_run_are_not_counted_as_several_runs(self):
        """한 실행이 상태를 여러 번 커밋합니다. 같은 __last_search_ok__ 는 한 실행입니다."""
        report = audit.summarize(snapshots(
            (BASE, "queue", make_blob(clocks=clock(search=BASE))),
            (BASE + 3, "record", make_blob(clocks=clock(search=BASE))),
            (BASE + 7, "record", make_blob(clocks=clock(search=BASE))),
            (BASE + 61, "queue", make_blob(clocks=clock(search=BASE + 61))),
        ))
        self.assertEqual(report["판"], 4)
        self.assertEqual(len(report["실행"]), 2)

    def test_a_recovery_without_its_warning_is_reported(self):
        """PR #22가 막은 그 모양 - 알린 적 없는 고장이 나았다는 말."""
        sent = ["health:recovered:1789192141",
                "health:created-ok:1789240824",
                "health:created-missing:1789240824:0"]
        report = audit.summarize(snapshots(
            (BASE, "queue", make_blob(sent_alerts=sent, clocks=clock(search=BASE))),
        ))
        loose = audit.unpaired_recoveries(report["health"])
        self.assertEqual([a for a, _ in loose["recovered"]], ["health:recovered:1789192141"])
        self.assertEqual(loose["created-ok"], [])

    def test_a_queued_alert_that_never_went_out_is_kept_apart(self):
        """대기열에만 있고 끝내 못 나간 알림을 '나갔다'로 세면 안 됩니다."""
        report = audit.summarize(snapshots(
            (BASE, "queue", make_blob(sent_alerts=["health:feed-ok:1"],
                                      pending=[{"alert_id": "health:empty-feed:2:0",
                                                "caption": "x", "photo": None}],
                                      clocks=clock(search=BASE))),
        ))
        self.assertEqual(sorted(report["health"]), ["health:feed-ok:1"])
        self.assertEqual(report["health 대기"], ["health:empty-feed:2:0"])


# ---------------------------------------------------------------------------
# 2. 고장을 잡는가
# ---------------------------------------------------------------------------

class CalibrationCatchesFaultsTests(unittest.TestCase):
    """합성 눈금 판에 고장을 넣고, 값이 **어긋나는지** 봅니다.

    눈금 판의 숫자는 자릿수·경계·0을 섞고, 무엇보다 **그 고장 아래에서 두 값이 같은
    값으로 뭉개지는 자리**를 일부러 둡니다. 크기만 섞는 것으로는 네 라운드 연속
    안 잡혔습니다(README "자릿수를 섞어 두는 것으로 끝내기").
    """

    # 15,695 와 14,136 은 만 단위로 내림하면 **둘 다 10,000** 이 됩니다.
    # 판 6개와 실행 3개는 실행 묶기를 빠뜨리면 **둘 다 6** 이 됩니다.
    # 15분 이상 2건과 30분 이상 1건은 문턱을 뭉치면 **둘 다 2** 가 됩니다.
    EXPECTED = {
        "판": 6,
        "실행": 3,
        "시계를 읽은 판": 5,
        "시계가 없던 판": 1,
        "seen 최대": 15695,
        "지문 최대": 14136,
        "15분 이상": 2,
        "30분 이상": 1,
        "매물0 자리": 1,
        "recovered": 1,
        "created-ok": 2,
        "짝 없는 recovered": 1,
        "짝 없는 created-ok": 1,
    }

    def board(self):
        """눈금 판. 위 EXPECTED 가 나와야 합니다."""
        big_seen = seen_of(dicts=15000, ints=600, nulls=95)          # 15,695
        big_fp = fingerprints_of(14136)
        sent_a = ["health:recovered:100"]          # 짝(search-down:100)이 없는 복구
        # created-ok:200 은 짝이 있고, created-ok:300 은 없습니다. 두 가족의 건수를
        # 일부러 갈라 둡니다(1건 vs 2건) - 운영 이력에서는 둘이 늘 같은 값입니다.
        sent_b = sent_a + ["health:created-missing:200:0", "health:created-ok:200",
                           "health:created-ok:300"]
        return snapshots(
            # 시계가 아예 없던 옛 판 (자리로 세면 안 됩니다)
            (BASE, "queue", make_blob(include_clocks=False)),
            (BASE + 60, "queue", make_blob(seen=big_seen, fingerprints=big_fp,
                                           clocks=clock(search=BASE + 60, items=BASE + 60,
                                                        created=BASE + 60))),
            (BASE + 63, "record", make_blob(clocks=clock(search=BASE + 60, items=BASE + 60,
                                                         created=BASE + 60))),
            # 20분 비었다가(>=15분, <30분) 검색은 됐는데 매물 0건
            (BASE + 1263, "queue", make_blob(sent_alerts=sent_a,
                                             clocks=clock(search=BASE + 1263, items=BASE + 60,
                                                          created=BASE + 1263))),
            # 35.6분 더 빔 (>=30분) - 두 문턱이 여기서 갈립니다
            (BASE + 3400, "queue", make_blob(sent_alerts=sent_b,
                                             clocks=clock(search=BASE + 3400,
                                                          items=BASE + 3400,
                                                          created=BASE + 3400))),
            (BASE + 3403, "record", make_blob(sent_alerts=sent_b,
                                              clocks=clock(search=BASE + 3400,
                                                           items=BASE + 3400,
                                                           created=BASE + 3400))),
        )

    def measured(self):
        report = audit.summarize(self.board())
        gaps = [g for _, g in report["커밋 간격"]]
        loose = audit.unpaired_recoveries(report["health"])
        grouped = audit.families(report["health"])
        return {
            "판": report["판"],
            "실행": len(report["실행"]),
            "시계를 읽은 판": report["시계를 읽은 판"],
            "시계가 없던 판": report["시계가 없던 판"],
            "seen 최대": report["seen 최대"],
            "지문 최대": report["지문 최대"],
            "15분 이상": sum(1 for g in gaps if g >= audit.WATCHDOG_STUCK_AFTER_SECONDS),
            "30분 이상": sum(1 for g in gaps if g >= audit.WATCHDOG_STALE_AFTER_SECONDS),
            "매물0 자리": len(report["매물0 자리"]),
            "recovered": len(grouped.get("recovered", {})),
            "created-ok": len(grouped.get("created-ok", {})),
            "짝 없는 recovered": len(loose["recovered"]),
            "짝 없는 created-ok": len(loose["created-ok"]),
        }

    def assert_board_passes(self):
        self.assertEqual(self.measured(), self.EXPECTED)

    def assert_board_fails(self, message):
        self.assertNotEqual(self.measured(), self.EXPECTED, message)

    def patch(self, name, replacement):
        original = getattr(audit, name)
        setattr(audit, name, replacement)
        self.addCleanup(setattr, audit, name, original)

    # -- 눈금 자신이 맞는지 먼저 ------------------------------------------

    def test_the_board_itself_is_right(self):
        self.assert_board_passes()

    # -- 실제로 냈던 고장들 -----------------------------------------------

    def test_counting_only_dict_shaped_seen_entries_is_caught(self):
        self.patch("count_seen_entries",
                   lambda chunk: None if chunk is None else chunk.count(b'"last_seen_price"'))
        self.assert_board_fails("seen 을 dict 만 세는 고장을 눈금이 통과시켰습니다")

    def test_using_the_seen_formula_on_fingerprints_is_caught(self):
        self.patch("count_fingerprints", audit.count_seen_entries)
        self.assert_board_fails("지문에 seen 공식을 쓰는 고장을 눈금이 통과시켰습니다")

    def test_dropping_the_missing_boundary_fallback_is_caught(self):
        """끝 경계 키가 없는 옛 판에서 시계를 잃는 그 고장입니다."""
        original = audit.slab

        def no_fallback(blob, key, following):
            import re as _re
            head = _re.search(rb'"%s":\s*' % _re.escape(key.encode()), blob)
            if head is None:
                return None
            for name in following:
                tail = _re.search(rb',\s*"%s":\s*' % _re.escape(name.encode()), blob)
                if tail is not None and tail.start() > head.end():
                    return blob[head.end():tail.start()]
            return blob[head.end():]          # 꼬리 '}' 를 떼지 않습니다

        self.patch("slab", no_fallback)
        board = [(w, s, audit.extract(b)) for w, s, b in
                 [(BASE, "queue", make_blob(clocks=clock(search=BASE),
                                            include_pending_relists=False))]]
        self.assertEqual(audit.summarize(board)["시계를 읽은 판"], 0,
                         "경계 폴백을 뺐는데도 시계가 읽혔습니다 — 시험이 무력합니다")
        audit.slab = original

    # -- 뭉개지는 자리 -----------------------------------------------------

    def test_flooring_the_counts_to_ten_thousands_is_caught(self):
        """15,695 와 14,136 이 만 단위 내림에서 **같은 10,000** 이 됩니다."""
        original = audit.count_seen_entries
        self.patch("count_seen_entries",
                   lambda chunk: None if chunk is None else original(chunk) // 10000 * 10000)
        original_fp = audit.count_fingerprints
        self.patch("count_fingerprints",
                   lambda chunk: None if chunk is None else original_fp(chunk) // 10000 * 10000)
        self.assert_board_fails("두 값이 같은 값으로 뭉개지는 고장을 눈금이 통과시켰습니다")

    def test_losing_the_run_grouping_is_caught(self):
        """실행 묶기를 빠뜨리면 실행 3개가 판 6개와 **같은 값**이 됩니다."""
        report = audit.summarize(self.board())
        self.assertNotEqual(len(report["실행"]), report["판"],
                            "눈금 판에서 실행 수와 판 수가 같아 이 고장을 못 잡습니다")

    def test_folding_the_two_gap_thresholds_together_is_caught(self):
        """15분 이상 2건과 30분 이상 1건이 문턱을 뭉치면 **둘 다 2** 가 됩니다."""
        self.patch("WATCHDOG_STALE_AFTER_SECONDS", audit.WATCHDOG_STUCK_AFTER_SECONDS)
        self.assert_board_fails("두 문턱을 뭉치는 고장을 눈금이 통과시켰습니다")

    def test_swapping_two_alert_families_is_caught(self):
        """운영 이력만으로는 못 잡는 자리입니다 - 거기서는 두 가족의 id 집합이 같습니다.

        그래서 눈금 판에서는 recovered 1건 / created-ok 2건으로 갈라 두었습니다.
        """
        self.patch("RECOVERY_PAIRS", {
            "recovered": "created-missing",     # 짝을 뒤바꿉니다
            "created-ok": "search-down",
            "feed-ok": "empty-feed",
            "cadence-ok": "cadence-slow",
        })
        self.assert_board_fails("복구-경고 짝을 뒤바꾸는 고장을 눈금이 통과시켰습니다")

    def test_treating_an_unreadable_clock_as_zero_is_caught(self):
        """못 잰 것을 0으로 읽으면 '자리'가 없는데 있는 것처럼 보입니다."""
        original = audit.extract

        def zeroed(blob):
            values = original(blob)
            for key in audit.CLOCK_KEYS:
                if values["clocks"].get(key) is None:
                    values["clocks"][key] = 0.0
            if not values["clocks"]:
                values["clocks"] = {k: 0.0 for k in audit.CLOCK_KEYS}
            return values

        self.patch("extract", zeroed)
        self.assert_board_fails("못 잰 시계를 0으로 읽는 고장을 눈금이 통과시켰습니다")


if __name__ == "__main__":
    unittest.main()
