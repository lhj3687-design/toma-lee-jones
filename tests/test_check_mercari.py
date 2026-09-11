import asyncio
import importlib
import json
import sys
import tempfile
import types
import unittest
from dataclasses import dataclass
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


class FakeResults:
    def __init__(self, items):
        self.items = items


class FakeMercapi:
    def __init__(self, items_by_keyword):
        self.items_by_keyword = items_by_keyword

    async def search(self, keyword, categories):
        return FakeResults(self.items_by_keyword[keyword])


class MercariStateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.original_seen_file = mercari.SEEN_FILE
        self.original_searches = mercari.SEARCHES
        mercari.SEEN_FILE = Path(self.temp_directory.name) / "seen_items.json"

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

        await mercari.check_keyword(fake_api, "test", [], seen, new_items)

        self.assertIn("https://jp.mercari.com/shops/product/2JUHeREMxTVqFa42uQFwEc", new_items[0]["caption"])
        self.assertIn("https://jp.mercari.com/item/m90925725213", new_items[1]["caption"])

    async def test_check_keyword_never_raises_alert_baseline_scenario_from_advisor(self):
        # 조언받은 시나리오 그대로 검증:
        # 50000->48000(알림) ->55000(상승, 알림기준 불변) ->54000(알림기준 48000보다 안 쌈, 알림 없음)
        # ->46000(48000보다 1000엔 이상 싸짐, 알림)
        seen = {}
        new_items: list = []

        async def observe(price):
            new_items.clear()
            api = FakeMercapi({"test": [FakeItem("item", "Margiela bag", price)]})
            await mercari.check_keyword(api, "test", [], seen, new_items)

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
        await mercari.check_keyword(fake_api, "test", [], seen, new_items)
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

    def test_save_state_removes_duplicate_and_already_sent_alerts(self):
        pending = [
            {"alert_id": "new:a", "caption": "a"},
            {"alert_id": "new:a", "caption": "a duplicate"},
            {"alert_id": "new:b", "caption": "b"},
            {"caption": "legacy"},
        ]
        mercari.save_state({"a": 1}, pending, ["new:b", "new:already"])
        state = json.loads(mercari.SEEN_FILE.read_text())
        self.assertEqual([entry["alert_id"] for entry in state["pending"]], ["new:a", "legacy:legacy"])
        self.assertEqual(state["sent_alerts"], ["new:b", "new:already"])

    async def test_collect_persists_before_send_and_delivery_is_not_repeated(self):
        mercari.SEARCHES = [{"query": "test", "categories": []}]
        mercari.save_state({"old-item": 12000}, [], [])
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
            remaining, sent_alerts = await mercari.flush_pending({}, pending, ["new:already"])
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
            remaining, sent_alerts = await mercari.flush_pending({}, pending, [])

        # 텔레그램 전송 자체는 첫 건만 시도되고 멈춤
        send_mock.assert_awaited_once_with("a", None)
        push_mock.assert_called_once_with("record Mercari alert delivery")
        # 이미 보낸 건 sent_alerts에는 남아 있어야, 이후 save_state 때 결국 반영됨
        self.assertEqual(sent_alerts, ["new:a"])
        # 두 번째 항목은 아직 안 보냈으므로 대기열에 그대로 남아 있어야 함
        self.assertEqual([entry["alert_id"] for entry in remaining], ["new:b"])


if __name__ == "__main__":
    unittest.main()
