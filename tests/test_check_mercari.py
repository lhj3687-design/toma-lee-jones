import asyncio
import importlib
import json
import sys
import tempfile
import types
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

mercapi_stub = types.ModuleType("mercapi")
mercapi_stub.Mercapi = object
sys.modules.setdefault("mercapi", mercapi_stub)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
mercari = importlib.import_module("check_mercari")


@dataclass
class FakeItem:
    id_: str
    name: str
    price: int | Decimal
    thumbnails: list[str] | None = None
    item_type: str = "ITEM"
    seller_id: str | None = None
    created: datetime | None = None
    is_no_price: bool = False


class FakeResults:
    def __init__(self, items):
        self.items = items


class FakeMercapi:
    """검색 호출을 흉내 냅니다.

    실제 코드는 키워드마다 '등록순'과 '추천순'으로 두 번 조회한 뒤 결과를 합치므로,
    호출 횟수를 세어 두 번 불렸는지도 검증할 수 있게 합니다.
    """

    def __init__(self, items_by_keyword, fail_keywords=()):
        self.items_by_keyword = items_by_keyword
        self.fail_keywords = set(fail_keywords)
        self.calls = []

    async def search(self, keyword, categories=(), **options):
        self.calls.append((keyword, options))
        if keyword in self.fail_keywords:
            raise RuntimeError("검색 실패 시뮬레이션")
        return FakeResults(self.items_by_keyword[keyword])


class MercariStateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.original_seen_file = mercari.SEEN_FILE
        self.original_searches = mercari.SEARCHES
        mercari.SEEN_FILE = Path(self.temp_directory.name) / "seen_items.json"
        # 검색 사이 대기(1초)까지 실제로 기다리면 테스트가 불필요하게 느려집니다.
        sleep_patcher = patch.object(mercari.asyncio, "sleep", new=AsyncMock())
        sleep_patcher.start()
        self.addCleanup(sleep_patcher.stop)

    def tearDown(self):
        mercari.SEEN_FILE = self.original_seen_file
        mercari.SEARCHES = self.original_searches
        self.temp_directory.cleanup()

    async def test_check_keyword_routes_shops_url_by_id_pattern_even_without_item_type(self):
        # item_type이 "SHOP"을 담고 있지 않아도, ID가 일반 매물 형식(m+숫자)이 아니면
        # 숍스 상품으로 판단해 /shops/product/ 링크를 만들어야 합니다.
        seen = {}
        new_items = []
        fake_api = FakeMercapi(
            {
                "test": [
                    FakeItem("2JUHeREMxTVqFa42uQFwEc", "Shop item", 5000, item_type="ITEM"),
                    FakeItem("m90925725213", "Regular item", 3000, item_type="ITEM"),
                ]
            }
        )

        await mercari.check_keyword(fake_api, "test", [], seen, {}, new_items)

        self.assertIn("https://jp.mercari.com/shops/product/2JUHeREMxTVqFa42uQFwEc", new_items[0]["caption"])
        self.assertIn("https://jp.mercari.com/item/m90925725213", new_items[1]["caption"])

    async def test_check_keyword_never_raises_alert_baseline_scenario_from_advisor(self):
        # 조언받은 시나리오 그대로 검증:
        # 50000->48000(알림) ->55000(상승, 알림기준 불변) ->54000(알림기준 48000보다 안 쌈, 알림 없음)
        # ->46000(48000보다 1000엔 이상 싸짐, 알림)
        seen = {}
        relist_fingerprints = {}
        new_items: list = []

        async def observe(price):
            new_items.clear()
            api = FakeMercapi({"test": [FakeItem("item", "Margiela bag", price)]})
            await mercari.check_keyword(api, "test", [], seen, relist_fingerprints, new_items)

        await observe(50000)  # 신규 매물 -> '신규' 알림은 발생 (가격인하 알림 대상 아님)
        self.assertEqual([e["alert_id"] for e in new_items], ["new:item"])
        self.assertEqual(seen["item"], {"last_alert_price": 50000, "last_seen_price": 50000})

        await observe(48000)  # 2000엔 하락 -> 알림
        self.assertEqual([e["alert_id"] for e in new_items], ["drop:item:50000:48000"])
        self.assertEqual(seen["item"], {"last_alert_price": 48000, "last_seen_price": 48000})

        await observe(55000)  # 가격 상승 -> 알림 없음, last_alert_price는 그대로
        self.assertEqual(new_items, [])
        self.assertEqual(seen["item"], {"last_alert_price": 48000, "last_seen_price": 55000})

        await observe(54000)  # 역대 최저가(48000)보다 안 쌈 -> 알림 없음
        self.assertEqual(new_items, [])
        self.assertEqual(seen["item"], {"last_alert_price": 48000, "last_seen_price": 54000})

        await observe(46000)  # 48000보다 2000엔 싸짐 -> 알림, 새 최저가로 갱신
        self.assertEqual([e["alert_id"] for e in new_items], ["drop:item:48000:46000"])
        self.assertEqual(seen["item"], {"last_alert_price": 46000, "last_seen_price": 46000})

    async def test_check_keyword_assigns_unique_keys_for_new_and_price_drop(self):
        seen = {"old-item": 12000}
        new_items = []
        fake_api = FakeMercapi(
            {
                "test": [
                    FakeItem("old-item", "Price drop", Decimal("9000")),
                    FakeItem("new-item", "New listing", 15000),
                ]
            }
        )
        await mercari.check_keyword(fake_api, "test", [], seen, {}, new_items)
        self.assertEqual(
            seen,
            {
                "old-item": {"last_alert_price": 9000, "last_seen_price": 9000},
                "new-item": {"last_alert_price": 15000, "last_seen_price": 15000},
            },
        )
        self.assertEqual(
            [entry["alert_id"] for entry in new_items],
            ["drop:old-item:12000:9000", "new:new-item"],
        )

    async def test_relist_with_same_seller_id_does_not_trigger_new_alert(self):
        # 같은 판매자가 삭제 후 새 ID로 재등록한 경우: '신규' 알림 없이
        # 예전 최저가 기록을 새 ID로 이어받아야 합니다.
        seen = {}
        relist_fingerprints = {}
        new_items: list = []

        api1 = FakeMercapi(
            {"test": [FakeItem("m1000", "Margiela 초레어 카트소", 20000, seller_id="seller-A")]}
        )
        await mercari.check_keyword(api1, "test", [], seen, relist_fingerprints, new_items)
        self.assertEqual([e["alert_id"] for e in new_items], ["new:m1000"])

        # 판매자가 삭제 후 재등록: ID는 바뀌었지만 판매자+제목(공백/기호 차이만 있음)은 동일
        new_items.clear()
        api2 = FakeMercapi(
            {"test": [FakeItem("m2000", "  Margiela  초레어  카트소 ", 20000, seller_id="seller-A")]}
        )
        await mercari.check_keyword(api2, "test", [], seen, relist_fingerprints, new_items)

        self.assertEqual(new_items, [])  # 재출품이므로 '신규' 알림 없음
        # 예전 ID는 굳이 지우지 않습니다. 지워 버리면 그 매물이 검색에 다시 잡혔을 때
        # 처음 보는 매물로 오인돼 알림이 가기 때문에, 용량 상한에 밀려 자연스럽게 사라지게 둡니다.
        self.assertIn("m1000", seen)
        self.assertEqual(seen["m2000"], {"last_alert_price": 20000, "last_seen_price": 20000})

        # 재등록하면서 1000엔 이상 더 싸게 올렸다면 가격인하 알림은 정상적으로 와야 함
        new_items.clear()
        api3 = FakeMercapi(
            {"test": [FakeItem("m3000", "Margiela 초레어 카트소", 18000, seller_id="seller-A")]}
        )
        await mercari.check_keyword(api3, "test", [], seen, relist_fingerprints, new_items)
        self.assertEqual([e["alert_id"] for e in new_items], ["drop:m3000:20000:18000"])

    async def test_relist_fallback_to_title_and_exact_price_without_seller_id(self):
        # seller_id를 못 가져오는 경우, 제목+가격이 완전히 같을 때만 재출품으로 판단합니다.
        seen = {}
        relist_fingerprints = {}
        new_items: list = []

        api1 = FakeMercapi({"test": [FakeItem("m1", "Chrome Hearts Tシャツ", 32000)]})
        await mercari.check_keyword(api1, "test", [], seen, relist_fingerprints, new_items)
        self.assertEqual([e["alert_id"] for e in new_items], ["new:m1"])

        # 같은 제목, 같은 가격, 다른 ID -> 재출품으로 처리 (알림 없음)
        new_items.clear()
        api2 = FakeMercapi({"test": [FakeItem("m2", "Chrome Hearts Tシャツ", 32000)]})
        await mercari.check_keyword(api2, "test", [], seen, relist_fingerprints, new_items)
        self.assertEqual(new_items, [])
        self.assertIn("m2", seen)

        # 같은 제목이라도 가격이 다르면(단서 부족) 구분 못 하고 신규로 처리 -> 알려진 한계
        new_items.clear()
        api3 = FakeMercapi({"test": [FakeItem("m3", "Chrome Hearts Tシャツ", 29000)]})
        await mercari.check_keyword(api3, "test", [], seen, relist_fingerprints, new_items)
        self.assertEqual([e["alert_id"] for e in new_items], ["new:m3"])

    def test_load_state_seeds_legacy_keywords_when_field_missing(self):
        # known_keywords 필드가 아직 없는 예전 상태 파일(이번 업그레이드 전)을 흉내냄.
        # 예전부터 쓰던 키워드들은 이미 알려진 것으로 간주되어야, 이번 배포로
        # 오래된 키워드까지 신규로 오인해 알림을 생략해버리는 일이 없습니다.
        mercari.SEEN_FILE.write_text(json.dumps({"seen": {"a": 1}, "pending": [], "sent_alerts": []}))
        _, _, _, _, known_keywords, _ = mercari.load_state()
        self.assertIn("Martin Margiela", known_keywords)
        self.assertIn("Chrome Hearts", known_keywords)
        # 이번에 새로 추가한 키워드는 레거시 시드 목록에 없어야 함(=첫 조회 시 알림 억제 대상)
        self.assertNotIn("Gunter Wermekes", known_keywords)

    def test_save_state_removes_duplicate_and_already_sent_alerts(self):
        pending = [
            {"alert_id": "new:a", "caption": "a"},
            {"alert_id": "new:a", "caption": "a duplicate"},
            {"alert_id": "new:b", "caption": "b"},
            {"caption": "legacy"},
        ]
        mercari.save_state({"a": 1}, pending, ["new:b", "new:already"], {}, set())
        state = json.loads(mercari.SEEN_FILE.read_text())
        self.assertEqual([entry["alert_id"] for entry in state["pending"]], ["new:a", "legacy:legacy"])
        self.assertEqual(state["sent_alerts"], ["new:b", "new:already"])

    async def test_collect_suppresses_new_alerts_only_for_newly_added_keyword(self):
        # "old-keyword"는 이미 알려진 키워드, "new-keyword"는 이번에 새로 추가된 키워드라고 가정.
        # 새 키워드의 기존 매물들은 '신규' 알림 없이 기준선만 저장되고,
        # 기존 키워드는 평소대로 정상적으로 알림이 와야 합니다.
        mercari.SEARCHES = [
            {"query": "old-keyword", "categories": []},
            {"query": "new-keyword", "categories": []},
        ]
        # old-keyword는 이미 조회 기준선이 잡혀 있는 상태(평소 운영 중),
        # new-keyword는 이번에 처음 조회하는 상태입니다.
        mercari.save_state(
            {"unrelated-item": {"last_alert_price": 1, "last_seen_price": 1}},
            [],
            [],
            {},
            {"old-keyword"},
            {"old-keyword": datetime.now().timestamp() - 300},
        )
        fake_api = FakeMercapi(
            {
                "old-keyword": [FakeItem("o1", "Old keyword item", 10000)],
                "new-keyword": [
                    FakeItem("n1", "New keyword item A", 5000),
                    FakeItem("n2", "New keyword item B", 7000),
                ],
            }
        )

        with patch.object(mercari, "Mercapi", return_value=fake_api), patch.object(
            mercari.asyncio, "sleep", new=AsyncMock()
        ):
            await mercari.collect_updates()

        collected = json.loads(mercari.SEEN_FILE.read_text())
        # old-keyword 매물은 정상적으로 '신규' 알림 대기열에 들어감
        self.assertEqual([entry["alert_id"] for entry in collected["pending"]], ["new:o1"])
        # new-keyword 매물들은 알림 없이 기준선(seen)에만 저장됨
        self.assertIn("n1", collected["seen"])
        self.assertIn("n2", collected["seen"])
        # 이제 new-keyword도 known_keywords에 등록되어, 다음 조회부터는 정상적으로 알림이 옴
        self.assertIn("new-keyword", collected["known_keywords"])

    async def test_collect_persists_before_send_and_delivery_is_not_repeated(self):
        mercari.SEARCHES = [{"query": "test", "categories": []}]
        # "test" 키워드는 이미 알려진 것으로 표시해서, 이 테스트가 검증하려는
        # 기존 collect->send 흐름이 새 키워드 첫 조회 억제 로직의 영향을 받지 않게 합니다.
        mercari.save_state(
            {"old-item": 12000}, [], [], {}, {"test"}, {"test": datetime.now().timestamp() - 300}
        )
        fake_api = FakeMercapi(
            {
                "test": [
                    FakeItem("old-item", "Price drop", 9000),
                    FakeItem("new-item", "New listing", 15000),
                ]
            }
        )
        with patch.object(mercari, "Mercapi", return_value=fake_api), patch.object(
            mercari.asyncio, "sleep", new=AsyncMock()
        ):
            await mercari.collect_updates()

        collected = json.loads(mercari.SEEN_FILE.read_text())
        self.assertEqual(
            [entry["alert_id"] for entry in collected["pending"]],
            ["drop:old-item:12000:9000", "new:new-item"],
        )
        self.assertEqual(
            collected["seen"],
            {
                "old-item": {"last_alert_price": 9000, "last_seen_price": 9000},
                "new-item": {"last_alert_price": 15000, "last_seen_price": 15000},
            },
        )

        with patch.object(mercari, "send_telegram", new=AsyncMock(return_value=(True, None))), patch.object(
            mercari, "push_state", return_value=True
        ), patch.object(mercari.asyncio, "sleep", new=AsyncMock()):
            await mercari.send_pending()

        delivered = json.loads(mercari.SEEN_FILE.read_text())
        self.assertEqual(delivered["pending"], [])
        self.assertEqual(
            delivered["sent_alerts"],
            ["drop:old-item:12000:9000", "new:new-item"],
        )

        send_mock = AsyncMock(return_value=(True, None))
        with patch.object(mercari, "send_telegram", new=send_mock), patch.object(
            mercari, "push_state", return_value=True
        ):
            await mercari.send_pending()
        send_mock.assert_not_awaited()

    async def test_flush_pending_skips_entries_recorded_as_sent(self):
        pending = [
            {"alert_id": "new:already", "caption": "already"},
            {"alert_id": "new:send", "caption": "send"},
        ]
        send_mock = AsyncMock(return_value=(True, None))
        with patch.object(mercari, "send_telegram", new=send_mock), patch.object(
            mercari, "push_state", return_value=True
        ), patch.object(mercari.asyncio, "sleep", new=AsyncMock()):
            remaining, sent_alerts = await mercari.flush_pending({}, pending, ["new:already"], {}, set())
        self.assertEqual(remaining, [])
        send_mock.assert_awaited_once_with("send", None)
        self.assertEqual(sent_alerts, ["new:already", "new:send"])

    async def test_flush_pending_persists_after_each_send_and_stops_on_push_failure(self):
        # 알림을 하나 보낼 때마다 곧바로 push_state를 호출해야 하고,
        # 그 저장이 실패하면 이후 남은 항목은 이번 실행에서 보내지 않아야 합니다
        # (배치 전체가 아니라 '방금 보낸 1건'만 위험 구간에 남기기 위함).
        pending = [
            {"alert_id": "new:a", "caption": "a"},
            {"alert_id": "new:b", "caption": "b"},
        ]
        send_mock = AsyncMock(return_value=(True, None))
        push_mock = Mock(side_effect=[False])  # 첫 전송 직후 저장이 실패하는 상황

        with patch.object(mercari, "send_telegram", new=send_mock), patch.object(
            mercari, "push_state", new=push_mock
        ), patch.object(mercari.asyncio, "sleep", new=AsyncMock()):
            remaining, sent_alerts = await mercari.flush_pending({}, pending, [], {}, set())

        # 텔레그램 전송 자체는 첫 건만 시도되고 멈춤
        send_mock.assert_awaited_once_with("a", None)
        push_mock.assert_called_once_with("record Mercari alert delivery")
        # 이미 보낸 건 sent_alerts에는 남아 있어야, 이후 save_state 때 결국 반영됨
        self.assertEqual(sent_alerts, ["new:a"])
        # 두 번째 항목은 아직 안 보냈으므로 대기열에 그대로 남아 있어야 함
        self.assertEqual([entry["alert_id"] for entry in remaining], ["new:b"])


    async def test_seen_keeps_most_recently_observed_items_at_the_end(self):
        # 실제로 발생했던 버그의 회귀 테스트:
        # 다시 본 매물이 dict 앞자리에 그대로 남으면, 용량 상한으로 잘라낼 때
        # "매번 검색에 걸리는 오래된 인기 매물"이 가장 먼저 잘려 나가고
        # 다음 조회에서 처음 보는 매물로 오인돼 알림이 갑니다.
        seen = {}
        relist_fingerprints = {}
        new_items: list = []

        api = FakeMercapi(
            {
                "test": [
                    FakeItem("old", "오래 올라와 있는 매물", 10000, seller_id="A"),
                    FakeItem("fresh", "방금 올라온 매물", 20000, seller_id="B"),
                ]
            }
        )
        await mercari.check_keyword(api, "test", [], seen, relist_fingerprints, new_items)
        self.assertEqual(list(seen), ["old", "fresh"])

        # "old"만 다시 관찰되면 목록 맨 뒤로 이동해야 합니다.
        new_items.clear()
        api2 = FakeMercapi({"test": [FakeItem("old", "오래 올라와 있는 매물", 10000, seller_id="A")]})
        await mercari.check_keyword(api2, "test", [], seen, relist_fingerprints, new_items)
        self.assertEqual(list(seen), ["fresh", "old"])

    async def test_old_listing_is_not_alerted_as_new_after_falling_out_of_state(self):
        # seen 용량 상한 때문에 기록이 밀려난 오래된 매물이 다시 검색에 잡혀도
        # 등록 시각이 기준선보다 예전이면 '신규' 알림을 보내지 않아야 합니다.
        now = datetime.now()
        old_item = FakeItem("m-old", "3개월 전 매물", 10000, created=now - timedelta(days=90))
        api = FakeMercapi({"test": [old_item]})
        new_items: list = []

        await mercari.check_keyword(
            api, "test", [], {}, {}, new_items, created_cutoff=(now - timedelta(minutes=5)).timestamp()
        )
        self.assertEqual(new_items, [])  # 알림 없음

    async def test_recent_listing_is_alerted_as_new(self):
        now = datetime.now()
        fresh_item = FakeItem("m-fresh", "방금 올라온 매물", 10000, created=now - timedelta(minutes=1))
        api = FakeMercapi({"test": [fresh_item]})
        new_items: list = []

        await mercari.check_keyword(
            api, "test", [], {}, {}, new_items, created_cutoff=(now - timedelta(minutes=5)).timestamp()
        )
        self.assertEqual([e["alert_id"] for e in new_items], ["new:m-fresh"])

    async def test_same_item_id_in_fingerprints_restores_price_history(self):
        # 같은 ID의 지문이 남아 있다면 예전에 이미 확인했던 매물입니다.
        # (seen에서만 밀려난 상황) '신규' 알림 대신 예전 최저가 기록을 되살려야 합니다.
        relist_fingerprints = {
            "seller:A:마르지엘라가방": {
                "item_id": "m1",
                "last_alert_price": 20000,
                "last_seen_price": 20000,
            }
        }
        new_items: list = []
        seen: dict = {}
        api = FakeMercapi({"test": [FakeItem("m1", "마르지엘라 가방", 18000, seller_id="A")]})

        await mercari.check_keyword(api, "test", [], seen, relist_fingerprints, new_items)

        # 신규가 아니라 예전 기준가(20000) 대비 가격 인하로 처리되어야 합니다.
        self.assertEqual([e["alert_id"] for e in new_items], ["drop:m1:20000:18000"])

    async def test_shops_seller_id_zero_is_treated_as_unknown_seller(self):
        # 메루카리 숍스 상품은 sellerId가 0으로 내려옵니다. 이걸 진짜 판매자로 쓰면
        # 제목만 같으면 서로 다른 상품이 같은 매물로 묶여 진짜 신규 알림이 삼켜집니다.
        self.assertIsNone(mercari.extract_seller_id(FakeItem("a", "x", 1, seller_id="0")))
        self.assertEqual(mercari.extract_seller_id(FakeItem("a", "x", 1, seller_id="123")), "123")

        seen: dict = {}
        relist_fingerprints: dict = {}
        new_items: list = []
        api = FakeMercapi({"test": [FakeItem("shop-1", "같은 제목 상품", 5000, seller_id="0")]})
        await mercari.check_keyword(api, "test", [], seen, relist_fingerprints, new_items)
        self.assertEqual([e["alert_id"] for e in new_items], ["new:shop-1"])

        # 제목은 같지만 가격이 다른 별개의 숍스 상품 -> 정상적으로 신규 알림이 와야 합니다.
        new_items.clear()
        api2 = FakeMercapi({"test": [FakeItem("shop-2", "같은 제목 상품", 7000, seller_id="0")]})
        await mercari.check_keyword(api2, "test", [], seen, relist_fingerprints, new_items)
        self.assertEqual([e["alert_id"] for e in new_items], ["new:shop-2"])

    async def test_no_price_items_do_not_set_a_bogus_price_baseline(self):
        # 가격 비공개 매물은 price에 9999999가 들어옵니다. 그대로 기준가로 삼으면
        # 다음 조회에서 말도 안 되는 '가격 인하' 알림이 갑니다.
        seen: dict = {}
        new_items: list = []
        api = FakeMercapi({"test": [FakeItem("m1", "가격 비공개", 9999999, is_no_price=True)]})
        await mercari.check_keyword(api, "test", [], seen, {}, new_items)
        self.assertEqual(seen["m1"], {"last_alert_price": None, "last_seen_price": None})

    async def test_search_failure_does_not_advance_keyword_checkpoint(self):
        # 검색이 실패했는데 조회 시각을 갱신해 버리면, 그 사이 올라온 매물이
        # 다음 실행에서 '오래된 매물'로 분류돼 영영 알림이 오지 않습니다.
        mercari.SEARCHES = [{"query": "broken", "categories": []}]
        mercari.save_state({"x": 1}, [], [], {}, {"broken"}, {})
        api = FakeMercapi({"broken": []}, fail_keywords=["broken"])

        with patch.object(mercari, "Mercapi", return_value=api), patch.object(
            mercari.asyncio, "sleep", new=AsyncMock()
        ):
            await mercari.collect_updates()

        state = json.loads(mercari.SEEN_FILE.read_text())
        self.assertEqual(state["keyword_checked_at"], {})

    async def test_collect_records_checkpoint_per_keyword_on_success(self):
        mercari.SEARCHES = [{"query": "test", "categories": []}]
        mercari.save_state({"x": 1}, [], [], {}, {"test"}, {"test": datetime.now().timestamp() - 300})
        api = FakeMercapi({"test": [FakeItem("m1", "item", 1000)]})

        with patch.object(mercari, "Mercapi", return_value=api), patch.object(
            mercari.asyncio, "sleep", new=AsyncMock()
        ):
            await mercari.collect_updates()

        state = json.loads(mercari.SEEN_FILE.read_text())
        self.assertIn("test", state["keyword_checked_at"])
        # 키워드마다 등록순/추천순 두 번 조회합니다.
        self.assertEqual([keyword for keyword, _ in api.calls], ["test", "test"])

    async def test_first_run_without_a_checkpoint_only_records_a_baseline(self):
        # 이 기능을 배포한 직후의 첫 실행입니다. 검색 정렬 방식이 바뀌면서 그동안
        # 눈에 띄지 않던 매물들이 한꺼번에 보이므로, 기존 키워드라도 첫 실행에서는
        # 기준선만 저장하고 신규 알림은 보내지 않아야 합니다.
        mercari.SEARCHES = [{"query": "test", "categories": []}]
        mercari.save_state({"old-item": 12000}, [], [], {}, {"test"}, {})
        api = FakeMercapi(
            {
                "test": [
                    FakeItem("old-item", "가격 인하", 9000),
                    FakeItem("m-unseen", "그동안 안 보이던 매물", 15000),
                ]
            }
        )

        with patch.object(mercari, "Mercapi", return_value=api):
            await mercari.collect_updates()

        state = json.loads(mercari.SEEN_FILE.read_text())
        # 신규 알림은 생략되고, 이미 추적 중이던 매물의 가격 인하 알림만 나갑니다.
        self.assertEqual(
            [entry["alert_id"] for entry in state["pending"]], ["drop:old-item:12000:9000"]
        )
        self.assertIn("m-unseen", state["seen"])

        # 기준선이 잡혔으니 다음 실행부터는 새 매물 알림이 정상적으로 옵니다.
        api2 = FakeMercapi({"test": [FakeItem("m-new", "진짜 새 매물", 21000)]})
        with patch.object(mercari, "Mercapi", return_value=api2):
            await mercari.collect_updates()

        state = json.loads(mercari.SEEN_FILE.read_text())
        self.assertIn("new:m-new", [entry["alert_id"] for entry in state["pending"]])

    def test_load_state_drops_unsupported_fingerprint_formats(self):
        # 지금 코드가 조회하지 않는 형식의 지문은 용량 상한만 잡아먹으므로 정리합니다.
        mercari.SEEN_FILE.write_text(
            json.dumps(
                {
                    "seen": {},
                    "relist_fingerprints": {
                        "seller:1:title": {"item_id": "m1"},
                        "seller-photo:1:title:http://x": {"item_id": "m2"},
                    },
                }
            )
        )
        _, _, _, fingerprints, _, _ = mercari.load_state()
        self.assertEqual(list(fingerprints), ["seller:1:title"])


if __name__ == "__main__":
    unittest.main()
