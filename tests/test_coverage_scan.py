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
        fresh, missed = coverage_scan.count_missed(items, {"new-seen"}, fresh_after)
        self.assertEqual((fresh, missed), (2, 1))

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


if __name__ == "__main__":
    unittest.main()
