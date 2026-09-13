import importlib
import io
import itertools
import json
import random
import sys
import tempfile
import types
import unittest
from dataclasses import dataclass, field
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


def just_before_a_wall_clock_boundary() -> float:
    """다음 벽시계 구간 경계 1분 전을 돌려줍니다(6시간 쿨다운이면 UTC 00/06/12/18시 직전).

    2026-09-12 사고의 재발 방지선입니다. 그때 고장 알림의 alert_id는
    int(now // HEALTH_ALERT_COOLDOWN_SECONDS), 즉 벽시계 절대 시각으로 만든 구간
    번호를 달고 있었습니다. 그래서 경계를 사이에 둔 두 시각의 id가 서로 달랐고,

      - 고장이 이어지는 중인데도 경계를 넘는 순간 알림이 한 번 더 나갔습니다
        (경계 직전에 시작된 고장이면 1분 간격으로 두 건).
      - "고장이 이어지는 동안은 같은 id"를 검사하는 테스트가 경계 앞 10분에만 깨졌고,
        워크플로는 테스트가 실패하면 조회·전송을 건너뛰므로 봇이 하루 네 번,
        10분씩 멈췄습니다(실제로 UTC 05:49~05:59에 11회 연속 실패).

    지금은 고장이 시작된 시점부터 구간을 세므로 경계를 넘어도 id가 같아야 합니다.
    아래 쿨다운 테스트들은 일부러 경계를 걸치는 시각을 써서 그 성질을 지킵니다.
    """
    cooldown = mercari.HEALTH_ALERT_COOLDOWN_SECONDS
    return (datetime.now().timestamp() // cooldown + 1) * cooldown - 60


def wall_clock_positions_across_a_day() -> list[float]:
    """하루를 훑는 절대 시각들. 쿨다운 경계 바로 앞뒤를 반드시 포함합니다.

    just_before_a_wall_clock_boundary()는 경계 '한 지점'만 봅니다. 사고가 그 지점에서
    났으니 당연한 선택이었지만, 새로 추가되는 고장 알림은 그 한 지점을 지나갈 뿐
    자동으로 보호받지 못합니다. 아래 목록은 하루 전체를 훑어서, 어느 시각에 고장이
    시작되든 같은 판정이 나오는지 확인하는 데 씁니다.
    """
    cooldown = mercari.HEALTH_ALERT_COOLDOWN_SECONDS
    day = 24 * 60 * 60
    midnight = (datetime.now().timestamp() // day) * day
    positions = {midnight + half_hour * 1800 for half_hour in range(day // 1800)}
    for period in range(day // cooldown):
        edge = midnight + period * cooldown
        # 경계 자체와 그 앞뒤 1초, 그리고 '첫 알림까지 10분'이 경계를 걸치는 자리.
        positions.update({edge - 1, edge, edge + 1, edge - 599, edge + 599})
    return sorted(positions)


# 고장이 24시간 이어지는 동안 20분 간격으로 관찰합니다. 첫 알림(10분)과 쿨다운(6시간)이
# 몇 번 돌아가는지 보려면 이 정도 간격이면 충분합니다.
OUTAGE_PROBES = [minutes * 60 for minutes in range(0, 24 * 60 + 1, 20)]


def delivered_alert_ids(make_alerts, start: float) -> list[str]:
    """고장이 start에 시작해 24시간 이어질 때 실제로 전송되는 alert_id 목록.

    같은 id를 걸러 내는 sent_alerts의 동작을 그대로 재현합니다. 고장 시작 시각은
    id 안에 그대로 박히므로(벽시계 위치마다 달라집니다) 자리표시자로 바꿔서,
    '언제 시작했든 같은 순서로 같은 수만큼 나간다'만 남게 합니다.
    """
    delivered = []
    for elapsed in OUTAGE_PROBES:
        for alert in make_alerts(start, start + elapsed):
            token = alert["alert_id"].replace(str(int(start)), "<고장시작>")
            if token not in delivered:
                delivered.append(token)
    return delivered


def search_down_alerts(start: float, now: float) -> list:
    """모든 키워드 검색이 start부터 실패하고 있는 상황."""
    return mercari.health_alerts(
        [("kw", [], False, False, 0.0)], {mercari.LAST_SEARCH_OK_KEY: start}, now
    )


def created_missing_alerts(start: float, now: float) -> list:
    """등록 시각을 start부터 절반 미만만 받아오고 있는 상황."""
    rows = [{"id_": f"n{i}"} for i in range(50)]
    return mercari.created_coverage_alerts(
        [("kw", rows, True, True, 0.0)], {mercari.LAST_CREATED_OK_KEY: start}, now
    )


def keyword_stuck_alerts(start: float, now: float) -> list:
    """봇은 멀쩡한데 키워드 하나만 start부터 막혀 있는 상황."""
    return mercari.keyword_health_alerts(
        [("kw", [], False, False, 0.0), ("other", [], True, True, 0.0)],
        {"kw": start, mercari.LAST_SEARCH_OK_KEY: now - 60},
        now,
    )


def empty_feed_alerts_for(start: float, now: float) -> list:
    """검색은 성공하는데 start부터 매물이 한 건도 오지 않는 상황."""
    return mercari.empty_feed_alerts(
        [("kw", [], True, True, 0.0)], {mercari.LAST_ITEMS_OK_KEY: start}, now
    )


def cadence_slow_alerts_for(start: float, now: float) -> list:
    """start부터 실행 주기가 기대보다 느려진 상황."""
    gap = mercari.EXPECTED_RUN_INTERVAL_SECONDS * mercari.RUN_INTERVAL_SLACK * 2
    return mercari.cadence_alerts(
        {mercari.LAST_SEARCH_OK_KEY: now - gap}, {mercari.LAST_CADENCE_OK_KEY: start}, now
    )


# 쿨다운을 거는 고장 알림 전부. 새 고장 알림을 추가하면 여기에도 추가해 주세요 —
# 아래 테스트가 그 알림도 벽시계에 묶이지 않았는지 확인합니다.
OUTAGE_ALERT_FAMILIES = [
    ("전량 검색 실패", search_down_alerts),
    ("등록 시각 방어선", created_missing_alerts),
    ("키워드 하나만 막힘", keyword_stuck_alerts),
    ("검색 결과 없음", empty_feed_alerts_for),
    ("실행 주기 저하", cadence_slow_alerts_for),
]


def keyword_checkpoints(state: dict) -> dict:
    """상태 파일의 키워드별 조회 시각만 추립니다(예약 키 제외)."""
    return {
        k: v
        for k, v in state.get("keyword_checked_at", {}).items()
        if k not in mercari.RESERVED_STATE_KEYS
    }


# 픽스처의 등록 시각은 인스턴스마다 반드시 달라야 합니다.
#
# 상태 갱신 순서가 (등록 시각, ID)로 정해지므로(state_update_order), 두 매물의 등록
# 시각이 같으면 ID가 동점을 가릅니다. 그러면 '나중에 만든 매물이 더 최근'이라는
# 픽스처의 전제가 깨져서, 순서를 단정하는 테스트가 ID 사전순에 따라 뒤집힙니다.
#
# datetime.now()를 그대로 쓰면 그 일이 실제로 일어납니다. 시계 해상도가 연속 호출을
# 구분하지 못해, 실측 약 1.7%(60회 중 1회) 확률로 두 매물이 같은 값을 받았습니다.
# 봇은 1분마다 이 테스트를 돌리고, 실패하면 그 실행은 조회·전송을 건너뜁니다.
# 즉 1.7%는 하루 스무 번 넘는 봇 장애입니다 — 2026-09-12 13:18에 실제로 났습니다
# (run #6358: "Run unit tests" 실패 -> 조회·전송 건너뜀 + 실패 알림 발송).
#
# 생성 순서대로 1마이크로초씩 벌려 두면 시계 해상도와 무관해집니다.
_created_sequence = itertools.count()


def distinct_created() -> datetime:
    """생성 순서대로 서로 다른 등록 시각(위 주석 참고)."""
    return datetime.now() + timedelta(microseconds=next(_created_sequence))


@dataclass
class FakeItem:
    id_: str
    name: str
    price: int | Decimal
    thumbnails: list[str] | None = None
    item_type: str = "ITEM"
    seller_id: str | None = None
    # 실제 메루카리 응답은 등록 시각을 항상 채워 줍니다(운영 로그에서 2861/2861 확인).
    # 기본값을 비워 두면 테스트가 "등록 시각이 하나도 없다"는 경고를 CI 로그에 쏟아내
    # 진짜 경고와 구분이 안 됩니다. 그 경로를 검증하는 테스트만 created=None을 명시합니다.
    created: datetime | None = field(default_factory=distinct_created)
    is_no_price: bool = False


@dataclass
class FakeMeta:
    next_page_token: str = ""
    prev_page_token: str = ""
    num_found: int = 0


class FakeResults:
    def __init__(self, items, pages=None, fail_next_page=False, has_next=None):
        self.items = items
        self.pages = list(pages or [])
        self.fail_next_page = fail_next_page
        token = "next" if (self.pages or fail_next_page) else ""
        if has_next is not None:
            token = "next" if has_next else ""
        self.meta = FakeMeta(next_page_token=token)

    async def next_page(self):
        if self.fail_next_page:
            raise RuntimeError("다음 페이지 조회 실패 시뮬레이션")
        return FakeResults(self.pages[0], self.pages[1:])


class FakeMercapi:
    """검색 호출을 흉내 냅니다.

    실제 코드는 키워드마다 '등록순'과 '추천순'으로 두 번 조회한 뒤 결과를 합치므로,
    호출 횟수를 세어 두 번 불렸는지도 검증할 수 있게 합니다.
    """

    def __init__(
        self,
        items_by_keyword,
        fail_keywords=(),
        extra_pages_by_keyword=None,
        fail_call_indexes=None,
        fail_next_page_keywords=(),
    ):
        self.items_by_keyword = items_by_keyword
        self.fail_keywords = set(fail_keywords)
        self.extra_pages_by_keyword = extra_pages_by_keyword or {}
        # 1페이지는 주지만 2페이지 요청에서 터지는 키워드
        self.fail_next_page_keywords = set(fail_next_page_keywords)
        # 키워드별로 "몇 번째 조회를 실패시킬지" (0=등록순, 1=추천순)
        self.fail_call_indexes = fail_call_indexes or {}
        self.call_counts = {}
        self.calls = []

    async def search(self, keyword, categories=(), **options):
        index = self.call_counts.get(keyword, 0)
        self.call_counts[keyword] = index + 1
        self.calls.append((keyword, options))
        if keyword in self.fail_keywords or index in self.fail_call_indexes.get(keyword, ()):
            raise RuntimeError("검색 실패 시뮬레이션")
        pages = self.extra_pages_by_keyword.get(keyword)
        return FakeResults(
            self.items_by_keyword[keyword],
            pages,
            fail_next_page=keyword in self.fail_next_page_keywords,
        )


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

        # 이 테스트의 주제는 링크 라우팅입니다. 대기열 순서는 별도 테스트에서 다루므로
        # 여기서는 alert_id로 찾아 확인합니다.
        caption = {entry["alert_id"]: entry["caption"] for entry in new_items}
        self.assertIn(
            "https://jp.mercari.com/shops/product/2JUHeREMxTVqFa42uQFwEc",
            caption["new:2JUHeREMxTVqFa42uQFwEc"],
        )
        self.assertIn("https://jp.mercari.com/item/m90925725213", caption["new:m90925725213"])

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
        # 주제는 '신규'와 '가격 인하'가 서로 구분되는 고유 키를 갖는지입니다(순서 무관).
        self.assertEqual(
            sorted(entry["alert_id"] for entry in new_items),
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

    def test_a_broken_state_file_stops_the_run_instead_of_starting_empty(self):
        """읽지 못하는 상태 파일을 빈 상태로 갈음하면 두 가지를 한꺼번에 잃습니다.

        sent_alerts가 사라져 이미 보낸 알림이 다시 나가고, 이어지는 save_state가
        깨진 파일을 정상 파일로 덮어써 되돌릴 원본까지 사라집니다.
        조용히 넘어가는 대신 멈춰서, 워크플로 실패 알림으로 사람이 알게 해야 합니다.
        """
        mercari.SEEN_FILE.write_text("{이건 JSON이 아닙니다")
        before = mercari.SEEN_FILE.read_text()

        with self.assertRaises(RuntimeError):
            mercari.load_state()

        self.assertEqual(mercari.SEEN_FILE.read_text(), before)  # 원본은 그대로 남습니다

    def test_missing_state_file_still_starts_clean(self):
        # 파일이 아예 없는 건 정상적인 첫 실행입니다. 이때는 멈추면 안 됩니다.
        seen, pending, sent, fingerprints, _known, checked_at = mercari.load_state()
        self.assertEqual((seen, pending, sent, fingerprints, checked_at), ({}, [], [], {}, {}))

    def test_fingerprints_with_unexpected_values_are_dropped(self):
        # 지문 값에서 item_id와 가격을 꺼내 쓰므로, dict가 아닌 값이 섞이면
        # 조회가 통째로 죽고 다음 실행도 같은 자리에서 다시 죽습니다.
        pruned = mercari.prune_fingerprints(
            {"seller:1:t": {"item_id": "m1"}, "seller:2:t": "m2", "title:t:100": None}
        )
        self.assertEqual(sorted(pruned), ["seller:1:t"])

    def test_pending_entries_without_a_caption_are_dropped(self):
        """본문 없는 항목은 전송 시 터지고, 터지면 대기열에 남아 다음 실행도 터집니다.

        즉 한 번 섞여 들어오면 전송 단계가 영영 멈춥니다. 대기열을 만들 때 걸러 냅니다.
        """
        pending = [
            {"alert_id": "new:broken"},
            {"alert_id": "new:empty", "caption": "   "},
            {"alert_id": "new:ok", "caption": "정상"},
        ]
        kept = mercari.deduplicate_pending(pending, [])
        self.assertEqual([entry["alert_id"] for entry in kept], ["new:ok"])

    async def test_a_caption_less_entry_does_not_crash_the_send_step(self):
        # 어떤 경로로든 본문 없는 항목이 상태 파일에 남아 있었다면, 전송 단계가
        # 예외로 죽는 대신 그 항목만 버리고 나머지를 정상적으로 보내야 합니다.
        mercari.save_state({}, [{"alert_id": "new:broken"}, {"alert_id": "new:ok", "caption": "정상"}], [], {}, set())

        with patch.object(mercari, "send_telegram", new=AsyncMock(return_value=(True, None))), patch.object(
            mercari, "push_state", return_value=True
        ):
            await mercari.send_pending()

        state = json.loads(mercari.SEEN_FILE.read_text())
        self.assertEqual(state["pending"], [])
        self.assertEqual(state["sent_alerts"], ["new:ok"])

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
        # 등록 시각을 명시합니다. 기대 순서가 '어느 쪽이 더 최근인지'에 달려 있으므로
        # 픽스처가 그걸 분명히 정해야 합니다(기본값에 맡기면 시계 해상도에 휘둘립니다).
        now = datetime.now()
        fake_api = FakeMercapi(
            {
                "test": [
                    FakeItem("old-item", "Price drop", 9000, created=now - timedelta(minutes=10)),
                    FakeItem("new-item", "New listing", 15000, created=now),
                ]
            }
        )
        with patch.object(mercari, "Mercapi", return_value=fake_api), patch.object(
            mercari.asyncio, "sleep", new=AsyncMock()
        ):
            await mercari.collect_updates()

        collected = json.loads(mercari.SEEN_FILE.read_text())
        # 대기열은 갓 올라온 매물이 먼저입니다. 상태 갱신은 등록 시각 오름차순으로 하고
        # (state_update_order), 알림만 되돌려 내보내기 때문입니다.
        self.assertEqual(
            [entry["alert_id"] for entry in collected["pending"]],
            ["new:new-item", "drop:old-item:12000:9000"],
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
            ["new:new-item", "drop:old-item:12000:9000"],
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

        # 등록 시각을 명시합니다. seen의 순서가 (등록 시각, ID)로 정해지므로,
        # 기본값에 맡기면 두 매물이 같은 시각을 받는 순간 ID 사전순으로 뒤집힙니다.
        now = datetime.now()
        old_listing = FakeItem(
            "old", "오래 올라와 있는 매물", 10000, seller_id="A", created=now - timedelta(days=2)
        )
        fresh_listing = FakeItem("fresh", "방금 올라온 매물", 20000, seller_id="B", created=now)
        api = FakeMercapi({"test": [old_listing, fresh_listing]})
        await mercari.check_keyword(api, "test", [], seen, relist_fingerprints, new_items)
        self.assertEqual(list(seen), ["old", "fresh"])

        # "old"만 다시 관찰되면 목록 맨 뒤로 이동해야 합니다.
        new_items.clear()
        api2 = FakeMercapi({"test": [old_listing]})
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
        self.assertEqual(keyword_checkpoints(state), {})

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


    async def test_two_live_listings_sharing_a_title_are_not_merged_as_a_relist(self):
        # 판매자 ID를 알 수 없는 숍스 상품은 '제목+가격' 지문으로 재출품을 판단합니다.
        # 그런데 예전 매물이 아직 버젓이 올라와 있다면 재출품이 아니라 별개의 매물이므로,
        # 진짜 새 매물 알림이 삼켜지면 안 됩니다.
        seen: dict = {}
        relist_fingerprints: dict = {}
        new_items: list = []

        first = FakeItem("shop-1", "같은 제목 같은 가격", 5000, seller_id="0")
        api1 = FakeMercapi({"test": [first]})
        await mercari.check_keyword(api1, "test", [], seen, relist_fingerprints, new_items)
        self.assertEqual([e["alert_id"] for e in new_items], ["new:shop-1"])

        # 두 매물이 동시에 올라와 있는 상태 -> 두 번째도 새 매물로 알림이 와야 합니다.
        new_items.clear()
        second = FakeItem("shop-2", "같은 제목 같은 가격", 5000, seller_id="0")
        api2 = FakeMercapi({"test": [first, second]})
        await mercari.check_keyword(api2, "test", [], seen, relist_fingerprints, new_items)
        self.assertEqual([e["alert_id"] for e in new_items], ["new:shop-2"])

        # 반대로 예전 매물이 사라진 뒤 같은 제목+가격으로 다시 올라오면 재출품으로 처리합니다.
        new_items.clear()
        third = FakeItem("shop-3", "같은 제목 같은 가격", 5000, seller_id="0")
        api3 = FakeMercapi({"test": [third]})
        await mercari.check_keyword(api3, "test", [], seen, relist_fingerprints, new_items)
        self.assertEqual(new_items, [])

    def test_extract_item_id_is_always_a_string(self):
        # 상태 파일은 JSON이라 키가 항상 문자열이 됩니다. 정수 ID가 섞이면 저장 전후로
        # 키가 달라져서 같은 매물을 다음 실행에 '처음 보는 매물'로 오인하게 됩니다.
        self.assertEqual(mercari.extract_item_id({"id_": 12345}), "12345")
        self.assertEqual(mercari.extract_item_id({"id_": "m123"}), "m123")
        self.assertIsNone(mercari.extract_item_id({}))

    async def test_failed_alert_does_not_block_the_rest_of_the_queue(self):
        # 예전에는 실패한 알림이 대기열 맨 앞에 그대로 남아, 그 한 건 때문에 뒤에 쌓인
        # 알림이 전부 막혔습니다(실행마다 1회 재시도 -> 15분 정체).
        pending = [
            {"alert_id": "new:bad", "caption": "bad"},
            {"alert_id": "new:good", "caption": "good"},
        ]

        async def send(caption, photo):
            return (caption != "bad"), None

        with patch.object(mercari, "send_telegram", new=AsyncMock(side_effect=send)), patch.object(
            mercari, "push_state", return_value=True
        ):
            remaining, sent_alerts = await mercari.flush_pending({}, pending, [], {}, set(), {})

        # 뒤에 있던 정상 알림은 이번 실행에 전송되고, 실패한 건만 대기열에 남습니다.
        self.assertEqual(sent_alerts, ["new:good"])
        self.assertEqual([e["alert_id"] for e in remaining], ["new:bad"])
        self.assertEqual(remaining[0]["attempts"], 1)

    async def test_flush_pending_stops_when_everything_keeps_failing(self):
        # 토큰 오류나 텔레그램 장애처럼 전부 실패하는 상황에서 대기열을 몇 바퀴씩
        # 헛돌지 않아야 합니다. 그리고 조용히 끝나면 안 됩니다 — 전송 경로가 막히면
        # 고장을 알릴 수단도 같이 막히므로, 실행을 실패시켜 Actions 탭에 드러냅니다.
        pending = [{"alert_id": f"new:{i}", "caption": str(i)} for i in range(20)]
        send_mock = AsyncMock(return_value=(False, None))

        with patch.object(mercari, "send_telegram", new=send_mock), patch.object(
            mercari, "push_state", return_value=True
        ), self.assertRaises(mercari.SendBlocked):
            await mercari.flush_pending({}, pending, [], {}, set(), {})

        self.assertEqual(send_mock.await_count, mercari.MAX_CONSECUTIVE_SEND_FAILURES)
        self.assertEqual(len(pending), 20)  # 한 건도 잃지 않고 다음 실행으로 넘김

    async def test_a_total_send_outage_does_not_burn_the_per_alert_retry_budget(self):
        """전송이 통째로 막힌 건 개별 알림의 잘못이 아닙니다.

        예전에는 막힌 실행에서도 재시도 횟수를 올렸습니다. 봇이 1분마다 도니까 텔레그램이
        몇 시간 막히면 멀쩡한 알림들이 차례로 MAX_ALERT_ATTEMPTS에 걸려 폐기됐고,
        워크플로는 초록색이라 아무도 알 수 없었습니다.
        """
        pending = [
            {"alert_id": f"new:{i}", "caption": str(i)}
            for i in range(mercari.MAX_CONSECUTIVE_SEND_FAILURES)
        ]

        for _run in range(mercari.MAX_ALERT_ATTEMPTS + 2):
            with patch.object(
                mercari, "send_telegram", new=AsyncMock(return_value=(False, None))
            ), patch.object(mercari, "push_state", return_value=True), self.assertRaises(
                mercari.SendBlocked
            ):
                await mercari.flush_pending({}, pending, [], {}, set(), {})

        # 몇 번을 막혀도 재시도 횟수가 쌓이지 않고, 알림도 버려지지 않습니다.
        self.assertEqual(len(pending), mercari.MAX_CONSECUTIVE_SEND_FAILURES)
        self.assertEqual([e.get("attempts") for e in pending], [None] * len(pending))

    async def test_an_alert_specific_failure_still_counts_toward_giving_up(self):
        # 위와 달리 '이 알림만' 실패하는 경우는 그대로 재시도 횟수를 세야 합니다.
        # (전체 장애와 구분하지 못하면 깨진 알림이 대기열에 영영 남습니다.)
        pending = [
            {"alert_id": "new:bad", "caption": "bad"},
            {"alert_id": "new:good", "caption": "good"},
        ]

        async def send(caption, photo):
            return caption != "bad", None

        with patch.object(mercari, "send_telegram", new=AsyncMock(side_effect=send)), patch.object(
            mercari, "push_state", return_value=True
        ):
            remaining, _ = await mercari.flush_pending({}, pending, [], {}, set(), {})

        self.assertEqual([e["alert_id"] for e in remaining], ["new:bad"])
        self.assertEqual(remaining[0]["attempts"], 1)

    async def test_send_step_exits_non_zero_when_sending_is_blocked(self):
        # 전송 단계가 실패로 끝나야 워크플로가 빨간색이 되고 기존 실행-실패 알림이
        # 나갑니다. 상태(대기열·전송 기록)는 그 전에 저장되어 있어야 합니다.
        mercari.save_state({}, [{"alert_id": "new:a", "caption": "a"}], [], {}, set())

        with patch.object(
            mercari, "send_telegram", new=AsyncMock(return_value=(False, None))
        ), patch.object(mercari, "push_state", return_value=True), patch.object(
            mercari.sys, "argv", ["check_mercari.py", "--mode", "send"]
        ), patch("sys.stderr", new=io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                await mercari.main()

        self.assertEqual(caught.exception.code, 1)
        state = json.loads(mercari.SEEN_FILE.read_text())
        self.assertEqual([e["alert_id"] for e in state["pending"]], ["new:a"])

    async def test_a_merged_push_result_is_not_overwritten_by_the_next_alert(self):
        """push_state가 원격과 병합한 결과를 다음 알림이 덮어쓰면 안 됩니다.

        push_state.sh는 push가 거부되면 원격 상태를 받아 병합하고 seen_items.json을
        그 결과로 고쳐 씁니다. 그런데 전송 루프는 알림 하나마다 자기 메모리 값으로
        파일을 다시 쓰므로, 되읽지 않으면 병합으로 들어온 상대편 기록이 사라집니다.
        그리고 그 다음 push는 충돌이 없어 통과하기 때문에 병합 경로가 다시 불려
        복구될 기회도 없습니다 — 상대편이 이미 보낸 알림이 다시 나갑니다.
        """
        pending = [
            {"alert_id": "new:a", "caption": "a"},
            {"alert_id": "new:b", "caption": "b"},
        ]

        def merging_push(_message):
            # 첫 push만 거부돼 원격과 병합된 상태가 파일에 남은 상황을 만듭니다.
            state = json.loads(mercari.SEEN_FILE.read_text())
            if "new:theirs" not in state["sent_alerts"]:
                state["sent_alerts"].append("new:theirs")
                state["pending"].append({"alert_id": "new:theirs-queued", "caption": "그쪽 대기열"})
                state["seen"]["m-theirs"] = {"last_alert_price": 1, "last_seen_price": 1}
                mercari.SEEN_FILE.write_text(json.dumps(state, ensure_ascii=False))
            return True

        seen = {}
        with patch.object(mercari, "send_telegram", new=AsyncMock(return_value=(True, None))), \
             patch.object(mercari, "push_state", side_effect=merging_push):
            remaining, sent_alerts = await mercari.flush_pending(seen, pending, [], {}, set(), {})

        # 상대편의 전송 기록이 살아 있어야 그 알림이 다시 나가지 않습니다.
        self.assertIn("new:theirs", sent_alerts)
        # 상대편이 본 매물도 남아야 다음 조회에서 '신규'로 오인하지 않습니다.
        self.assertIn("m-theirs", seen)
        # 상대편 대기열도 잃지 않습니다(이번에 보내거나 다음 실행으로 넘깁니다).
        queued = [entry["alert_id"] for entry in remaining]
        self.assertTrue(
            "new:theirs-queued" in sent_alerts or "new:theirs-queued" in queued,
            (sent_alerts, queued),
        )

    def test_absorbing_a_push_changes_nothing_when_there_was_no_merge(self):
        """병합이 없었다면 되읽기는 아무것도 바꾸지 않아야 합니다.

        이 성질이 깨지면 평소(충돌 없는) 실행의 동작까지 달라집니다.
        """
        seen = {"m1": {"last_alert_price": 1, "last_seen_price": 1}}
        pending = [{"alert_id": "new:x", "caption": "x"}]
        sent_alerts = ["new:done"]
        fingerprints = {"seller:1:제목": {"item_id": "m1", "last_alert_price": 1, "last_seen_price": 1}}
        known = {"kw"}
        checked_at = {"kw": 1000.0}
        mercari.save_state(seen, pending, sent_alerts, fingerprints, known, checked_at)
        before = (dict(seen), list(pending), list(sent_alerts), dict(fingerprints), set(known), dict(checked_at))

        mercari.absorb_pushed_state(seen, pending, sent_alerts, fingerprints, known, checked_at)

        self.assertEqual((seen, pending, sent_alerts, fingerprints, known, checked_at), before)

    async def test_flush_pending_caps_how_much_it_sends_in_one_run(self):
        # 한 실행이 5분 크론을 넘겨 다음 실행들이 줄줄이 밀리지 않도록 상한을 둡니다.
        count = mercari.MAX_SEND_ATTEMPTS_PER_RUN + 10
        pending = [{"alert_id": f"new:{i}", "caption": str(i)} for i in range(count)]

        with patch.object(mercari, "send_telegram", new=AsyncMock(return_value=(True, None))), patch.object(
            mercari, "push_state", return_value=True
        ):
            remaining, sent_alerts = await mercari.flush_pending({}, pending, [], {}, set(), {})

        self.assertEqual(len(sent_alerts), mercari.MAX_SEND_ATTEMPTS_PER_RUN)
        self.assertEqual(len(remaining), 10)  # 나머지는 대기열에 그대로 보존

    async def test_send_stops_when_the_run_time_budget_is_spent(self):
        """건수 상한만으로는 실행 시간이 묶이지 않습니다.

        텔레그램이 레이트리밋을 걸면 한 건마다 최대 1분을 기다리므로 상한 40건이
        곧 40분이 될 수 있고, concurrency 그룹 때문에 그동안 조회까지 전부 멈춥니다.
        시간 상한에 닿으면 남은 대기열을 그대로 두고 이번 실행을 끝내야 합니다.
        """
        pending = [{"alert_id": f"new:{i}", "caption": str(i)} for i in range(5)]
        # 한 번 볼 때마다 100초씩 흐르는 시계: 예산(4분)이 곧 바닥납니다.
        elapsed = {"now": 0.0}

        def ticking_clock():
            elapsed["now"] += 100.0
            return elapsed["now"]

        with patch.object(mercari, "send_telegram", new=AsyncMock(return_value=(True, None))), patch.object(
            mercari, "push_state", return_value=True
        ), patch.object(mercari, "current_time", side_effect=ticking_clock):
            remaining, sent_alerts = await mercari.flush_pending({}, pending, [], {}, set(), {})

        self.assertEqual(sent_alerts, ["new:0", "new:1"])
        # 남은 건은 한 건도 잃지 않고 다음 실행으로 넘어갑니다.
        self.assertEqual([e["alert_id"] for e in remaining], ["new:2", "new:3", "new:4"])

    async def test_rate_limit_wait_never_exceeds_the_run_time_budget(self):
        # 429가 이어질 때 "기다렸다 재시도"만 반복하면 한 실행이 수십 분을 잡아먹습니다.
        pending = [{"alert_id": f"new:{i}", "caption": str(i)} for i in range(5)]
        elapsed = {"now": 0.0}

        def ticking_clock():
            elapsed["now"] += 60.0
            return elapsed["now"]

        with patch.object(
            mercari, "send_telegram", new=AsyncMock(return_value=(False, 60))
        ), patch.object(mercari, "push_state", return_value=True), patch.object(
            mercari, "current_time", side_effect=ticking_clock
        ):
            remaining, sent_alerts = await mercari.flush_pending({}, pending, [], {}, set(), {})

        self.assertEqual(sent_alerts, [])
        self.assertEqual(len(remaining), 5)  # 대기열은 그대로 보존
        self.assertLess(mercari.asyncio.sleep.await_count, 5)  # 예산을 넘겨 가며 기다리지 않음

    async def test_photo_failure_falls_back_to_a_text_message(self):
        # 메루카리 썸네일은 webp라 텔레그램이 사진으로 거부하는 경우가 있습니다.
        # 사진 때문에 알림 자체를 놓치면 안 됩니다.
        calls = []

        async def fake_post(token, chat_id, method, payload):
            calls.append(method)
            if method == "sendPhoto":
                return False, None, 400
            return True, None, 200

        with patch.dict(
            mercari.os.environ, {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "c"}
        ), patch.object(mercari, "_telegram_post", new=AsyncMock(side_effect=fake_post)):
            ok, _ = await mercari.send_telegram("caption", "https://example.com/a.webp")

        self.assertTrue(ok)
        self.assertEqual(calls, ["sendPhoto", "sendMessage"])

    async def test_rate_limited_photo_is_not_downgraded_to_text(self):
        # 429(레이트리밋)는 사진이 문제가 아니라 잠시 기다리라는 뜻이므로
        # 텍스트로 바꿔 보내면 안 됩니다.
        async def fake_post(token, chat_id, method, payload):
            return False, 20, 429

        with patch.dict(
            mercari.os.environ, {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "c"}
        ), patch.object(mercari, "_telegram_post", new=AsyncMock(side_effect=fake_post)) as post:
            ok, retry_after = await mercari.send_telegram("caption", "https://example.com/a.webp")

        self.assertFalse(ok)
        self.assertEqual(retry_after, 20)
        self.assertEqual(post.await_count, 1)

    async def test_next_page_is_fetched_when_a_full_page_is_all_brand_new(self):
        # 인기 키워드에서 짧은 시간에 매물이 쏟아지면 한 페이지(120개)로는 모자랍니다.
        # 페이지 전체가 기준선 이후 등록분이면 다음 페이지도 확인해야 놓치지 않습니다.
        now = datetime.now()
        cutoff = (now - timedelta(minutes=5)).timestamp()
        page1 = [
            FakeItem(f"p1-{i}", f"신규 {i}", 1000, created=now - timedelta(minutes=1))
            for i in range(mercari.MAX_ITEMS_PER_KEYWORD)
        ]
        page2 = [FakeItem("p2-0", "신규 추가분", 1000, created=now - timedelta(minutes=2))]
        api = FakeMercapi({"test": page1}, extra_pages_by_keyword={"test": [page2]})

        items, ok, _ = await mercari.search_items(api, "test", [], created_cutoff=cutoff)
        self.assertTrue(ok)
        self.assertIn("p2-0", {mercari.extract_item_id(f) for f in items})

    async def test_next_page_is_not_fetched_for_a_normal_page(self):
        # 평소에는(한 페이지를 다 채우지 못하거나 오래된 매물이 섞여 있으면) 한 페이지만 봅니다.
        now = datetime.now()
        cutoff = (now - timedelta(minutes=5)).timestamp()
        page1 = [FakeItem("p1-0", "신규", 1000, created=now - timedelta(minutes=1))]
        page2 = [FakeItem("p2-0", "더 있음", 1000, created=now - timedelta(minutes=2))]
        api = FakeMercapi({"test": page1}, extra_pages_by_keyword={"test": [page2]})

        items, _, _ = await mercari.search_items(api, "test", [], created_cutoff=cutoff)
        self.assertNotIn("p2-0", {mercari.extract_item_id(f) for f in items})

    async def test_a_failure_mid_pagination_does_not_advance_the_checkpoint(self):
        """1페이지는 받았는데 2페이지에서 터진 경우는 '훑었다'가 아닙니다.

        2페이지를 요청했다는 건 1페이지가 전부 기준선 이후 등록분이어서 "뒤에 새 매물이
        더 있다"고 판단했다는 뜻입니다. 거기서 실패했는데도 조회 시각을 전진시키면,
        못 읽은 페이지의 새 매물이 다음 실행에서 '오래된 매물'로 분류돼 영영 묻힙니다.
        """
        now = datetime.now()
        cutoff = (now - timedelta(minutes=5)).timestamp()
        full_fresh_page = [
            FakeItem(f"p1-{i}", f"신규 {i}", 1000, created=now - timedelta(minutes=1))
            for i in range(mercari.MAX_ITEMS_PER_KEYWORD)
        ]
        api = FakeMercapi({"test": full_fresh_page}, fail_next_page_keywords=["test"])

        items, succeeded, coverage = await mercari.search_items(
            api, "test", [], created_cutoff=cutoff, sort_passes=["created"]
        )

        self.assertTrue(succeeded)  # 1페이지는 받았으므로 그 결과는 그대로 씁니다
        self.assertEqual(len(items), mercari.MAX_ITEMS_PER_KEYWORD)
        self.assertFalse(coverage)  # 기준선은 전진하지 않습니다

    async def test_a_complete_single_page_pass_advances_the_checkpoint(self):
        # 평소(한 페이지로 끝나는 경우)에는 당연히 훑은 것으로 인정해야 합니다.
        now = datetime.now()
        api = FakeMercapi({"test": [FakeItem("m1", "신규", 1000, created=now)]})

        _items, succeeded, coverage = await mercari.search_items(
            api, "test", [], created_cutoff=(now - timedelta(minutes=5)).timestamp(),
            sort_passes=["created"],
        )

        self.assertTrue(succeeded)
        self.assertTrue(coverage)

    async def test_exhausting_the_page_budget_still_counts_as_covered(self):
        """페이지 상한까지 다 쓴 경우는 인정해야 합니다.

        인정하지 않으면 매물이 쏟아지는 키워드의 기준선이 영영 전진하지 못해
        매 실행 같은 구간을 다시 훑고, 키워드 단위 고장 알림까지 헛되게 울립니다.
        """
        now = datetime.now()
        cutoff = (now - timedelta(minutes=5)).timestamp()

        def full_page(tag):
            return [
                FakeItem(f"{tag}-{i}", f"신규 {i}", 1000, created=now - timedelta(minutes=1))
                for i in range(mercari.MAX_ITEMS_PER_KEYWORD)
            ]

        # 어느 페이지에나 다음 페이지가 남아 있어서 상한에 걸려 멈추는 상황
        pages = [full_page(f"p{n}") for n in range(1, mercari.MAX_SEARCH_PAGES + 2)]
        api = FakeMercapi({"test": pages[0]}, extra_pages_by_keyword={"test": pages[1:]})

        _items, succeeded, coverage = await mercari.search_items(
            api, "test", [], created_cutoff=cutoff, sort_passes=["created"]
        )

        self.assertTrue(succeeded)
        self.assertTrue(coverage)
        self.assertEqual(api.call_counts["test"], 1)  # search 1회 + next_page 반복

    async def test_mid_pagination_failure_keeps_the_keyword_checkpoint_in_place(self):
        # 위 단위 동작이 실제 collect 흐름에서도 기준선을 지키는지 확인합니다.
        mercari.SEARCHES = [{"query": "test", "categories": []}]
        base = datetime.now().timestamp()
        mercari.save_state(
            {"x": 1}, [], [], {}, {"test"},
            {"test": base - 60, mercari.FULL_SCAN_STATE_KEY: base - 60},
        )
        now = datetime.now()
        full_fresh_page = [
            FakeItem(f"p1-{i}", f"신규 {i}", 1000, created=now)
            for i in range(mercari.MAX_ITEMS_PER_KEYWORD)
        ]
        api = FakeMercapi({"test": full_fresh_page}, fail_next_page_keywords=["test"])

        with patch.object(mercari, "Mercapi", return_value=api), patch.object(
            mercari, "current_time", return_value=base
        ):
            await mercari.collect_updates()

        state = json.loads(mercari.SEEN_FILE.read_text())
        self.assertEqual(keyword_checkpoints(state), {"test": base - 60})

    def test_state_for_removed_keywords_is_forgotten(self):
        """SEARCHES에서 빠진 키워드 기록을 남겨 두면 두 가지가 따라옵니다.

        상태 파일이 상한 없이 조금씩 커지고, 나중에 그 키워드를 되살릴 때
        '첫 조회는 기준선만 저장' 장치가 동작하지 않아 최대 24시간치 매물이
        한꺼번에 신규 알림으로 쏟아집니다.
        """
        mercari.SEARCHES = [{"query": "살아있는 키워드", "categories": []}]
        known = {"살아있는 키워드", "지운 키워드"}
        checked_at = {
            "살아있는 키워드": 100.0,
            "지운 키워드": 200.0,
            mercari.FULL_SCAN_STATE_KEY: 300.0,
            mercari.LAST_SEARCH_OK_KEY: 400.0,
        }

        mercari.forget_removed_keywords(known, checked_at)

        self.assertEqual(known, {"살아있는 키워드"})
        self.assertEqual(
            checked_at,
            {
                "살아있는 키워드": 100.0,
                mercari.FULL_SCAN_STATE_KEY: 300.0,
                mercari.LAST_SEARCH_OK_KEY: 400.0,
            },
        )

    def test_forgetting_keywords_never_touches_reserved_keys(self):
        # 예약 키를 키워드로 오인해 지우면 전체 조회 간격과 고장 감지가 초기화됩니다.
        mercari.SEARCHES = []
        checked_at = {key: 1.0 for key in mercari.RESERVED_STATE_KEYS}
        mercari.forget_removed_keywords(set(), checked_at)
        self.assertEqual(sorted(checked_at), sorted(mercari.RESERVED_STATE_KEYS))

    async def test_a_readded_keyword_only_records_a_baseline_again(self):
        # 키워드를 지웠다가 되살리면 '첫 조회' 취급을 받아 알림이 생략되어야 합니다.
        base = datetime.now().timestamp()
        mercari.SEARCHES = [{"query": "kw", "categories": []}]
        mercari.save_state(
            {"x": 1}, [], [], {}, {"kw"},
            {"kw": base - 60, mercari.FULL_SCAN_STATE_KEY: base - 60},
        )

        # 1) 키워드를 지운 상태로 한 번 실행 -> 기록이 정리됩니다.
        mercari.SEARCHES = [{"query": "other", "categories": []}]
        with patch.object(
            mercari, "Mercapi", return_value=FakeMercapi({"other": []})
        ), patch.object(mercari, "current_time", return_value=base):
            await mercari.collect_updates()

        state = json.loads(mercari.SEEN_FILE.read_text())
        self.assertNotIn("kw", state["keyword_checked_at"])
        self.assertNotIn("kw", state["known_keywords"])

        # 2) 되살린 뒤 첫 조회: 이미 올라와 있던 매물이 알림 폭탄이 되면 안 됩니다.
        mercari.SEARCHES = [{"query": "kw", "categories": []}]
        old_listing = FakeItem("m-old", "예전 매물", 5000, created=datetime.now())
        with patch.object(
            mercari, "Mercapi", return_value=FakeMercapi({"kw": [old_listing]})
        ), patch.object(mercari, "current_time", return_value=base + 120):
            await mercari.collect_updates()

        state = json.loads(mercari.SEEN_FILE.read_text())
        self.assertEqual(state["pending"], [])
        self.assertIn("m-old", state["seen"])

    async def test_same_listings_in_a_different_search_order_save_an_identical_file(self):
        """이 PR의 핵심 성질입니다.

        remember()는 항목을 dict 맨 뒤로 보내므로 '갱신 순서'가 곧 상태 파일의 바이트
        배치입니다. 예전에는 검색 결과 순서대로 갱신했는데 메루카리 결과 순서는 매 실행
        흔들려서, 같은 매물을 같은 집합으로 다시 봐도 1.5MB 파일이 통째로 다시 쓰였습니다
        (실측 커밋당 9.3KB / 1년 약 11.5GB).

        같은 매물 집합을 본 실행이라면 검색 순서가 어떻든 저장 결과가 같아야 합니다.
        """
        now = datetime.now()
        listings = [
            FakeItem(f"m{i}", f"매물 {i}", 10000 + i, created=now - timedelta(minutes=i))
            for i in range(12)
        ]

        async def collect(order):
            mercari.SEEN_FILE.unlink(missing_ok=True)
            mercari.SEARCHES = [{"query": "kw", "categories": []}]
            base = now.timestamp()
            mercari.save_state(
                {"기존": {"last_alert_price": 1, "last_seen_price": 1}}, [], [], {}, {"kw"},
                {"kw": base - 60, mercari.FULL_SCAN_STATE_KEY: base - 60},
            )
            api = FakeMercapi({"kw": order})
            with patch.object(mercari, "Mercapi", return_value=api), patch.object(
                mercari, "current_time", return_value=base
            ):
                await mercari.collect_updates()
            return mercari.SEEN_FILE.read_text()

        forward = await collect(list(listings))
        shuffled = list(listings)
        random.Random(1).shuffle(shuffled)
        reshuffled = await collect(shuffled)
        reversed_order = await collect(list(reversed(listings)))

        self.assertNotEqual(forward, "")
        self.assertEqual(forward, reshuffled)
        self.assertEqual(forward, reversed_order)

    async def test_the_newest_listing_sits_furthest_from_the_capacity_cut(self):
        """용량 상한은 dict 앞에서부터 잘라냅니다(save_state의 [-MAX:]).

        갱신을 등록 시각 오름차순으로 하므로 갓 올라온 매물이 맨 뒤, 즉 잘릴 위험이
        가장 적은 자리에 놓입니다. 예전에는 검색 결과 위치에 따라 아무 데나 놓였습니다.
        """
        now = datetime.now()
        items = [
            FakeItem("m-oldest", "가장 오래된", 1000, created=now - timedelta(days=3)),
            FakeItem("m-newest", "방금", 2000, created=now),
            FakeItem("m-middle", "중간", 3000, created=now - timedelta(hours=5)),
        ]
        seen: dict = {}
        await mercari.check_keyword(
            FakeMercapi({"kw": items}), "kw", [], seen, {}, [],
            created_cutoff=(now - timedelta(minutes=5)).timestamp(),
        )

        self.assertEqual(list(seen), ["m-oldest", "m-middle", "m-newest"])

    async def test_listings_without_a_created_time_are_cut_first(self):
        # 등록 시각을 모르는 매물은 신규 판정에서도 근거가 가장 약합니다.
        # 상한에 먼저 닿는 앞자리에 두는 편이 맞습니다.
        now = datetime.now()
        items = [
            FakeItem("m-known", "등록시각 있음", 1000, created=now),
            FakeItem("m-unknown", "등록시각 없음", 2000, created=None),
        ]
        seen: dict = {}
        await mercari.check_keyword(FakeMercapi({"kw": items}), "kw", [], seen, {}, [])

        self.assertEqual(list(seen), ["m-unknown", "m-known"])

    async def test_alerts_still_go_out_newest_first(self):
        """운영에서 등록순 조회는 최신 매물을 먼저 돌려줍니다.

        상태 갱신 순서를 오름차순으로 바꿨지만 알림 순서는 예전과 같아야 합니다.
        """
        now = datetime.now()
        newest_first = [
            FakeItem("m-new", "방금", 1000, created=now),
            FakeItem("m-mid", "조금 전", 1000, created=now - timedelta(minutes=3)),
            FakeItem("m-old", "더 전", 1000, created=now - timedelta(minutes=9)),
        ]
        alerts: list = []
        await mercari.check_keyword(
            FakeMercapi({"kw": newest_first}), "kw", [], {}, {}, alerts,
            created_cutoff=(now - timedelta(minutes=30)).timestamp(),
        )

        self.assertEqual(
            [e["alert_id"] for e in alerts], ["new:m-new", "new:m-mid", "new:m-old"]
        )

    def test_fixture_listings_never_share_a_created_time(self):
        """이 성질이 깨지면 순서를 단정하는 테스트들이 시계 해상도에 휘둘립니다.

        예전에는 FakeItem의 기본 등록 시각이 datetime.now()였고, 연속 생성이 같은 값을
        받는 일이 실측 약 1.7% 있었습니다. 봇은 1분마다 테스트를 돌리고 실패하면 그 실행은
        조회·전송을 건너뛰므로 하루 스무 번 넘는 장애였습니다(2026-09-12 13:18 run #6358).
        """
        created = [FakeItem(f"m{i}", "매물", 1000).created for i in range(300)]
        self.assertEqual(len(set(created)), len(created))
        # 생성 순서 = 등록 시각 순서 (픽스처가 '나중에 만든 게 더 최근'을 보장)
        self.assertEqual(created, sorted(created))

    async def test_an_empty_feed_is_not_mistaken_for_good_health(self):
        """검색은 성공하는데 매물이 0건인 상태는 기존 감지에 전부 걸리지 않습니다.

        search_items는 예외만 안 나면 succeeded=True라서, 메루카리가 빈 결과를
        돌려주면 health_alerts는 정상, keyword_checked_at은 전진,
        created_coverage_alerts는 표본이 없다며 판단 보류합니다. 모든 신호가 초록인 채로
        아무것도 못 찾는 상태입니다.
        """
        base = datetime.now().timestamp()
        empty = [("kw", [], True, True, 0.0)]

        # 기존 감지는 전부 '정상'이라고 합니다.
        # (직전 성공이 최근이라 '복구' 알림도 나올 이유가 없는 상태입니다.)
        checked_at = {mercari.LAST_SEARCH_OK_KEY: base - 60}
        self.assertEqual(mercari.health_alerts(empty, checked_at, base), [])
        self.assertEqual(checked_at[mercari.LAST_SEARCH_OK_KEY], base)  # 정상으로 보고 전진까지 합니다
        self.assertEqual(mercari.created_coverage_alerts(empty, {}, base), [])

        # 새 감지는 잡습니다(단, 이어질 때만).
        fresh = {mercari.LAST_ITEMS_OK_KEY: base - 60}
        self.assertEqual(mercari.empty_feed_alerts(empty, fresh, base), [])

        stale = {mercari.LAST_ITEMS_OK_KEY: base - mercari.HEALTH_ALERT_AFTER_SECONDS - 60}
        alerts = mercari.empty_feed_alerts(empty, stale, base)
        self.assertEqual(len(alerts), 1)
        self.assertTrue(alerts[0]["alert_id"].startswith("health:empty-feed:"))

    async def test_an_empty_feed_alert_needs_a_successful_search(self):
        # 전량 검색 실패는 health_alerts가 다룹니다. 여기서 또 알리면 중복입니다.
        base = datetime.now().timestamp()
        failed = [("kw", [], False, False, 0.0)]
        stale = {mercari.LAST_ITEMS_OK_KEY: base - mercari.HEALTH_ALERT_AFTER_SECONDS - 60}
        self.assertEqual(mercari.empty_feed_alerts(failed, stale, base), [])

    async def test_the_feed_recovery_is_announced(self):
        base = datetime.now().timestamp()
        got = [("kw", [{"id_": "m1", "created": datetime.now()}], True, True, 0.0)]
        checked_at = {mercari.LAST_ITEMS_OK_KEY: base - mercari.HEALTH_ALERT_AFTER_SECONDS - 60}

        announced = [f"health:empty-feed:{int(checked_at[mercari.LAST_ITEMS_OK_KEY])}:0"]
        alerts = mercari.empty_feed_alerts(got, checked_at, base, announced)

        self.assertEqual(len(alerts), 1)
        self.assertIn("복구", alerts[0]["caption"])
        self.assertEqual(checked_at[mercari.LAST_ITEMS_OK_KEY], base)

    async def test_the_feed_recovery_is_silent_when_nothing_was_announced(self):
        base = datetime.now().timestamp()
        got = [("kw", [{"id_": "m1", "created": datetime.now()}], True, True, 0.0)]
        checked_at = {mercari.LAST_ITEMS_OK_KEY: base - mercari.HEALTH_ALERT_AFTER_SECONDS - 60}

        self.assertEqual(mercari.empty_feed_alerts(got, checked_at, base, []), [])
        self.assertEqual(checked_at[mercari.LAST_ITEMS_OK_KEY], base)

    async def test_a_slower_run_cadence_is_detected(self):
        """1분 주기는 저장소 밖의 cron이 만듭니다.

        그게 멈춰도 워크플로의 5분 스케줄 덕에 봇은 계속 돌고 검색도 성공합니다.
        기존 고장 알림은 하나도 울리지 않는데 새 매물 알림만 최대 5분 늦어집니다.
        """
        base = datetime.now().timestamp()
        slack = mercari.EXPECTED_RUN_INTERVAL_SECONDS * mercari.RUN_INTERVAL_SLACK

        # 정상 주기: 알리지 않고 기준선만 전진합니다.
        checked_at = {}
        self.assertEqual(
            mercari.cadence_alerts({mercari.LAST_SEARCH_OK_KEY: base - 60}, checked_at, base), []
        )
        self.assertEqual(checked_at[mercari.LAST_CADENCE_OK_KEY], base)

        # 벌어졌지만 아직 짧으면 알리지 않습니다(취소로 한두 번 건너뛰는 건 정상).
        previous = {mercari.LAST_SEARCH_OK_KEY: base - slack - 60}
        self.assertEqual(
            mercari.cadence_alerts(previous, {mercari.LAST_CADENCE_OK_KEY: base - 60}, base), []
        )

        # 이어지면 알립니다.
        stale = {mercari.LAST_CADENCE_OK_KEY: base - mercari.HEALTH_ALERT_AFTER_SECONDS - 60}
        alerts = mercari.cadence_alerts(previous, stale, base)
        self.assertEqual(len(alerts), 1)
        self.assertTrue(alerts[0]["alert_id"].startswith("health:cadence-slow:"))

    async def test_the_first_run_never_reports_a_slow_cadence(self):
        # 기준선이 없는 첫 실행을 '주기 저하'로 오인하면 안 됩니다.
        base = datetime.now().timestamp()
        self.assertEqual(mercari.cadence_alerts({}, {}, base), [])

    async def test_the_cadence_check_runs_before_the_search_ok_timestamp_moves(self):
        """순서가 뒤집히면 주기 저하를 영영 못 봅니다.

        health_alerts가 __last_search_ok__를 이번 실행 시각으로 덮어쓰기 때문에,
        그 뒤에 cadence_alerts를 부르면 간격이 0으로 보입니다.
        """
        base = datetime.now().timestamp()
        searched = [("kw", [{"id_": "m1", "created": datetime.now()}], True, True, 0.0)]
        checked_at = {mercari.LAST_SEARCH_OK_KEY: base - 600}

        mercari.health_alerts(searched, checked_at, base)

        # 덮어쓴 뒤의 값으로는 간격이 사라집니다 — 그래서 스냅샷을 써야 합니다.
        self.assertEqual(checked_at[mercari.LAST_SEARCH_OK_KEY], base)
        self.assertEqual(mercari.cadence_alerts(checked_at, {}, base), [])

    def test_outage_alert_ids_never_depend_on_the_wall_clock(self):
        """고장 알림의 쿨다운은 '고장이 시작된 시점'으로만 세야 합니다.

        2026-09-12 사고의 일반화된 재발 방지선입니다. 그때 alert_id는
        int(now // HEALTH_ALERT_COOLDOWN_SECONDS), 즉 벽시계 절대 시각으로 만든 구간
        번호를 달고 있었습니다. 그래서 고장이 이어지는 중에 경계(UTC 00/06/12/18시)를
        넘으면 id가 갈라져 알림이 한 건 더 나갔고, 그 성질을 검사하는 테스트가 경계 앞
        10분에만 깨졌습니다. 봇은 1분마다 테스트를 돌리고 실패하면 그 실행의 조회·전송을
        건너뛰므로, 하루 네 번 10분씩 멈췄습니다(UTC 05:49~05:59, 11회 연속 실패).

        기존 쿨다운 테스트들은 경계 '한 지점'만 걸칩니다. 하루 중 10분짜리 창에서만
        어긋나는 결합(사고와 같은 모양)은 그 지점을 비껴가면 통과합니다. 여기서는 하루
        전체를 훑으므로, 나중에 추가되는 고장 알림이 다시 벽시계에 묶여도 걸립니다.
        """
        positions = wall_clock_positions_across_a_day()
        self.assertGreater(len(positions), 50)  # 하루를 실제로 훑고 있는지

        for name, make_alerts in OUTAGE_ALERT_FAMILIES:
            with self.subTest(알림=name):
                reference_start = positions[0]
                reference = delivered_alert_ids(make_alerts, reference_start)

                # 쿨다운이 실제로 여러 번 돌아가는 표본이어야 의미가 있습니다.
                self.assertGreaterEqual(len(reference), 3, reference)

                for start in positions[1:]:
                    self.assertEqual(
                        delivered_alert_ids(make_alerts, start),
                        reference,
                        f"고장 시작 시각이 벽시계 어디냐에 따라 알림이 달라집니다 "
                        f"({name}): {start} vs {reference_start}",
                    )

    def test_state_update_order_is_independent_of_search_position(self):
        # 정렬 키가 매물에 붙어 있는 값만 쓰는지(=실행과 무관한지) 확인합니다.
        now = datetime.now()
        a = {"id_": "m2", "created": now}
        b = {"id_": "m1", "created": now - timedelta(hours=1)}
        c = {"id_": "m3"}  # 등록 시각 없음
        self.assertEqual(
            [f["id_"] for f in sorted([a, b, c], key=mercari.state_update_order)],
            ["m3", "m1", "m2"],
        )
        # 같은 등록 시각이면 ID로 갈라서 순서가 흔들리지 않게 합니다.
        same = [{"id_": "m9", "created": now}, {"id_": "m8", "created": now}]
        self.assertEqual(
            [f["id_"] for f in sorted(same, key=mercari.state_update_order)], ["m8", "m9"]
        )

    def test_pending_queue_overflow_is_reported(self):
        # 상한을 넘겨 알림이 버려지는 상황은 조용히 넘어가면 안 됩니다.
        pending = [{"alert_id": f"new:{i}", "caption": str(i)} for i in range(mercari.MAX_PENDING_ALERTS + 3)]
        with patch("sys.stderr", new=io.StringIO()) as captured:
            result = mercari.deduplicate_pending(pending, [])
        self.assertEqual(len(result), mercari.MAX_PENDING_ALERTS)
        self.assertIn("대기 알림이 상한", captured.getvalue())


    async def test_relist_check_considers_every_keyword_searched_in_the_run(self):
        # 같은 판매자가 제목이 똑같은 상품을 두 개 올렸는데, 카테고리 필터 때문에
        # 키워드마다 한쪽씩만 잡히는 경우입니다. 한 키워드 결과만 보면 "예전 매물이
        # 사라졌다"고 착각해 진짜 새 매물 알림을 삼켜 버립니다.
        mercari.SEARCHES = [
            {"query": "kw-a", "categories": [1]},
            {"query": "kw-b", "categories": [2]},
        ]
        checkpoints = {"kw-a": datetime.now().timestamp() - 300, "kw-b": datetime.now().timestamp() - 300}
        mercari.save_state({}, [], [], {}, {"kw-a", "kw-b"}, checkpoints)

        first = FakeItem("m1", "완전히 같은 제목", 5000, seller_id="seller-A")
        second = FakeItem("m2", "완전히 같은 제목", 5000, seller_id="seller-A")

        base = datetime.now().timestamp()

        # 1회차(전체 조회): kw-a에서 m1만 보임
        with patch.object(mercari, "Mercapi", return_value=FakeMercapi({"kw-a": [first], "kw-b": []})), \
             patch.object(mercari, "current_time", return_value=base):
            await mercari.collect_updates()

        # 2회차(빠른 조회, 1분 뒤): m2가 재출품인지 별개 매물인지 가릴 근거가 없으므로
        # 상태를 건드리지 않고 판정을 미룹니다.
        with patch.object(
            mercari, "Mercapi", return_value=FakeMercapi({"kw-a": [first], "kw-b": [second]})
        ), patch.object(mercari, "current_time", return_value=base + 60):
            await mercari.collect_updates()

        state = json.loads(mercari.SEEN_FILE.read_text())
        self.assertEqual(state["pending"], [])
        self.assertNotIn("m2", state["seen"])  # 다음 전체 조회에 맡김

        # 3회차(전체 조회, 6분 뒤): 두 매물이 동시에 살아 있음이 확인되므로 새 매물로 알림
        with patch.object(
            mercari, "Mercapi", return_value=FakeMercapi({"kw-a": [first], "kw-b": [second]})
        ), patch.object(mercari, "current_time", return_value=base + 360):
            await mercari.collect_updates()

        state = json.loads(mercari.SEEN_FILE.read_text())
        self.assertIn("new:m2", [entry["alert_id"] for entry in state["pending"]])


    async def test_checkpoint_waits_when_only_the_new_item_search_fails(self):
        # 새 매물을 책임지는 건 '등록순' 조회입니다. 등록순이 실패했는데 추천순만
        # 성공했다고 조회 시각을 갱신하면, 그 구간에 올라온 매물이 다음 실행에서
        # '오래된 매물'로 분류돼 영영 알림이 오지 않습니다.
        mercari.SEARCHES = [{"query": "test", "categories": []}]
        mercari.save_state({"old-item": 12000}, [], [], {}, {"test"}, {"test": 1000.0})
        api = FakeMercapi(
            {"test": [FakeItem("old-item", "가격 인하", 9000)]},
            fail_call_indexes={"test": {0}},  # 등록순만 실패
        )

        with patch.object(mercari, "Mercapi", return_value=api):
            await mercari.collect_updates()

        state = json.loads(mercari.SEEN_FILE.read_text())
        # 조회 시각은 예전 값 그대로 -> 다음 실행에서 그 구간을 다시 훑습니다.
        self.assertEqual(keyword_checkpoints(state), {"test": 1000.0})
        # 추천순으로 본 결과의 가격 인하 알림은 그대로 나갑니다.
        self.assertEqual(
            [entry["alert_id"] for entry in state["pending"]], ["drop:old-item:12000:9000"]
        )

    async def test_checkpoint_advances_when_the_new_item_search_succeeds(self):
        mercari.SEARCHES = [{"query": "test", "categories": []}]
        mercari.save_state({"x": 1}, [], [], {}, {"test"}, {"test": 1000.0})
        api = FakeMercapi(
            {"test": [FakeItem("m1", "item", 1000)]},
            fail_call_indexes={"test": {1}},  # 추천순만 실패해도 기준선은 전진
        )

        with patch.object(mercari, "Mercapi", return_value=api):
            await mercari.collect_updates()

        state = json.loads(mercari.SEEN_FILE.read_text())
        self.assertGreater(state["keyword_checked_at"]["test"], 1000.0)

    async def test_missing_created_field_is_reported_loudly(self):
        # created가 비어 있으면 방어선 하나가 조용히 사라집니다. 로그로 드러나야 합니다.
        mercari.SEARCHES = [{"query": "test", "categories": []}]
        mercari.save_state({"x": 1}, [], [], {}, {"test"}, {"test": 1000.0})
        api = FakeMercapi({"test": [FakeItem("m1", "created 없음", 1000, created=None)]})

        with patch.object(mercari, "Mercapi", return_value=api), patch(
            "sys.stderr", new=io.StringIO()
        ) as captured:
            await mercari.collect_updates()

        self.assertIn("등록 시각(created)이 하나도", captured.getvalue())

    async def test_feed_health_is_quiet_when_created_is_present(self):
        mercari.SEARCHES = [{"query": "test", "categories": []}]
        mercari.save_state({"x": 1}, [], [], {}, {"test"}, {"test": 1000.0})
        api = FakeMercapi({"test": [FakeItem("m1", "정상", 1000, created=datetime.now())]})

        with patch.object(mercari, "Mercapi", return_value=api), patch(
            "sys.stderr", new=io.StringIO()
        ) as captured:
            await mercari.collect_updates()

        self.assertNotIn("[경고]", captured.getvalue())


    async def test_quick_scan_only_runs_the_new_item_search(self):
        # 1분처럼 짧은 주기에서는 등록순만 봅니다(실행 시간과 API 호출량 절반).
        mercari.SEARCHES = [{"query": "test", "categories": []}]
        base = datetime.now().timestamp()
        mercari.save_state(
            {"x": 1}, [], [], {}, {"test"},
            {"test": base - 60, mercari.FULL_SCAN_STATE_KEY: base - 60},
        )
        api = FakeMercapi({"test": [FakeItem("m1", "item", 1000)]})

        with patch.object(mercari, "Mercapi", return_value=api), patch.object(
            mercari, "current_time", return_value=base
        ):
            await mercari.collect_updates()

        self.assertEqual(len(api.calls), 1)  # 등록순 한 번만

    async def test_full_scan_runs_after_the_interval_and_records_its_time(self):
        mercari.SEARCHES = [{"query": "test", "categories": []}]
        base = datetime.now().timestamp()
        stale = base - mercari.FULL_SCAN_INTERVAL_SECONDS - 1
        mercari.save_state(
            {"x": 1}, [], [], {}, {"test"},
            {"test": base - 60, mercari.FULL_SCAN_STATE_KEY: stale},
        )
        api = FakeMercapi({"test": [FakeItem("m1", "item", 1000)]})

        with patch.object(mercari, "Mercapi", return_value=api), patch.object(
            mercari, "current_time", return_value=base
        ):
            await mercari.collect_updates()

        self.assertEqual(len(api.calls), 2)  # 등록순 + 추천순
        state = json.loads(mercari.SEEN_FILE.read_text())
        self.assertEqual(state["keyword_checked_at"][mercari.FULL_SCAN_STATE_KEY], base)

    async def test_full_scan_marker_is_not_mistaken_for_a_keyword(self):
        # 예약 키가 키워드처럼 취급되면 엉뚱한 기준선이 생깁니다.
        cutoff = mercari.new_item_cutoff(
            {mercari.FULL_SCAN_STATE_KEY: 0}, mercari.FULL_SCAN_STATE_KEY, 10_000.0
        )
        self.assertEqual(cutoff, 10_000.0 - mercari.FIRST_RUN_LOOKBACK_SECONDS)

    async def test_quick_scan_still_alerts_on_brand_new_listings(self):
        # 빠른 조회에서도 새 매물 알림은 정상적으로 와야 합니다(이게 핵심 목적).
        mercari.SEARCHES = [{"query": "test", "categories": []}]
        base = datetime.now().timestamp()
        mercari.save_state(
            {"x": 1}, [], [], {}, {"test"},
            {"test": base - 60, mercari.FULL_SCAN_STATE_KEY: base - 60},
        )
        fresh = FakeItem("m-new", "방금 올라옴", 30000, created=datetime.now())
        api = FakeMercapi({"test": [fresh]})

        with patch.object(mercari, "Mercapi", return_value=api), patch.object(
            mercari, "current_time", return_value=base
        ):
            await mercari.collect_updates()

        state = json.loads(mercari.SEEN_FILE.read_text())
        self.assertEqual([e["alert_id"] for e in state["pending"]], ["new:m-new"])


    async def test_zero_interval_makes_every_run_a_full_scan(self):
        # 실행 주기를 늘렸을 때를 위한 설정입니다. 이 값을 0으로 두면 매 실행이
        # 전체 조회가 되어, 빠른/전체 분리를 넣기 전과 똑같이 동작합니다.
        mercari.SEARCHES = [{"query": "test", "categories": []}]
        base = datetime.now().timestamp()
        mercari.save_state(
            {"x": 1}, [], [], {}, {"test"},
            {"test": base - 60, mercari.FULL_SCAN_STATE_KEY: base},  # 방금 전체 조회함
        )
        api = FakeMercapi({"test": [FakeItem("m1", "item", 1000)]})

        with patch.object(mercari, "FULL_SCAN_INTERVAL_SECONDS", 0), patch.object(
            mercari, "Mercapi", return_value=api
        ), patch.object(mercari, "current_time", return_value=base):
            await mercari.collect_updates()

        self.assertEqual(len(api.calls), 2)  # 간격이 0이면 언제나 등록순+추천순


    async def test_no_health_alert_for_a_single_failed_run(self):
        # 일시적인 네트워크 오류 한 번으로 헛알림이 가면 안 됩니다.
        base = datetime.now().timestamp()
        checked_at = {mercari.LAST_SEARCH_OK_KEY: base - 60}
        searched = [("kw", [], False, False, 0.0)]
        self.assertEqual(mercari.health_alerts(searched, checked_at, base), [])

    async def test_health_alert_after_searches_fail_for_a_while(self):
        base = datetime.now().timestamp()
        down_for = mercari.HEALTH_ALERT_AFTER_SECONDS + 120
        checked_at = {mercari.LAST_SEARCH_OK_KEY: base - down_for}
        searched = [("kw", [], False, False, 0.0)]

        alerts = mercari.health_alerts(searched, checked_at, base)
        self.assertEqual(len(alerts), 1)
        self.assertTrue(alerts[0]["alert_id"].startswith("health:search-down:"))
        self.assertIn("이상", alerts[0]["caption"])
        # 고장이 이어져도 마지막 성공 시각은 갱신하지 않아야 경과 시간이 계속 늘어납니다.
        self.assertEqual(checked_at[mercari.LAST_SEARCH_OK_KEY], base - down_for)

    async def test_repeated_failures_reuse_one_alert_id_within_the_cooldown(self):
        # 고장이 계속돼도 1분마다 알림이 쏟아지면 안 됩니다.
        # base와 base+300초는 벽시계 구간 경계를 사이에 두고 있습니다. 예전처럼 절대
        # 시각으로 구간을 나누면 여기서 id가 갈라집니다(2026-09-12 사고).
        base = just_before_a_wall_clock_boundary()
        checked_at = {mercari.LAST_SEARCH_OK_KEY: base - mercari.HEALTH_ALERT_AFTER_SECONDS - 60}
        searched = [("kw", [], False, False, 0.0)]

        first = mercari.health_alerts(searched, dict(checked_at), base)[0]["alert_id"]
        later = mercari.health_alerts(searched, dict(checked_at), base + 300)[0]["alert_id"]
        self.assertEqual(first, later)  # 같은 id -> sent_alerts가 재전송을 막습니다

        after_cooldown = mercari.health_alerts(
            searched, dict(checked_at), base + mercari.HEALTH_ALERT_COOLDOWN_SECONDS
        )[0]["alert_id"]
        self.assertNotEqual(first, after_cooldown)  # 쿨다운이 지나면 다시 알립니다

    async def test_a_long_outage_alerts_exactly_once_per_cooldown(self):
        """고장이 이어지는 동안 알림은 '고장 시작 시점 기준'으로 쿨다운마다 한 번입니다.

        예전에는 벽시계 절대 시각으로 구간을 나눠서, 경계를 넘는 순간 고장이 이어지는
        중인데도 한 건이 더 나갔습니다(최악의 경우 1분 간격으로 두 건).
        """
        searched = [("kw", [], False, False, 0.0)]
        start = just_before_a_wall_clock_boundary() - mercari.HEALTH_ALERT_AFTER_SECONDS
        cooldown = mercari.HEALTH_ALERT_COOLDOWN_SECONDS

        delivered = []
        for minute in range(24 * 60 + 1):  # 24시간을 1분 간격으로 재현
            for alert in mercari.health_alerts(
                searched, {mercari.LAST_SEARCH_OK_KEY: start}, start + minute * 60
            ):
                if alert["alert_id"] not in delivered:  # sent_alerts가 하는 일과 동일
                    delivered.append(alert["alert_id"])

        # 10분째 첫 알림, 이후 6시간마다 한 번 -> 24시간에 5건.
        self.assertEqual(len(delivered), 5)
        self.assertEqual(len(set(delivered)), 5)
        self.assertEqual(
            delivered[0], f"health:search-down:{int(start)}:0"
        )
        self.assertEqual(delivered[-1], f"health:search-down:{int(start)}:{24 * 3600 // cooldown}")

    async def test_recovery_alert_after_an_outage(self):
        base = datetime.now().timestamp()
        down_since = base - mercari.HEALTH_ALERT_AFTER_SECONDS - 60
        checked_at = {mercari.LAST_SEARCH_OK_KEY: down_since}
        searched = [("kw", [], True, True, 0.0)]
        # 고장을 실제로 알린 뒤여야 복구를 알립니다(아래 ..._is_silent 테스트 참고).
        announced = [f"health:search-down:{int(down_since)}:0"]

        alerts = mercari.health_alerts(searched, checked_at, base, announced)
        self.assertEqual(len(alerts), 1)
        self.assertIn("복구", alerts[0]["caption"])
        self.assertEqual(checked_at[mercari.LAST_SEARCH_OK_KEY], base)

    async def test_recovery_is_silent_when_the_outage_was_never_announced(self):
        """봇이 그냥 안 돌았을 때 "나았습니다"라고 말하지 않습니다.

        복구 조건인 "마지막 정상으로부터 10분"은 고장났을 때뿐 아니라 **실행 자체가
        없었을 때**도 참입니다. 실행이 없으면 경고를 보낼 주체도 없으므로, 막지 않으면
        경고 없이 복구만 나갑니다. 실측(2026-09-13) 복구 11건이 전부 그랬습니다.
        """
        base = datetime.now().timestamp()
        checked_at = {mercari.LAST_SEARCH_OK_KEY: base - mercari.HEALTH_ALERT_AFTER_SECONDS - 60}
        searched = [("kw", [], True, True, 0.0)]

        self.assertEqual(mercari.health_alerts(searched, checked_at, base, []), [])
        # 알리지 않아도 기준선은 전진해야 합니다(다음 실행이 또 10분을 세면 안 됩니다).
        self.assertEqual(checked_at[mercari.LAST_SEARCH_OK_KEY], base)

    async def test_a_recovery_matches_its_outage_by_the_same_baseline(self):
        """경고와 복구는 같은 기준 시각을 공유합니다. 다른 고장의 경고로는 안 열립니다."""
        base = datetime.now().timestamp()
        down_since = base - mercari.HEALTH_ALERT_AFTER_SECONDS - 60
        searched = [("kw", [], True, True, 0.0)]
        other = [f"health:search-down:{int(down_since) - 9999}:0"]

        self.assertEqual(
            mercari.health_alerts(searched, {mercari.LAST_SEARCH_OK_KEY: down_since}, base, other),
            [],
        )
        # 고장이 길어져 쿨다운 구간이 넘어간 뒤(bucket 1)에도 복구는 열려야 합니다.
        late = [f"health:search-down:{int(down_since)}:1"]
        self.assertEqual(
            len(mercari.health_alerts(
                searched, {mercari.LAST_SEARCH_OK_KEY: down_since}, base, late)),
            1,
        )

    async def test_no_recovery_alert_during_normal_operation(self):
        base = datetime.now().timestamp()
        checked_at = {mercari.LAST_SEARCH_OK_KEY: base - 60}
        searched = [("kw", [], True, True, 0.0)]
        self.assertEqual(mercari.health_alerts(searched, checked_at, base), [])
        self.assertEqual(checked_at[mercari.LAST_SEARCH_OK_KEY], base)

    async def test_health_alert_reaches_the_queue_even_on_the_first_run(self):
        # 첫 실행은 매물 알림을 생략하지만, 봇 고장은 그때도 알려야 합니다.
        mercari.SEARCHES = [{"query": "broken", "categories": []}]
        base = datetime.now().timestamp()
        mercari.save_state(
            {}, [], [], {}, set(),
            {mercari.LAST_SEARCH_OK_KEY: base - mercari.HEALTH_ALERT_AFTER_SECONDS - 120},
        )
        api = FakeMercapi({"broken": []}, fail_keywords=["broken"])

        with patch.object(mercari, "Mercapi", return_value=api), patch.object(
            mercari, "current_time", return_value=base
        ):
            await mercari.collect_updates()

        state = json.loads(mercari.SEEN_FILE.read_text())
        ids = [e["alert_id"] for e in state["pending"]]
        self.assertTrue(any(i.startswith("health:search-down:") for i in ids), ids)

    async def test_reserved_state_keys_are_never_treated_as_keywords(self):
        for key in mercari.RESERVED_STATE_KEYS:
            cutoff = mercari.new_item_cutoff({key: 0}, key, 10_000.0)
            self.assertEqual(cutoff, 10_000.0 - mercari.FIRST_RUN_LOOKBACK_SECONDS, key)


    def test_unknown_seller_fingerprints_are_pruned(self):
        # 숍스 상품의 sellerId가 0으로 내려오던 시절 만들어진 지문입니다.
        # 지금은 같은 매물을 'title:' 지문으로 다루므로 영영 조회되지 않습니다.
        # 남겨 두면 용량 상한만 차지하므로 정리합니다.
        self.assertFalse(mercari.is_usable_fingerprint("seller:0:어떤제목"))
        self.assertFalse(mercari.is_usable_fingerprint("seller::어떤제목"))
        self.assertFalse(mercari.is_usable_fingerprint("seller-photo:1:제목:url"))
        self.assertTrue(mercari.is_usable_fingerprint("seller:123456:어떤제목"))
        self.assertTrue(mercari.is_usable_fingerprint("title:어떤제목:5000"))

    def test_load_state_drops_unknown_seller_fingerprints(self):
        mercari.SEEN_FILE.write_text(
            json.dumps(
                {
                    "seen": {},
                    "relist_fingerprints": {
                        "seller:0:shop": {"item_id": "s1"},
                        "seller:777:real": {"item_id": "m1"},
                        "title:shop:5000": {"item_id": "s2"},
                    },
                }
            )
        )
        _, _, _, fingerprints, _, _ = mercari.load_state()
        self.assertEqual(sorted(fingerprints), ["seller:777:real", "title:shop:5000"])


    async def test_stuck_keyword_is_reported_while_others_are_fine(self):
        # 전량 실패는 health_alerts가 잡습니다. 여기서 잡는 건 "다른 키워드는 멀쩡한데
        # 이 키워드만 계속 실패"하는 상황 — 그대로 두면 그 키워드 알림만 조용히 멈춥니다.
        base = datetime.now().timestamp()
        stuck = base - mercari.KEYWORD_STUCK_AFTER_SECONDS - 120
        previous = {"ok-keyword": base - 60, "broken-keyword": stuck}
        searched = [
            ("ok-keyword", [], True, True, 0.0),
            ("broken-keyword", [], False, False, 0.0),
        ]

        alerts = mercari.keyword_health_alerts(searched, previous, base)
        self.assertEqual(len(alerts), 1)
        self.assertTrue(alerts[0]["alert_id"].startswith("health:keyword-down:broken-keyword:"))
        self.assertIn("broken-keyword", alerts[0]["caption"])

    async def test_briefly_failing_keyword_is_not_reported(self):
        base = datetime.now().timestamp()
        previous = {"kw": base - 120}  # 2분 전에는 성공
        searched = [("kw", [], False, False, 0.0), ("other", [], True, True, 0.0)]
        self.assertEqual(mercari.keyword_health_alerts(searched, previous, base), [])

    async def test_keyword_alerts_are_silent_when_the_whole_bot_is_down(self):
        # 전량 실패한 실행에서 키워드마다 알리면 한 번에 17건이 쏟아집니다.
        # 그 상황은 health_alerts가 한 건으로 알리므로 여기서는 아무것도 내보내지 않습니다.
        base = datetime.now().timestamp()
        stuck = base - mercari.KEYWORD_STUCK_AFTER_SECONDS - 120
        previous = {"a": stuck, "b": stuck}
        searched = [("a", [], False, False, 0.0), ("b", [], False, False, 0.0)]
        self.assertEqual(mercari.keyword_health_alerts(searched, previous, base), [])

    async def test_stuck_keyword_reports_recovery(self):
        base = datetime.now().timestamp()
        stuck = base - mercari.KEYWORD_STUCK_AFTER_SECONDS - 300
        previous = {"kw": stuck}
        searched = [("kw", [], True, True, 0.0)]

        alerts = mercari.keyword_health_alerts(searched, previous, base)
        self.assertEqual(len(alerts), 1)
        self.assertTrue(alerts[0]["alert_id"].startswith("health:keyword-up:kw:"))
        self.assertIn("재개", alerts[0]["caption"])

    async def test_bot_wide_outage_does_not_fire_a_recovery_alert_per_keyword(self):
        """봇 전체가 멈췄다 살아나면 모든 키워드가 동시에 '오래 막혀 있었다'가 됩니다.

        막아 두지 않으면 복구되는 순간 "✅ [키워드] 검색 재개"가 키워드 수만큼
        한꺼번에 쏟아집니다. 같은 소식을 health_alerts가 이미 한 건으로 알립니다.
        """
        base = datetime.now().timestamp()
        outage_started = base - mercari.KEYWORD_STUCK_AFTER_SECONDS - 600
        keywords = [f"kw-{i}" for i in range(18)]
        searched = [(k, [], True, True, 0.0) for k in keywords]
        previous = {k: outage_started for k in keywords}
        previous[mercari.LAST_SEARCH_OK_KEY] = outage_started

        self.assertEqual(mercari.keyword_health_alerts(searched, previous, base), [])

        # 전체 고장이 아니었다면(봇은 계속 돌고 있었다면) 평소대로 그 키워드만 알립니다.
        previous[mercari.LAST_SEARCH_OK_KEY] = base - 60
        alerts = mercari.keyword_health_alerts(searched[:1], previous, base)
        self.assertEqual(len(alerts), 1)
        self.assertIn("재개", alerts[0]["caption"])

    async def test_keyword_without_a_baseline_is_not_judged(self):
        # 처음 추가한 키워드는 기준선이 없으므로 고장으로 오인하면 안 됩니다.
        base = datetime.now().timestamp()
        searched = [("new-kw", [], False, False, 0.0), ("other", [], True, True, 0.0)]
        self.assertEqual(mercari.keyword_health_alerts(searched, {}, base), [])

    async def test_stuck_keyword_alert_is_rate_limited(self):
        # base와 base+600초가 벽시계 구간 경계를 사이에 둡니다(위 헬퍼 설명 참고).
        base = just_before_a_wall_clock_boundary()
        stuck = base - mercari.KEYWORD_STUCK_AFTER_SECONDS - 120
        previous = {"kw": stuck}
        searched = [("kw", [], False, False, 0.0), ("other", [], True, True, 0.0)]

        first = mercari.keyword_health_alerts(searched, previous, base)[0]["alert_id"]
        soon = mercari.keyword_health_alerts(searched, previous, base + 600)[0]["alert_id"]
        self.assertEqual(first, soon)  # 같은 id -> sent_alerts가 재전송을 막습니다
        later = mercari.keyword_health_alerts(
            searched, previous, base + mercari.HEALTH_ALERT_COOLDOWN_SECONDS
        )[0]["alert_id"]
        self.assertNotEqual(first, later)

    async def test_stuck_keyword_alert_reaches_the_queue(self):
        mercari.SEARCHES = [{"query": "ok", "categories": []}, {"query": "broken", "categories": []}]
        base = datetime.now().timestamp()
        mercari.save_state(
            {"x": 1}, [], [], {}, {"ok", "broken"},
            {
                "ok": base - 60,
                "broken": base - mercari.KEYWORD_STUCK_AFTER_SECONDS - 120,
                mercari.LAST_SEARCH_OK_KEY: base - 60,
                mercari.FULL_SCAN_STATE_KEY: base - 60,
            },
        )
        api = FakeMercapi({"ok": [FakeItem("m1", "item", 1000)], "broken": []},
                          fail_keywords=["broken"])

        with patch.object(mercari, "Mercapi", return_value=api), patch.object(
            mercari, "current_time", return_value=base
        ):
            await mercari.collect_updates()

        state = json.loads(mercari.SEEN_FILE.read_text())
        ids = [e["alert_id"] for e in state["pending"]]
        self.assertTrue(any(i.startswith("health:keyword-down:broken:") for i in ids), ids)


    def _items(self, n_with_created, n_without):
        rows = [
            {"id_": f"c{i}", "created": datetime.now()} for i in range(n_with_created)
        ] + [{"id_": f"n{i}"} for i in range(n_without)]
        return [("kw", rows, True, True, 0.0)]

    async def test_no_alert_while_created_is_healthy(self):
        base = datetime.now().timestamp()
        checked_at = {mercari.LAST_CREATED_OK_KEY: base - 60}
        self.assertEqual(
            mercari.created_coverage_alerts(self._items(100, 0), checked_at, base), []
        )
        self.assertEqual(checked_at[mercari.LAST_CREATED_OK_KEY], base)

    async def test_single_bad_run_does_not_alert(self):
        # 한 번 어긋났다고 바로 알리면 일시적인 응답 이상에 헛알림이 갑니다.
        base = datetime.now().timestamp()
        checked_at = {mercari.LAST_CREATED_OK_KEY: base - 60}
        self.assertEqual(
            mercari.created_coverage_alerts(self._items(0, 100), checked_at, base), []
        )

    async def test_alert_when_created_stays_missing(self):
        base = datetime.now().timestamp()
        checked_at = {
            mercari.LAST_CREATED_OK_KEY: base - mercari.HEALTH_ALERT_AFTER_SECONDS - 120
        }
        alerts = mercari.created_coverage_alerts(self._items(0, 100), checked_at, base)
        self.assertEqual(len(alerts), 1)
        self.assertTrue(alerts[0]["alert_id"].startswith("health:created-missing:"))
        self.assertIn("0%", alerts[0]["caption"])
        # 이상이 이어지는 동안 마지막 정상 시각을 갱신하면 안 됩니다(경과 시간이 멈춥니다).
        self.assertLess(checked_at[mercari.LAST_CREATED_OK_KEY], base)

    async def test_partial_coverage_above_the_floor_is_tolerated(self):
        # 일부 매물에 등록 시각이 없는 건 정상 변동 범위로 봅니다.
        # 직전까지 정상이었으므로 알림도 복구 알림도 나오면 안 됩니다.
        base = datetime.now().timestamp()
        checked_at = {mercari.LAST_CREATED_OK_KEY: base - 60}
        self.assertEqual(
            mercari.created_coverage_alerts(self._items(80, 20), checked_at, base), []
        )
        # 정상 범위이므로 마지막 정상 시각이 갱신됩니다.
        self.assertEqual(checked_at[mercari.LAST_CREATED_OK_KEY], base)

    async def test_coverage_below_the_floor_is_treated_as_broken(self):
        # 절반 아래로 떨어지면 방어선이 사실상 동작하지 않는 상태로 봅니다.
        base = datetime.now().timestamp()
        checked_at = {
            mercari.LAST_CREATED_OK_KEY: base - mercari.HEALTH_ALERT_AFTER_SECONDS - 120
        }
        alerts = mercari.created_coverage_alerts(self._items(30, 70), checked_at, base)
        self.assertEqual(len(alerts), 1)
        self.assertTrue(alerts[0]["alert_id"].startswith("health:created-missing:"))

    async def test_created_alert_is_rate_limited_and_recovers(self):
        # base와 base+600초가 벽시계 구간 경계를 사이에 둡니다(위 헬퍼 설명 참고).
        base = just_before_a_wall_clock_boundary()
        stale = base - mercari.HEALTH_ALERT_AFTER_SECONDS - 120
        first = mercari.created_coverage_alerts(
            self._items(0, 50), {mercari.LAST_CREATED_OK_KEY: stale}, base
        )[0]["alert_id"]
        soon = mercari.created_coverage_alerts(
            self._items(0, 50), {mercari.LAST_CREATED_OK_KEY: stale}, base + 600
        )[0]["alert_id"]
        self.assertEqual(first, soon)

        # 위에서 실제로 만들어진 경고 id를 그대로 '보낸 것'으로 넘깁니다.
        checked_at = {mercari.LAST_CREATED_OK_KEY: stale}
        recovery = mercari.created_coverage_alerts(
            self._items(50, 0), checked_at, base, [first]
        )
        self.assertEqual(len(recovery), 1)
        self.assertIn("복구", recovery[0]["caption"])
        self.assertEqual(checked_at[mercari.LAST_CREATED_OK_KEY], base)

    async def test_the_created_recovery_is_silent_when_nothing_was_announced(self):
        base = datetime.now().timestamp()
        stale = base - mercari.HEALTH_ALERT_AFTER_SECONDS - 120
        checked_at = {mercari.LAST_CREATED_OK_KEY: stale}

        self.assertEqual(
            mercari.created_coverage_alerts(self._items(50, 0), checked_at, base, []), []
        )
        self.assertEqual(checked_at[mercari.LAST_CREATED_OK_KEY], base)

    async def test_failed_search_does_not_trigger_a_created_alert(self):
        # 검색 자체가 실패해 표본이 없는 실행은 판단하지 않습니다(다른 알림이 다룹니다).
        base = datetime.now().timestamp()
        checked_at = {
            mercari.LAST_CREATED_OK_KEY: base - mercari.HEALTH_ALERT_AFTER_SECONDS - 120
        }
        searched = [("kw", [], False, False, 0.0)]
        self.assertEqual(mercari.created_coverage_alerts(searched, checked_at, base), [])

    async def test_created_alert_reaches_the_queue(self):
        mercari.SEARCHES = [{"query": "test", "categories": []}]
        base = datetime.now().timestamp()
        mercari.save_state(
            {"x": 1}, [], [], {}, {"test"},
            {
                "test": base - 60,
                mercari.LAST_SEARCH_OK_KEY: base - 60,
                mercari.FULL_SCAN_STATE_KEY: base - 60,
                mercari.LAST_CREATED_OK_KEY: base - mercari.HEALTH_ALERT_AFTER_SECONDS - 120,
            },
        )
        api = FakeMercapi({"test": [FakeItem("m1", "등록시각 없음", 1000, created=None)]})

        with patch.object(mercari, "Mercapi", return_value=api), patch.object(
            mercari, "current_time", return_value=base
        ), patch("sys.stderr", new=io.StringIO()):
            await mercari.collect_updates()

        state = json.loads(mercari.SEEN_FILE.read_text())
        ids = [e["alert_id"] for e in state["pending"]]
        self.assertTrue(any(i.startswith("health:created-missing:") for i in ids), ids)

    # ── 2026-09-13 사고 재현 ────────────────────────────────────────────────
    #
    # 러너 배정이 막혀 봇이 15분간 돌지 못한 뒤 살아난 실행입니다. 검색·등록시각·매물
    # 모두 정상인데, 세 기준선이 전부 15분 전이라 복구 조건("10분 지남")이 참이 됩니다.
    # 고장 알림은 하나도 나간 적이 없습니다 - 보낼 실행 자체가 없었으니까요.

    async def _run_after_a_15_minute_standstill(self, announce=lambda _baseline: []):
        """15분 멈춰 있다가 살아난 실행 하나를 돌리고, 대기열에 쌓인 alert_id를 돌려줍니다.

        announce는 기준선(멈추기 직전 마지막 정상 시각)을 받아 '이미 보낸 알림' 목록을
        만듭니다. 기준선은 여기서 정해지므로 호출자가 미리 알 수 없어 콜백으로 받습니다.
        """
        mercari.SEARCHES = [{"query": "test", "categories": []}]
        base = datetime.now().timestamp()
        stood_still_since = base - 15 * 60
        mercari.save_state(
            {"x": 1}, [], list(announce(int(stood_still_since))), {}, {"test"},
            {
                "test": stood_still_since,
                mercari.LAST_SEARCH_OK_KEY: stood_still_since,
                mercari.FULL_SCAN_STATE_KEY: stood_still_since,
                mercari.LAST_CREATED_OK_KEY: stood_still_since,
                mercari.LAST_ITEMS_OK_KEY: stood_still_since,
                mercari.LAST_CADENCE_OK_KEY: stood_still_since,
            },
        )
        api = FakeMercapi({"test": [FakeItem("m1", "정상 매물", 1000)]})
        with patch.object(mercari, "Mercapi", return_value=api), patch.object(
            mercari, "current_time", return_value=base
        ), patch("sys.stderr", new=io.StringIO()):
            await mercari.collect_updates()
        state = json.loads(mercari.SEEN_FILE.read_text())
        return [e["alert_id"] for e in state["pending"]]

    async def test_a_standstill_does_not_produce_recoveries_nobody_asked_about(self):
        """실측된 헛알림입니다.

        2026-09-13까지 나간 복구 11건(recovered 4 / created-ok 4 / feed-ok 3)이 전부
        이 경우였고, 같은 기간 짝이 되는 경고는 0건이었습니다. 러너 정체가 하루 두어 번
        있어서 한 번에 세 건씩 나갔습니다.
        """
        ids = await self._run_after_a_15_minute_standstill()

        for bogus in ("health:recovered:", "health:created-ok:", "health:feed-ok:"):
            self.assertFalse([i for i in ids if i.startswith(bogus)], f"{bogus} -> {ids}")

        # 실행이 멈춘 사실 자체는 여전히 알려야 합니다. 그건 cadence가 맡습니다.
        self.assertTrue([i for i in ids if i.startswith("health:cadence-slow:")], ids)

    async def test_a_real_outage_still_gets_its_recovery_announced(self):
        """호출부가 sent_alerts를 실제로 넘기는지까지 확인합니다.

        기본값 ()에 기대고 있으면 이 테스트가 깨집니다 - 넘기지 않으면 '알린 적 없음'이
        되어 복구가 영영 나가지 않기 때문입니다. 위 테스트만으로는 그 구분이 안 됩니다.
        """
        ids = await self._run_after_a_15_minute_standstill(
            announce=lambda baseline: [f"health:search-down:{baseline}:0"]
        )
        self.assertTrue([i for i in ids if i.startswith("health:recovered:")], ids)
        # 짝이 없는 나머지 둘은 여전히 조용해야 합니다.
        self.assertFalse([i for i in ids if i.startswith("health:created-ok:")], ids)
        self.assertFalse([i for i in ids if i.startswith("health:feed-ok:")], ids)


if __name__ == "__main__":
    unittest.main()
