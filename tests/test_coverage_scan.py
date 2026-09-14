"""scripts/coverage_scan.py의 집계 논리를 검증합니다.

이 도구는 "봇이 못 본 매물이 있다/없다"를 말하는 자리라, 도구가 틀리면 그 결론이
통째로 틀립니다. 메루카리를 실제로 부르는 부분(walk)은 가짜 API로 돌려 확인합니다.
"""
import asyncio
import contextlib
import importlib
import io
import json
import sys
import tempfile
import types
import unittest
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

mercapi_stub = types.ModuleType("mercapi")
mercapi_stub.Mercapi = object
sys.modules.setdefault("mercapi", mercapi_stub)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
coverage_scan = importlib.import_module("coverage_scan")


@dataclass
class FakeMeta:
    num_found: int = 0
    next_page_token: str = ""


@dataclass
class FakeItem:
    id_: str
    created: datetime = field(default_factory=datetime.now)
    name: str = "매물"
    price: int = 1000


class FakeResults:
    def __init__(self, pages, index, num_found):
        self.pages, self.index, self.num_found = pages, index, num_found
        self.items = pages[index]
        self.meta = FakeMeta(num_found=num_found,
                             next_page_token="next" if index + 1 < len(pages) else "")

    async def next_page(self):
        return FakeResults(self.pages, self.index + 1, self.num_found)


class FakeApi:
    """categories를 넘기면 걸러 낸 결과를, 안 넘기면 전부를 돌려주는 가짜 검색."""

    def __init__(self, pages_by_filter, num_found_by_filter):
        self.pages_by_filter = pages_by_filter
        self.num_found_by_filter = num_found_by_filter
        self.calls = []
        self.options = []

    async def search(self, keyword, categories=(), **options):
        key = "filtered" if categories else "all"
        self.calls.append((keyword, tuple(categories)))
        self.options.append(options)
        return FakeResults(self.pages_by_filter[key], 0, self.num_found_by_filter[key])


class CoverageScanTests(unittest.TestCase):
    def test_window_split_matches_what_the_bot_actually_looks_at(self):
        items = [{"id_": str(i)} for i in range(300)]
        inside, outside = coverage_scan.split_by_window(items)
        self.assertEqual(len(inside), coverage_scan.MAX_ITEMS_PER_KEYWORD)
        self.assertEqual(len(outside), 300 - coverage_scan.MAX_ITEMS_PER_KEYWORD)

    def test_listings_newer_than_the_state_snapshot_are_not_misses(self):
        """상태 파일은 항상 조금 과거입니다 — 그 뒤에 올라온 매물은 없는 게 당연합니다.

        브랜치에서 돌리면 상태 파일이 브랜치를 딴 시점에 멈춰 있고, main에서 돌려도
        체크아웃 뒤로 시간이 흐릅니다. 그 시차 안에 올라온 매물을 '못 봄'으로 세면
        스캔을 늦게 돌릴수록 결함이 늘어나는 엉터리 숫자가 됩니다. 2026-09-14 측정에서
        실제로 '창 안 못 본 것 8건'이 그렇게 나왔습니다(상태 파일 06:43, 스캔 07:03).
        """
        now = datetime.now()
        fresh_after = (now - timedelta(hours=24)).timestamp()
        checked_at = (now - timedelta(minutes=20)).timestamp()
        items = [
            {"id_": "봇이 보고 기록함", "created": now - timedelta(hours=2)},
            {"id_": "봇이 볼 수 있었는데 없음", "created": now - timedelta(hours=1)},
            {"id_": "상태 파일 이후 등록", "created": now - timedelta(minutes=5)},
        ]
        fresh, missed, after = coverage_scan.count_missed(
            items, {"봇이 보고 기록함"}, fresh_after, checked_at
        )
        self.assertEqual((fresh, missed, after), (3, 1, 1))

    def test_only_recent_listings_count_as_missed(self):
        """오래된 매물이 상태 파일에 없는 건 정상입니다.

        봇은 기록을 시작한 뒤에 올라온 매물만 알림 대상으로 삼습니다. 그걸 '놓쳤다'로
        세면 봇이 돌기 전부터 있던 매물 전부가 결함으로 잡혀 숫자가 무의미해집니다.
        """
        now = datetime.now()
        fresh_after = (now - timedelta(hours=24)).timestamp()
        items = [
            {"id_": "new-seen", "created": now - timedelta(hours=1)},
            {"id_": "new-missed", "created": now - timedelta(hours=2)},
            {"id_": "old-missed", "created": now - timedelta(days=30)},
        ]
        fresh, missed, after = coverage_scan.count_missed(items, {"new-seen"}, fresh_after)
        self.assertEqual((fresh, missed, after), (2, 1, 0))

    def test_items_only_in_the_unfiltered_list_are_the_ones_the_filter_removed(self):
        """필터는 걸러내기만 합니다 — 통과했다면 순위가 앞당겨질 뿐 사라지지 않습니다."""
        filtered = [{"id_": "a"}, {"id_": "c"}]
        unfiltered = [{"id_": "a"}, {"id_": "b"}, {"id_": "c"}]
        excluded = coverage_scan.excluded_by_filter(filtered, unfiltered)
        self.assertEqual([entry["id_"] for entry in excluded], ["b"])

    def test_walk_follows_pages_and_reports_the_total(self):
        pages = [[FakeItem(f"p1-{i}") for i in range(3)], [FakeItem(f"p2-{i}") for i in range(2)]]
        api = FakeApi({"filtered": pages, "all": pages}, {"filtered": 42, "all": 99})

        collected, found = asyncio.run(coverage_scan.walk(api, "kw", [30], pages=2))

        self.assertEqual(len(collected), 5)          # 두 페이지를 이어 붙입니다
        self.assertEqual(found, 42)                  # 전체 건수는 첫 페이지에서 읽습니다
        self.assertEqual(api.calls, [("kw", (30,))])  # 2페이지는 next_page로 갑니다

    def test_walk_stops_at_the_last_page_even_if_more_pages_were_asked_for(self):
        pages = [[FakeItem("only")]]
        api = FakeApi({"filtered": pages, "all": pages}, {"filtered": 1, "all": 1})
        collected, found = asyncio.run(coverage_scan.walk(api, "kw", [], pages=5))
        self.assertEqual(len(collected), 1)
        self.assertEqual(found, 1)

    def test_walk_keeps_what_it_got_when_a_page_fails(self):
        """조회가 중간에 터져도 그때까지 받은 것은 집계에 씁니다.

        여기서 예외가 새면 키워드 하나의 실패가 스캔 전체를 죽입니다.
        """
        class ExplodingResults(FakeResults):
            async def next_page(self):
                raise RuntimeError("2페이지 조회 실패")

        class ExplodingApi(FakeApi):
            async def search(self, keyword, categories=(), **options):
                return ExplodingResults([[FakeItem("a")], [FakeItem("b")]], 0, 7)

        api = ExplodingApi({"filtered": [], "all": []}, {"filtered": 0, "all": 0})
        collected, found = asyncio.run(coverage_scan.walk(api, "kw", [], pages=3))
        self.assertEqual(len(collected), 1)
        self.assertEqual(found, 7)



class WindowDepthTests(unittest.TestCase):
    """창을 '건수'가 아니라 '시간 깊이'로 옮겨 보는 계산들.

    이 숫자로 "창을 줄여도 되는가"를 답하게 되므로, 계산이 틀리면 결론이 통째로
    틀립니다. 특히 등록순 역전은 '앞 120건 = 가장 최근 120건'이라는 전제가 깨지는
    자리라, 그것부터 잡아내는지 확인합니다.
    """

    def items(self, minutes_ago: list) -> list:
        now = datetime.now().timestamp()
        return [{"id_": str(i), "created": now - m * 60} for i, m in enumerate(minutes_ago)]

    def test_dwell_is_how_long_a_listing_stays_inside_the_window(self):
        """창 밖으로 밀려난 것 중 가장 최근 매물의 나이 = 창 안에 머무는 시간."""
        now = datetime.now().timestamp()
        fields = self.items([0, 10, 20, 30, 40])
        profile = coverage_scan.window_dwell(fields, now, sizes=(1, 3))
        self.assertEqual([size for size, _ in profile], [1, 3])
        self.assertAlmostEqual(profile[0][1], 10, delta=1)
        self.assertAlmostEqual(profile[1][1], 30, delta=1)

    def test_an_old_listing_mixed_into_the_results_does_not_move_the_number(self):
        """등록순 결과에는 예전 매물이 섞여 들어옵니다(실측 598건 중 253건이 역전).

        '순위 N번째 매물의 나이'로 재면 그 자리에 앉은 예전 매물 때문에 값이 통째로
        널뜁니다 — 실제로 '30건=2.1시간치 / 60건=16.7일치 / 120건=15.5시간치'처럼
        창을 넓혔는데 깊이가 얕아지는 표가 나왔습니다. 밖으로 밀려난 쪽의 최솟값은
        섞여 들어온 예전 매물에 흔들리지 않습니다.
        """
        now = datetime.now().timestamp()
        fields = self.items([1, 2, 60 * 24 * 300, 4, 5])  # 세 번째가 300일 전 매물입니다
        (_, minutes), = coverage_scan.window_dwell(fields, now, sizes=(3,))
        self.assertAlmostEqual(minutes, 4, delta=1)

    def test_a_window_bigger_than_the_result_is_not_a_limit(self):
        """매물이 창보다 적으면 창이 제약이 아닙니다 — 0분으로 세면 정반대로 읽힙니다."""
        now = datetime.now().timestamp()
        profile = coverage_scan.window_dwell(self.items([0, 5]), now, sizes=(2, 120))
        self.assertIsNone(profile[0][1])
        self.assertIsNone(profile[1][1])
        self.assertEqual(coverage_scan.format_span(None), "창이 남음")

    def test_the_oldest_item_in_the_window_gates_the_extra_page_fetch(self):
        """봇의 '다음 페이지도 본다'는 페이지 전체가 신규일 때만 발동합니다.

        창 안에 예전 매물이 하나라도 섞여 있으면 발동할 수 없으므로, 그 조건이
        실제로 만족될 수 있는지는 이 값으로만 말할 수 있습니다.
        """
        now = datetime.now().timestamp()
        fields = self.items([1, 2, 60 * 24 * 30, 4])
        self.assertAlmostEqual(coverage_scan.oldest_in_window(fields, now, window=4),
                               60 * 24 * 30, delta=1)
        self.assertAlmostEqual(coverage_scan.oldest_in_window(fields, now, window=2),
                               2, delta=1)

    def test_inversions_catch_a_list_that_is_not_really_newest_first(self):
        fields = self.items([0, 30, 10, 60])  # 세 번째가 두 번째보다 최근입니다
        self.assertEqual(coverage_scan.order_inversions(fields), 1)
        self.assertEqual(coverage_scan.order_inversions(self.items([0, 10, 20])), 0)

    def test_inversions_can_be_counted_on_the_update_time_too(self):
        """'새로운 순'의 기준이 등록 시각이 아니라 수정 시각인지 가리는 계산입니다."""
        now = datetime.now().timestamp()
        fields = [
            {"id_": "a", "created": now - 600, "updated": now - 60},
            {"id_": "b", "created": now - 60, "updated": now - 120},
        ]
        self.assertEqual(coverage_scan.order_inversions(fields), 1)          # 등록 시각으로는 역전
        self.assertEqual(coverage_scan.order_inversions(fields, "updated"), 0)  # 수정 시각으로는 정렬됨

    def test_items_outside_the_window_can_be_newer_than_the_window_tail(self):
        fields = self.items([0, 90, 5, 120])  # 창 2건: 뒤쪽 5분짜리가 창 밖입니다
        self.assertEqual(coverage_scan.newer_beyond_window(fields, window=2), 1)

    def test_peak_counts_the_busiest_stretch_not_the_average(self):
        fields = self.items([0, 0.5, 0.9, 50, 100])  # 1분 안에 3건이 몰렸습니다
        self.assertEqual(coverage_scan.peak_arrivals(fields, 60), 3)

    def test_missed_ages_only_covers_recent_listings_the_bot_does_not_have(self):
        now = datetime.now().timestamp()
        fresh_after = now - 24 * 3600
        fields = self.items([3, 20, 60 * 48])  # 마지막은 이틀 전이라 '최근'이 아닙니다
        fields[1]["id_"] = "seen"
        ages = coverage_scan.missed_ages(fields, {"seen"}, fresh_after, now)
        self.assertEqual(len(ages), 1)
        # 상태 파일 이후에 올라온 것은 나이 목록에서도 빠집니다.
        self.assertEqual(
            coverage_scan.missed_ages(fields, {"seen"}, fresh_after, now, now - 10 * 60), []
        )
        self.assertAlmostEqual(ages[0], 3, delta=1)

    def test_span_is_readable_at_every_scale(self):
        self.assertEqual(coverage_scan.format_span(45), "45분치")
        self.assertEqual(coverage_scan.format_span(60 * 5), "5.0시간치")
        self.assertEqual(coverage_scan.format_span(60 * 24 * 3), "3.0일치")


if __name__ == "__main__":
    unittest.main()


class SortPassTests(unittest.TestCase):
    """등록순과 추천순을 정말 가르고 있는지.

    이 검사가 없으면 도구가 조용히 거짓말을 합니다 — 아래 두 가지가 겹치면
    '등록순 대 추천순' 표가 같은 조회 두 벌이 됩니다.
    """

    def test_walk_sends_the_sort_options_of_the_pass_it_was_asked_for(self):
        """--sort score가 정말 다른 조회를 부르는지. 안 그러면 표가 통째로 거짓입니다."""
        original = dict(coverage_scan.SEARCH_SORT_OPTIONS)
        coverage_scan.SEARCH_SORT_OPTIONS.update(
            {"created": {"sort_by": "CREATED"}, "score": {"sort_by": "SCORE"}}
        )
        try:
            pages = [[FakeItem("a")]]
            api = FakeApi({"filtered": pages, "all": pages}, {"filtered": 1, "all": 1})
            asyncio.run(coverage_scan.walk(api, "kw", [], pages=1, sort="score"))
            asyncio.run(coverage_scan.walk(api, "kw", [], pages=1, sort="created"))
            self.assertEqual(
                [opts.get("sort_by") for opts in api.options], ["SCORE", "CREATED"]
            )
        finally:
            coverage_scan.SEARCH_SORT_OPTIONS.clear()
            coverage_scan.SEARCH_SORT_OPTIONS.update(original)

    def test_empty_search_options_are_refused_instead_of_silently_measured(self):
        """build_search_options()가 물러난 상태를 잡아냅니다.

        mercapi import가 실패하면 그 함수는 예외를 삼키고 `{"created": {}, "score": {}}`로
        물러납니다. 그러면 두 패스가 **옵션 없는 같은 조회**가 되는데, mercapi의 기본
        정렬이 추천순이라 결과는 둘 다 추천순입니다. 그대로 돌리면 "추천순도 등록순과
        똑같더라"는 결론이 나옵니다 — 재고 있는 도구로 이상 없다고 말하는 자리입니다.
        """
        original = dict(coverage_scan.SEARCH_SORT_OPTIONS)
        try:
            coverage_scan.SEARCH_SORT_OPTIONS.update({"created": {}, "score": {}})
            ok, why = coverage_scan.sort_options_are_distinct()
            self.assertFalse(ok)
            self.assertIn("비어", why)

            coverage_scan.SEARCH_SORT_OPTIONS.update(
                {"created": {"sort_by": "SAME"}, "score": {"sort_by": "SAME"}}
            )
            ok, why = coverage_scan.sort_options_are_distinct()
            self.assertFalse(ok)
            self.assertIn("sort_by", why)

            coverage_scan.SEARCH_SORT_OPTIONS.update(
                {"created": {"sort_by": "CREATED"}, "score": {"sort_by": "SCORE"}}
            )
            self.assertEqual(coverage_scan.sort_options_are_distinct(), (True, ""))
        finally:
            coverage_scan.SEARCH_SORT_OPTIONS.clear()
            coverage_scan.SEARCH_SORT_OPTIONS.update(original)


class DwellMethodTests(unittest.TestCase):
    """'창 밖으로 밀려난 것 중 가장 어린 것의 나이'를 써도 되는 자리인지 가립니다.

    그 계산은 **컨베이어 가정** 위에 서 있습니다 — 새 매물은 맨 앞으로 들어와 뒤로만
    밀린다는 가정입니다. 등록순에서는 서지만, 순위를 시각이 만들지 않는 정렬에서는
    설 이유가 없습니다. 가정이 깨진 것을 못 잡으면 도구가 아무 숫자나 내놓습니다.
    """

    def _listing(self, minutes_old):
        return {"created": datetime.now() - timedelta(minutes=minutes_old)}

    def test_fresh_listings_sitting_at_the_front_keep_the_conveyor_assumption(self):
        now = datetime.now().timestamp()
        items = [self._listing(m) for m in range(0, 300)]  # 앞이 어리고 뒤가 오래됨
        ranks = coverage_scan.young_item_ranks(items, now, max_age_minutes=60)
        self.assertEqual(ranks, list(range(0, 61)))
        self.assertTrue(coverage_scan.conveyor_holds(ranks))

    def test_a_fresh_listing_ranked_outside_the_window_breaks_it(self):
        """추천순에서 기대하는 모습입니다 — 갓 올라온 매물이 200등에 꽂힙니다."""
        now = datetime.now().timestamp()
        items = [self._listing(500) for _ in range(300)]
        items[5] = self._listing(3)
        items[200] = self._listing(4)     # 창(120) 밖인데 갓 올라옴
        ranks = coverage_scan.young_item_ranks(items, now, max_age_minutes=60)
        self.assertEqual(ranks, [5, 200])
        self.assertFalse(coverage_scan.conveyor_holds(ranks))

    def test_no_fresh_listing_at_all_is_not_treated_as_a_holding_assumption(self):
        """어린 매물이 하나도 없으면 가정을 확인한 게 아닙니다 — 참으로 치면 안 됩니다."""
        self.assertFalse(coverage_scan.conveyor_holds([]))

    def test_predicted_survival_is_the_conveyor_arithmetic(self):
        """깊이 D인 컨베이어는 lag 동안 lag/D 만큼 갈립니다."""
        self.assertAlmostEqual(coverage_scan.predicted_survival(120, 30), 0.75)
        self.assertAlmostEqual(coverage_scan.predicted_survival(120, 120), 0.0)
        self.assertAlmostEqual(coverage_scan.predicted_survival(120, 500), 0.0)
        self.assertIsNone(coverage_scan.predicted_survival(None, 10))


class SurvivalCurveTests(unittest.TestCase):
    """가정을 쓰지 않는 자 — 같은 창을 다시 찍어 누가 남았는지 직접 셉니다."""

    def _snapshot(self, minute, order):
        return (minute * 60.0, order, [(3, None)])

    def test_survival_counts_how_many_of_the_window_are_still_there(self):
        window = 3
        shots = [
            self._snapshot(0, ["a", "b", "c", "x"]),
            self._snapshot(5, ["d", "a", "b", "c"]),   # c가 창 밖으로
            self._snapshot(10, ["e", "d", "a", "b"]),  # b도 창 밖으로
        ]
        result = coverage_scan.survival_curve(shots, window=window)
        by_step = {step: (round(lag), round(kept, 3)) for step, lag, kept in result["lags"]}
        # 한 칸 뒤: {a,b,c}->{d,a,b} 2/3, {d,a,b}->{e,d,a} 2/3
        self.assertEqual(by_step[1], (5, round(4 / 6, 3)))
        # 두 칸 뒤: {a,b,c} 중 {e,d,a}에 남은 것은 a 하나
        self.assertEqual(by_step[2], (10, round(1 / 3, 3)))

    def test_an_item_that_leaves_the_window_and_comes_back_is_counted(self):
        """컨베이어면 0이어야 하는 값입니다. 0이 아니면 '머무는 시간'이 한 구간이 아닙니다."""
        shots = [
            self._snapshot(0, ["a", "b", "c"]),
            self._snapshot(5, ["x", "y", "c"]),        # a, b가 창 밖
            self._snapshot(10, ["a", "b", "c"]),       # 둘 다 돌아옴
        ]
        result = coverage_scan.survival_curve(shots, window=3)
        self.assertEqual(result["reentry"], 2)

    def test_pushed_out_and_sold_off_are_told_apart(self):
        """창에서 빠진 이유가 '순위에 밀림'인지 '아예 없어짐'인지 갈라야 처방이 갈립니다."""
        shots = [
            self._snapshot(0, ["a", "b", "c", "z"]),
            self._snapshot(5, ["x", "y", "z", "a"]),   # a는 4등으로 밀림, b·c는 통째로 없음
        ]
        result = coverage_scan.survival_curve(shots, window=3)
        self.assertEqual(result["window_size"], 3)
        self.assertEqual(result["displaced"], 1)   # a
        self.assertEqual(result["vanished"], 2)    # b, c

    def test_a_still_window_reports_no_turnover(self):
        shots = [self._snapshot(m, ["a", "b", "c"]) for m in (0, 5, 10)]
        result = coverage_scan.survival_curve(shots, window=3)
        self.assertEqual(result["reentry"], 0)
        self.assertEqual((result["displaced"], result["vanished"]), (0, 0))
        self.assertTrue(all(kept == 1.0 for _, _, kept in result["lags"]))


class EndToEndTests(unittest.TestCase):
    """scan()과 measure_residence()가 실제로 끝까지 도는지.

    이 도구는 손으로만 돌리는 데다 메루카리를 수백 번 부릅니다. 본문이 중간에
    터지면 그 사실을 **워크플로를 한 번 태우고 나서야** 알게 됩니다. 가짜 API로
    본문을 통째로 한 번 돌려 둡니다.
    """

    def setUp(self):
        self.saved = {
            "searches": coverage_scan.SEARCHES,
            "options": dict(coverage_scan.SEARCH_SORT_OPTIONS),
            "pause": coverage_scan.PAGE_PAUSE_SECONDS,
            "seen_file": coverage_scan.SEEN_FILE,
            "mercapi": sys.modules["mercapi"].Mercapi,
        }
        coverage_scan.PAGE_PAUSE_SECONDS = 0
        coverage_scan.SEARCHES = [{"query": "kw", "categories": [30]}]
        coverage_scan.SEARCH_SORT_OPTIONS.update(
            {"created": {"sort_by": "CREATED"}, "score": {"sort_by": "SCORE"}}
        )
        self.state = Path(tempfile.mkdtemp()) / "seen_items.json"
        self.state.write_text(json.dumps({
            "seen": {"보고 있는 매물": {}},
            "keyword_checked_at": {"kw": datetime.now().timestamp()},
        }))
        coverage_scan.SEEN_FILE = self.state

        now = datetime.now()
        pages = [[FakeItem(f"item-{i}", created=now - timedelta(minutes=i))
                  for i in range(150)]]
        sys.modules["mercapi"].Mercapi = lambda: FakeApi(
            {"filtered": pages, "all": pages}, {"filtered": 150, "all": 150}
        )

    def tearDown(self):
        coverage_scan.SEARCHES = self.saved["searches"]
        coverage_scan.SEARCH_SORT_OPTIONS.clear()
        coverage_scan.SEARCH_SORT_OPTIONS.update(self.saved["options"])
        coverage_scan.PAGE_PAUSE_SECONDS = self.saved["pause"]
        coverage_scan.SEEN_FILE = self.saved["seen_file"]
        sys.modules["mercapi"].Mercapi = self.saved["mercapi"]

    def _run(self, coro):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            asyncio.run(coro)
        return buffer.getvalue()

    def test_scan_runs_both_passes_and_reports_each_separately(self):
        output = self._run(
            coverage_scan.scan(pages=1, fresh_hours=24, sorts=("created", "score"),
                               check_filter=False, track={"item-3"})
        )
        self.assertIn("등록순(created) 패스", output)
        self.assertIn("추천순(score) 패스", output)
        self.assertIn("이름을 대고 쫓은 매물", output)
        self.assertIn("item-3", output)
        self.assertIn("한 번의 전체 조회에서 봇이 실제로 보는 것", output)
        self.assertIn("완료", output)

    def test_scan_stops_instead_of_measuring_two_identical_passes(self):
        """정렬을 가를 수 없으면 표를 찍지 않고 멈춥니다."""
        coverage_scan.SEARCH_SORT_OPTIONS.update({"created": {}, "score": {}})
        with self.assertRaises(SystemExit):
            asyncio.run(coverage_scan.scan(pages=1, fresh_hours=24,
                                           sorts=("created", "score")))

    def test_residence_mode_reports_the_longitudinal_numbers(self):
        output = self._run(
            coverage_scan.measure_residence(pages=1, snapshots=2, interval=0,
                                            sorts=("score",), keywords=[])
        )
        self.assertIn("한 장짜리 추정 깊이", output)
        self.assertIn("실측이 말하는 깊이", output)
        self.assertIn("다시 들어온", output)


class SellerVisibilityTests(unittest.TestCase):
    """지목한 판매자의 매물이 창 안에 들어오는가.

    재출품 오판 36건 중 32건이 판매자 한 명(119903670)에게서 나왔습니다. 그 판매자의
    매물이 평소에 창 밖에 앉아 있다면, '이번 조회 결과에 없다'는 사라짐의 증거가
    아니라 그 매물들의 **평소 상태**입니다.
    """

    def setUp(self):
        self.saved_searches = coverage_scan.SEARCHES
        self.saved_pause = coverage_scan.PAGE_PAUSE_SECONDS
        self.saved_seen = coverage_scan.SEEN_FILE
        coverage_scan.PAGE_PAUSE_SECONDS = 0
        coverage_scan.SEARCHES = [{"query": "kw", "categories": []}]
        self.state = Path(tempfile.mkdtemp()) / "seen_items.json"
        self.state.write_text(json.dumps({"seen": {}, "keyword_checked_at": {}}))
        coverage_scan.SEEN_FILE = self.state

    def tearDown(self):
        coverage_scan.SEARCHES = self.saved_searches
        coverage_scan.PAGE_PAUSE_SECONDS = self.saved_pause
        coverage_scan.SEEN_FILE = self.saved_seen

    def test_the_seller_count_splits_at_the_window_edge(self):
        window = coverage_scan.MAX_ITEMS_PER_KEYWORD
        items = [{"id_": f"기타-{i}", "seller_id": "999"} for i in range(window + 10)]
        items[0] = {"id_": "앞자리", "seller_id": "119903670"}
        items[window] = {"id_": "창 밖 첫 자리", "seller_id": "119903670"}
        items[window + 5] = {"id_": "더 뒤", "seller_id": "119903670"}

        class Api(FakeApi):
            async def search(self, keyword, categories=(), **options):
                return FakeResults([items], 0, len(items))

        api = Api({"filtered": [], "all": []}, {"filtered": 0, "all": 0})
        rows, _, _, _ = asyncio.run(
            coverage_scan.census(
                api, "score", 1, 24.0, set(), {},
                check_filter=False, sellers={"119903670"},
            )
        )
        self.assertEqual((rows[0]["seller_in"], rows[0]["seller_out"]), (1, 2))


class AgeMeasurementTests(unittest.TestCase):
    """나이를 재는 기준 시각이 조회보다 앞서면 안 됩니다.

    2026-09-14 실측에서 실제로 났던 일입니다 — 스캔 시작 시각 하나로 모든 패스의
    나이를 쟀는데, 추천순 패스는 그보다 몇 분 뒤에 돌기 때문에 창 깊이가
    '-3분치'로 찍혔습니다. 매물이 조회보다 나중에 올라올 수는 없으니 음수는
    **잰 방법이 틀렸다는 뜻**인데, 작은 음수는 그럴듯해 보여서 그냥 지나갈 수
    있습니다. 그래서 두 군데를 막습니다.
    """

    def test_a_negative_span_is_reported_as_a_broken_measurement(self):
        self.assertEqual(coverage_scan.format_span(-3.0), "⛔잰 방법 틀림")
        self.assertEqual(coverage_scan.format_span(-0.2), "⛔잰 방법 틀림")
        self.assertEqual(coverage_scan.format_span(None), "창이 남음")
        self.assertEqual(coverage_scan.format_span(30.0), "30분치")

    def test_each_keyword_is_aged_against_its_own_scan_time(self):
        """뒤에 도는 패스도 나이가 음수로 나오면 안 됩니다."""
        saved = (coverage_scan.SEARCHES, coverage_scan.PAGE_PAUSE_SECONDS,
                 coverage_scan.SEEN_FILE)
        coverage_scan.PAGE_PAUSE_SECONDS = 0
        coverage_scan.SEARCHES = [{"query": "kw", "categories": []}]
        state = Path(tempfile.mkdtemp()) / "seen_items.json"
        state.write_text(json.dumps({"seen": {}, "keyword_checked_at": {}}))
        coverage_scan.SEEN_FILE = state
        try:
            window = coverage_scan.MAX_ITEMS_PER_KEYWORD
            # 조회 직전에 올라온 매물이 창 밖 첫 자리에 있는 경우입니다.
            items = [FakeItem(f"오래된-{i}", created=datetime.now() - timedelta(days=9))
                     for i in range(window + 5)]
            items[window] = FakeItem("갓 올라옴", created=datetime.now())

            class Api(FakeApi):
                async def search(self, keyword, categories=(), **options):
                    return FakeResults([items], 0, len(items))

            api = Api({"filtered": [], "all": []}, {"filtered": 0, "all": 0})
            rows, _, _, _ = asyncio.run(
                coverage_scan.census(api, "score", 1, 24.0, set(), {}, check_filter=False)
            )
            depths = [minutes for _, minutes in rows[0]["dwell"] if minutes is not None]
            self.assertTrue(depths)
            self.assertTrue(all(depth >= 0 for depth in depths), depths)
        finally:
            (coverage_scan.SEARCHES, coverage_scan.PAGE_PAUSE_SECONDS,
             coverage_scan.SEEN_FILE) = saved


class OnSaleCheckTests(unittest.TestCase):
    """'훑은 범위에 없음'이 '팔렸다'인지 '순위가 뒤'인지 갈라 줍니다.

    갈리지 않으면 재출품 오판을 설명할 수 없습니다 — 팔린 것이면 '사라졌다'가
    맞는 판정이었다는 뜻이니까요.
    """

    def _api(self, results):
        class Api:
            async def item(self, item_id):
                value = results[item_id]
                if isinstance(value, Exception):
                    raise value
                return value
        return Api()

    def test_statuses_are_translated_and_failures_do_not_kill_the_scan(self):
        saved = coverage_scan.PAGE_PAUSE_SECONDS
        coverage_scan.PAGE_PAUSE_SECONDS = 0
        try:
            api = self._api({
                "m1": types.SimpleNamespace(status="ITEM_STATUS_ON_SALE"),
                "m2": types.SimpleNamespace(status="ITEM_STATUS_SOLD_OUT"),
                "m3": None,
                "m4": RuntimeError("boom"),
            })
            states = asyncio.run(coverage_scan.still_on_sale(api, {"m1", "m2", "m3", "m4"}))
            self.assertEqual(states["m1"], "판매중")
            self.assertEqual(states["m2"], "판매완료")
            self.assertEqual(states["m3"], "없음(삭제)")
            self.assertTrue(states["m4"].startswith("확인 실패"))
        finally:
            coverage_scan.PAGE_PAUSE_SECONDS = saved

    def test_shop_products_go_to_the_product_endpoint_not_the_item_one(self):
        """숍스 ID로 item()을 부르면 mercapi가 KeyError로 터집니다.

        2026-09-14 실측에서 쫓던 12건이 **전부** '확인 실패(KeyError)'였습니다.
        전부 숍스 상품(m+숫자가 아닌 ID)이었기 때문입니다. 엔드포인트를 가르지 않으면
        이 확인은 한 건도 답을 내지 못합니다.
        """
        saved = coverage_scan.PAGE_PAUSE_SECONDS
        coverage_scan.PAGE_PAUSE_SECONDS = 0

        class Api:
            def __init__(self):
                self.item_calls, self.product_calls = [], []

            async def item(self, item_id):
                self.item_calls.append(item_id)
                raise KeyError("data")     # 숍스 응답에는 "data" 키가 없습니다

            async def product(self, product_id):
                self.product_calls.append(product_id)
                return types.SimpleNamespace(name="숍스 상품")

        try:
            api = Api()
            states = asyncio.run(coverage_scan.still_on_sale(
                api, {"m12345", "2JSVsYv7PXtWWgyKzRoqP7"}))
            self.assertEqual(api.product_calls, ["2JSVsYv7PXtWWgyKzRoqP7"])
            self.assertEqual(api.item_calls, ["m12345"])
            self.assertEqual(states["2JSVsYv7PXtWWgyKzRoqP7"], "상품 페이지 있음")
        finally:
            coverage_scan.PAGE_PAUSE_SECONDS = saved

    def test_all_checks_failing_is_not_reported_as_nothing_on_sale(self):
        """못 잰 것을 '다 팔렸다'로 읽게 두면 안 됩니다."""
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            coverage_scan.report_tracked(
                {"가", "나"}, {"created": {}}, set(), ("created",),
                {"가": "확인 실패(KeyError)", "나": "확인 실패(KeyError)"},
            )
        output = buffer.getvalue()
        self.assertIn("말할 수 없습니다", output)
        self.assertNotIn("안 나오는 것", output)


class DwellGrowthTests(unittest.TestCase):
    """창을 넓혔는데 깊이가 그대로면 그 값은 깊이가 아닙니다.

    PR #30에서는 **비단조인 표**가 방법이 틀렸다는 신호였습니다(창을 넓혔는데
    얕아짐). 추천순에서는 **평평한 표**가 같은 신호입니다 — 2026-09-14 실측에서
    거의 모든 키워드가 30건=60건=120건으로 나왔습니다.
    """

    def test_a_conveyor_gets_deeper_as_the_window_widens(self):
        self.assertTrue(coverage_scan.dwell_grows_with_the_window(
            [(30, 37.0), (60, 68.0), (120, 120.0)]))

    def test_a_flat_table_is_flagged(self):
        self.assertFalse(coverage_scan.dwell_grows_with_the_window(
            [(30, 7.0), (60, 7.0), (120, 7.0)]))

    def test_a_table_that_gets_shallower_is_flagged(self):
        """PR #30이 실제로 마주쳤던 모양입니다 (Hermes 30건=2.1시간 / 60건=16.7일 / 120건=15.5시간)."""
        self.assertFalse(coverage_scan.dwell_grows_with_the_window(
            [(30, 126.0), (60, 24048.0), (120, 930.0)]))

    def test_a_window_with_room_left_is_not_judged(self):
        self.assertIsNone(coverage_scan.dwell_grows_with_the_window(
            [(30, 500.0), (60, None), (120, None)]))
