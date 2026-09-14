"""scripts/coverage_scan.py의 집계 논리를 검증합니다.

이 도구는 "봇이 못 본 매물이 있다/없다"를 말하는 자리라, 도구가 틀리면 그 결론이
통째로 틀립니다. 메루카리를 실제로 부르는 부분(walk)은 가짜 API로 돌려 확인합니다.
"""
import asyncio
import importlib
import sys
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

    async def search(self, keyword, categories=(), **options):
        key = "filtered" if categories else "all"
        self.calls.append((keyword, tuple(categories)))
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
