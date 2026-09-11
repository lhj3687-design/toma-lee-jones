"""
메루카리(mercari.jp) 키워드 알림 봇

GitHub Actions는 아래 순서로 이 파일을 두 번 실행합니다.
1. collect: 새 매물/가격 인하를 찾아 상태와 대기열을 먼저 저장합니다.
2. send: 이미 원격 저장소에 저장된 대기열만 텔레그램으로 전송합니다.

이 순서 덕분에 텔레그램 전송이 오래 걸리거나 실행이 중간에 겹쳐도,
같은 매물이나 같은 가격 인하가 다시 새 알림으로 등록되는 일을 막습니다.

'신규' 판정은 상태 파일(seen)뿐 아니라 매물의 실제 등록 시각(created)을 함께 봅니다.
상태 파일은 용량 제한 때문에 오래된 항목을 버릴 수밖에 없는데, 등록 시각을 같이 보면
버려진 오래된 매물이 다시 '신규'로 둔갑해 알림이 가는 일을 원천적으로 막을 수 있습니다.
"""

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import unicodedata
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import httpx
from mercapi import Mercapi

# 일반 메루카리 개인 매물 ID는 항상 "m" + 숫자 형식입니다 (예: m90925725213).
# 이 형식이 아니면 메루카리 숍스(기업 판매자) 상품이므로 /shops/product/ 링크를 써야 합니다.
# item_type 필드만으로는 라이브러리가 숍스 여부를 늘 정확히 채워주지 않아 ID 형식을 보조 판단 기준으로 씁니다.
MERCARI_ITEM_ID_PATTERN = re.compile(r"^m\d+$")

# 제목 정규화 시 제거할 기호(전각/반각 공백, 대괄호·괄호·특수문자 등 꾸밈 차이 무시용)
_TITLE_NOISE_PATTERN = re.compile(r"[\s　!-/:-@\[-`{-~、。・【】「」『』［］（）]+")

# 메루카리 검색 API는 숍스(기업 판매자) 상품의 sellerId를 0으로 돌려줍니다.
# 이 값을 진짜 판매자 ID로 쓰면 서로 다른 숍스 상품들이 "같은 판매자"로 묶여서
# 제목만 같으면 재출품으로 오인되고, 진짜 새 매물 알림이 조용히 삼켜집니다.
UNKNOWN_SELLER_IDS = {"", "0", "none", "null"}

SEEN_FILE = Path("seen_items.json")
MAX_ITEMS_PER_KEYWORD = 120  # 키워드당 정렬 방식마다 확인할 매물 개수
MAX_SEEN_ITEMS = 15000
MAX_RELIST_FINGERPRINTS = 6000
PRICE_DROP_ALERT_THRESHOLD = 1000  # 마지막 알림 가격보다 이 금액(엔) 이상 떨어졌을 때만 알립니다.
MAX_PENDING_ALERTS = 500
MAX_SENT_ALERTS = 8000

# 등록 시각 기준으로 '신규'를 판정할 때 쓰는 여유값들(초 단위).
# GRACE: 크론 지연·실행 큐 대기 때문에 직전 조회 시각이 조금 밀릴 수 있어 그만큼 넉넉히 봅니다.
# FIRST_LOOKBACK: 해당 키워드의 직전 조회 기록이 아직 없을 때(업그레이드 직후 첫 실행) 쓰는 기본 창.
# MAX_LOOKBACK: 봇이 오래 멈춰 있다 살아났을 때 며칠치가 한꺼번에 쏟아지지 않도록 상한을 둡니다.
NEW_ITEM_GRACE_SECONDS = 15 * 60
FIRST_RUN_LOOKBACK_SECONDS = 60 * 60
MAX_LOOKBACK_SECONDS = 24 * 60 * 60

SEARCHES = [
    {"query": "Carol Christian Poell", "categories": []},
    {"query": "Martin Margiela", "categories": [30]},
    {"query": "マルタンマルジェラ", "categories": [30]},
    {"query": "Margiela", "categories": [2, 1]},
    {"query": "マルジェラ", "categories": [2, 1]},
    {"query": "Hermes Margiela", "categories": []},
    {"query": "Hermes", "categories": [30, 11, 12, 13]},
    {"query": "エルメス", "categories": [30, 11, 12, 13]},
    {"query": "Chrome Hearts", "categories": [30, 31, 32, 11]},
    {"query": "クロムハーツ", "categories": [30, 31, 32, 11]},
    {"query": "Richard Avedon", "categories": [3088]},
    {"query": "The Row", "categories": [2]},
    {"query": "ザ・ロウ", "categories": [2]},
    {"query": "Gunter Wermekes", "categories": []},
    {"query": "Label Under Construction", "categories": []},
    {"query": "Yves Saint Laurent Rive Gauche", "categories": [2]},
    {"query": "YSL Rive Gauche", "categories": [2]},
]

# 이 기능(키워드별 '첫 조회' 추적)을 배포하기 전부터 이미 돌리고 있던 키워드 목록입니다.
# known_keywords가 상태 파일에 아직 없을 때(첫 업그레이드 실행)만 이 목록을 '이미 알려짐'으로
# 간주해서, 오래전부터 쓰던 키워드까지 신규로 오인해 알림을 생략해버리는 일을 막습니다.
# 이후 새 키워드를 추가할 때는 이 목록을 더 손댈 필요가 없습니다(실행 한 번이면 자동으로 등록됨).
LEGACY_KEYWORDS_SEEDED_AT_UPGRADE = [
    "Carol Christian Poell",
    "Martin Margiela",
    "マルタンマルジェラ",
    "Margiela",
    "マルジェラ",
    "Hermes Margiela",
    "Hermes",
    "エルメス",
    "Chrome Hearts",
    "クロムハーツ",
    "Richard Avedon",
    "The Row",
    "ザ・ロウ",
]

# 현재 코드가 만들어 내는 재출품 지문 접두사입니다.
# 예전(또는 외부 도구가 잠깐 끼어들어 만든) 형식의 지문은 지금 코드가 조회하지 않으면서
# 용량 상한만 잡아먹기 때문에, 상태를 읽을 때 한 번 정리합니다.
SUPPORTED_FINGERPRINT_PREFIXES = ("seller:", "title:")


def build_search_options() -> dict:
    """정렬 방식별 검색 옵션을 준비합니다.

    - created: 등록 시각 내림차순. 새 매물을 놓치지 않기 위한 본 패스입니다.
    - score: 메루카리 기본값(추천순). 오래된 매물의 가격 인하를 계속 추적하기 위해 함께 봅니다.
    두 패스 모두 '판매중'만 조회해서, 이미 팔린 매물이 자리를 차지하지 않게 합니다.

    mercapi가 없거나(테스트) 옵션 이름이 바뀌어도 봇이 죽지 않도록 실패 시 기본 검색으로 물러납니다.
    """
    try:
        from mercapi.requests import SearchRequestData as request_data

        on_sale = [request_data.Status.STATUS_ON_SALE]
        return {
            "created": {
                "sort_by": request_data.SortBy.SORT_CREATED_TIME,
                "sort_order": request_data.SortOrder.ORDER_DESC,
                "status": on_sale,
            },
            "score": {
                "sort_by": request_data.SortBy.SORT_SCORE,
                "sort_order": request_data.SortOrder.ORDER_DESC,
                "status": on_sale,
            },
        }
    except Exception as exc:  # pragma: no cover - 라이브러리 구조가 바뀐 예외 상황
        print(f"[검색 정렬 옵션 준비 실패 -> 기본 검색으로 진행] {exc}", file=sys.stderr)
        return {"created": {}, "score": {}}


SEARCH_SORT_OPTIONS = build_search_options()


def load_state() -> tuple[dict, list, list, dict, set, dict]:
    empty = ({}, [], [], {}, set(LEGACY_KEYWORDS_SEEDED_AT_UPGRADE), {})
    if not SEEN_FILE.exists():
        return empty
    try:
        data = json.loads(SEEN_FILE.read_text())
    except Exception as exc:
        print(f"[상태 파일 읽기 실패] {exc}", file=sys.stderr)
        return empty

    if isinstance(data, list):
        return ({item_id: None for item_id in data},) + empty[1:]

    raw_seen = data.get("seen", [])
    seen = {item_id: None for item_id in raw_seen} if isinstance(raw_seen, list) else raw_seen
    pending = data.get("pending", [])
    sent_alerts = data.get("sent_alerts", [])
    relist_fingerprints = data.get("relist_fingerprints", {})
    raw_known_keywords = data.get("known_keywords")
    known_keywords = (
        set(raw_known_keywords)
        if isinstance(raw_known_keywords, list)
        else set(LEGACY_KEYWORDS_SEEDED_AT_UPGRADE)
    )
    raw_checked_at = data.get("keyword_checked_at")
    keyword_checked_at = raw_checked_at if isinstance(raw_checked_at, dict) else {}

    return (
        seen if isinstance(seen, dict) else {},
        pending if isinstance(pending, list) else [],
        sent_alerts if isinstance(sent_alerts, list) else [],
        prune_fingerprints(relist_fingerprints if isinstance(relist_fingerprints, dict) else {}),
        known_keywords,
        keyword_checked_at,
    )


def prune_fingerprints(relist_fingerprints: dict) -> dict:
    """현재 코드가 조회하지 않는 형식의 재출품 지문을 걸러냅니다."""
    return {
        key: value
        for key, value in relist_fingerprints.items()
        if str(key).startswith(SUPPORTED_FINGERPRINT_PREFIXES)
    }


def alert_key(entry: dict) -> str:
    value = entry.get("alert_id")
    if value:
        return str(value)
    return f"legacy:{entry.get('caption', '')}"


def unique_recent(values: list[str], limit: int) -> list[str]:
    result = []
    seen_values = set()
    for value in values:
        value = str(value)
        if value and value not in seen_values:
            result.append(value)
            seen_values.add(value)
    return result[-limit:]


def deduplicate_pending(pending: list, sent_alerts: list) -> list:
    sent_keys = set(sent_alerts)
    pending_keys = set()
    result = []
    for entry in pending:
        if not isinstance(entry, dict):
            continue
        key = alert_key(entry)
        if key in sent_keys or key in pending_keys:
            continue
        normalized = dict(entry)
        normalized.setdefault("alert_id", key)
        result.append(normalized)
        pending_keys.add(key)
    return result[-MAX_PENDING_ALERTS:]


def remember(store: dict, key, value) -> None:
    """가장 최근에 본 항목이 항상 dict의 맨 뒤에 오도록 다시 넣습니다.

    파이썬 dict는 이미 있는 키에 값만 바꾸면 원래 자리(처음 넣은 순서)를 그대로 지킵니다.
    상태 파일은 용량 상한 때문에 '뒤에서부터 N개'만 남기므로, 이 처리를 빼먹으면
    매번 검색에 걸리는 '오래 올라와 있는 인기 매물'이 가장 먼저 잘려 나가고,
    다음 조회에서 처음 보는 매물로 오인돼 알림이 갑니다. (실제로 발생했던 버그)
    """
    store.pop(key, None)
    store[key] = value


def save_state(
    seen: dict,
    pending: list,
    sent_alerts: list,
    relist_fingerprints: dict,
    known_keywords: set,
    keyword_checked_at: dict | None = None,
) -> None:
    sent_alerts = unique_recent(sent_alerts, MAX_SENT_ALERTS)
    pending = deduplicate_pending(pending, sent_alerts)
    relist_fingerprints = prune_fingerprints(relist_fingerprints)
    data = {
        "seen": dict(list(seen.items())[-MAX_SEEN_ITEMS:]),
        "pending": pending,
        "sent_alerts": sent_alerts,
        "relist_fingerprints": dict(list(relist_fingerprints.items())[-MAX_RELIST_FINGERPRINTS:]),
        "known_keywords": sorted(known_keywords),
        "keyword_checked_at": keyword_checked_at or {},
    }
    temporary_file = SEEN_FILE.with_suffix(".tmp")
    temporary_file.write_text(json.dumps(data, ensure_ascii=False))
    temporary_file.replace(SEEN_FILE)


def push_state(commit_message: str) -> bool:
    """scripts/push_state.sh를 호출해 현재 상태 파일을 원격 저장소에 즉시 반영합니다.

    텔레그램 알림을 하나 보낼 때마다 이 함수를 호출해서,
    '전송 완료 기록'이 원격에 반영되기 전의 위험 구간을 최소화합니다.
    실패하면 False를 반환하며, 호출한 쪽에서 더 이상의 전송을 멈춰야 합니다.
    """
    try:
        result = subprocess.run(
            ["bash", "scripts/push_state.sh", commit_message],
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        print(f"[상태 저장 실행 실패] {exc}", file=sys.stderr)
        return False

    if result.returncode != 0:
        print(f"[상태 저장 실패]\n{result.stdout}\n{result.stderr}", file=sys.stderr)
        return False
    if result.stdout.strip():
        print(result.stdout.strip())
    return True


def price_record(seen: dict, item_id) -> dict:
    """seen[item_id]를 {'last_alert_price', 'last_seen_price'} 형태로 정규화합니다.

    예전 버전은 seen[item_id]에 정수(또는 None) 하나만 저장했기 때문에,
    그런 값을 만나면 두 필드에 동일하게 채워서 자연스럽게 새 형식으로 넘어가게 합니다.
    """
    value = seen.get(item_id)
    if isinstance(value, dict):
        return {
            "last_alert_price": value.get("last_alert_price"),
            "last_seen_price": value.get("last_seen_price"),
        }
    return {"last_alert_price": value, "last_seen_price": value}


def normalize_title(name) -> str:
    """재출품 판별용으로 제목을 정규화합니다 (공백/기호/전각·반각 차이를 무시)."""
    text = unicodedata.normalize("NFKC", str(name or ""))
    text = _TITLE_NOISE_PATTERN.sub("", text)
    return text.lower()


def extract_seller_id(item) -> str | None:
    """검색 결과에서 판매자 ID를 뽑아냅니다. 라이브러리 버전에 따라 필드 위치가
    다를 수 있어 평평한 필드와 중첩된 seller 객체를 모두 시도합니다.

    숍스 상품처럼 판매자 ID가 0으로 내려오는 경우는 '모름'으로 취급합니다
    (0을 그대로 쓰면 서로 다른 숍스 상품이 같은 판매자로 묶여 버립니다)."""

    def clean(value):
        if value is None:
            return None
        text = str(value).strip()
        return None if text.lower() in UNKNOWN_SELLER_IDS else text

    data = asdict(item)
    direct = clean(data.get("seller_id")) or clean(data.get("sellerId"))
    if direct:
        return direct
    seller = data.get("seller")
    if isinstance(seller, dict):
        nested = clean(seller.get("id")) or clean(seller.get("id_")) or clean(seller.get("seller_id"))
        if nested:
            return nested
    return None


def relist_fingerprint(seller_id, name, price) -> str | None:
    """같은 매물의 재출품(삭제 후 재등록)을 잡아내기 위한 지문을 만듭니다.

    판매자 ID를 알 수 있으면 '판매자+정규화된 제목'만으로 판단하고(가격이 달라도 매칭),
    판매자 ID를 못 가져오면 '정규화된 제목+정확히 같은 가격'으로 대체합니다.
    """
    normalized_title = normalize_title(name)
    if not normalized_title:
        return None
    if seller_id:
        return f"seller:{seller_id}:{normalized_title}"
    if isinstance(price, int):
        return f"title:{normalized_title}:{price}"
    return None


def extract_field(item, candidates, default=None):
    data = asdict(item)
    for key in candidates:
        value = data.get(key)
        if value:
            return value
    return default


def listing_created_at(item) -> float | None:
    """매물의 실제 등록 시각을 epoch 초로 돌려줍니다 (없으면 None)."""
    value = getattr(item, "created", None)
    if isinstance(value, datetime):
        try:
            return value.timestamp()
        except Exception:
            return None
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    return None


def new_item_cutoff(keyword_checked_at: dict, keyword: str, now: float) -> float:
    """이 키워드에서 '신규'로 볼 등록 시각의 하한선을 계산합니다.

    - 직전에 이 키워드를 조회한 시각이 기준선입니다(그 이후 등록된 것만 새 매물).
    - 조회 기록이 없으면(업그레이드 직후 첫 실행) 최근 1시간만 신규로 봅니다.
    - 봇이 오래 멈춰 있었다면 최대 24시간까지만 거슬러 올라갑니다.
    """
    last_checked = keyword_checked_at.get(keyword)
    if not isinstance(last_checked, (int, float)):
        return now - FIRST_RUN_LOOKBACK_SECONDS
    return max(float(last_checked), now - MAX_LOOKBACK_SECONDS)


def is_fresh_listing(created_at: float | None, cutoff: float | None) -> bool:
    """등록 시각 기준으로 '이번에 새로 올라온 매물'인지 판단합니다.

    등록 시각을 알 수 없거나 비교 기준이 없으면, 예전처럼 상태 파일 기준으로만 판단하도록
    True를 돌려줍니다(알림을 놓치는 쪽보다 한 번 더 보내는 쪽이 안전).
    """
    if created_at is None or cutoff is None:
        return True
    return created_at >= cutoff - NEW_ITEM_GRACE_SECONDS


async def send_telegram(caption: str, photo_url):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[텔레그램 설정 누락] TELEGRAM_BOT_TOKEN 또는 TELEGRAM_CHAT_ID", file=sys.stderr)
        return False, None

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            if photo_url:
                response = await client.post(
                    f"https://api.telegram.org/bot{token}/sendPhoto",
                    data={"chat_id": chat_id, "caption": caption, "photo": photo_url},
                )
            else:
                response = await client.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    data={"chat_id": chat_id, "text": caption},
                )
        except Exception as exc:
            print(f"[텔레그램 전송 에러] {exc}", file=sys.stderr)
            return False, None

    if response.status_code == 200:
        return True, None

    retry_after = None
    try:
        retry_after = response.json().get("parameters", {}).get("retry_after")
    except Exception:
        pass
    print(f"[텔레그램 전송 실패 {response.status_code}] {response.text}", file=sys.stderr)
    return False, retry_after


async def flush_pending(
    seen: dict,
    pending: list,
    sent_alerts: list,
    relist_fingerprints: dict,
    known_keywords: set,
    keyword_checked_at: dict | None = None,
) -> tuple[list, list]:
    """대기열을 전송하고, 성공할 때마다 곧바로 원격 저장소에 기록합니다.

    한 건이라도 전송된 뒤 원격 저장 기록(push_state)이 실패하면 즉시 멈춥니다.
    이미 텔레그램으로는 전송됐으므로 sent_alerts에는 남겨 두고(그래야 이후
    save_state 호출에서 결국 반영됨), 그 이상 대기열을 처리하지 않아
    "배치 전체 중복 재전송" 위험을 '방금 보낸 1건'으로 최소화합니다.
    """
    remaining = list(pending)
    sent = 0
    while remaining:
        entry = remaining[0]
        key = alert_key(entry)
        if key in sent_alerts:
            remaining.pop(0)
            continue
        ok, retry_after = await send_telegram(entry["caption"], entry.get("photo"))
        if ok:
            sent_alerts.append(key)
            remaining.pop(0)
            sent += 1
            save_state(seen, remaining, sent_alerts, relist_fingerprints, known_keywords, keyword_checked_at)
            if not push_state("record Mercari alert delivery"):
                print(
                    "[중단] 전송 기록 저장에 실패해 이번 실행은 여기서 멈춥니다 "
                    "(다음 실행 때 안전하게 이어갑니다)",
                    file=sys.stderr,
                )
                break
            await asyncio.sleep(1.5)
            continue
        if retry_after and retry_after <= 60:
            print(f"[레이트리밋] {retry_after}초 대기 후 재시도", file=sys.stderr)
            await asyncio.sleep(retry_after + 1)
            continue
        entry["attempts"] = entry.get("attempts", 0) + 1
        if entry["attempts"] >= 3:
            print(f"[알림 포기: 3회 실패] {entry['caption'][:50]}", file=sys.stderr)
            remaining.pop(0)
            continue
        break
    if sent:
        print(f"텔레그램 알림 {sent}건 전송 완료")
    return remaining, unique_recent(sent_alerts, MAX_SENT_ALERTS)


async def search_items(m: Mercapi, keyword: str, categories: list) -> tuple[list, bool]:
    """한 키워드를 '등록순'과 '추천순' 두 가지로 조회해 매물 목록을 합칩니다.

    메루카리 기본 검색은 추천순(관련도)이라, 갓 올라온 매물이 상위 120개 안에 못 드는 일이
    자주 생깁니다. 그래서 새 매물은 등록순으로 확실히 잡고, 오래 올라와 있는 매물의
    가격 인하는 추천순 결과로 계속 추적합니다.

    두 번째 값은 '조회에 한 번이라도 성공했는지'입니다. 전부 실패했다면 이번 실행에서는
    이 키워드의 조회 시각을 갱신하면 안 됩니다(그 사이 올라온 매물을 영영 놓치게 되므로).
    """
    merged: dict = {}
    succeeded = False
    for index, (pass_name, options) in enumerate(SEARCH_SORT_OPTIONS.items()):
        if index:
            await asyncio.sleep(1)
        try:
            results = await m.search(keyword, categories=categories, **options)
        except Exception as exc:
            print(f"[검색 실패: {keyword} / {pass_name}] {exc}", file=sys.stderr)
            continue
        succeeded = True
        for item in list(getattr(results, "items", []) or [])[:MAX_ITEMS_PER_KEYWORD]:
            item_id = extract_field(item, ["id_", "id", "item_id", "itemId"])
            if item_id and item_id not in merged:
                merged[item_id] = item
    return list(merged.values()), succeeded


async def check_keyword(
    m: Mercapi,
    keyword: str,
    categories: list,
    seen: dict,
    relist_fingerprints: dict,
    new_items: list,
    created_cutoff: float | None = None,
) -> bool:
    items, succeeded = await search_items(m, keyword, categories)
    if not succeeded:
        return False

    new_count = 0
    drop_count = 0
    relist_count = 0
    stale_count = 0
    for item in items:
        item_id = extract_field(item, ["id_", "id", "item_id", "itemId"])
        if not item_id:
            continue
        name = getattr(item, "name", None) or extract_field(item, ["name", "title"], "(제목 없음)")
        price = getattr(item, "price", None)
        if isinstance(price, Decimal):
            price = int(price)
        # 가격 비공개(is_no_price) 매물은 price에 9999999가 들어옵니다. 그대로 두면
        # 말도 안 되는 가격 인하 알림의 기준가가 되므로 '가격 모름'으로 취급합니다.
        if getattr(item, "is_no_price", False):
            price = None
        photo = extract_field(item, ["thumbnails", "photos", "thumbnail", "image_url"])
        if isinstance(photo, (list, tuple)):
            photo = photo[0] if photo else None
        seller_id = extract_seller_id(item)
        fingerprint = relist_fingerprint(seller_id, name, price)

        # item_type에 "SHOP"이 찍히거나, ID가 일반 매물 형식(m+숫자)이 아니면 숍스 상품으로 간주합니다.
        item_type = str(extract_field(item, ["item_type"], "")).upper()
        is_shop_item = "SHOP" in item_type or not MERCARI_ITEM_ID_PATTERN.match(str(item_id))
        item_url = (
            f"https://jp.mercari.com/shops/product/{item_id}"
            if is_shop_item
            else f"https://jp.mercari.com/item/{item_id}"
        )
        price_txt = f"¥{price:,}" if isinstance(price, int) else "가격 확인 필요"

        if item_id in seen:
            record = price_record(seen, item_id)
        else:
            # 처음 보는 ID지만 지문이 이미 있다면 두 가지 경우입니다.
            # 1) 같은 ID의 지문이 남아 있음 -> 예전에 확인했는데 seen 용량 상한 때문에 밀려난 매물.
            # 2) 다른 ID의 지문 -> 같은 판매자(또는 같은 제목+가격)가 삭제 후 재등록한 매물.
            # 어느 쪽이든 새 매물이 아니므로, 예전 가격 이력을 이어받고 '신규' 알림을 보내지 않습니다.
            matched = relist_fingerprints.get(fingerprint) if fingerprint else None
            if matched:
                record = {
                    "last_alert_price": matched.get("last_alert_price"),
                    "last_seen_price": matched.get("last_seen_price"),
                }
                if matched.get("item_id") != item_id:
                    relist_count += 1
            else:
                record = None

        if record is None:
            remember(seen, item_id, {"last_alert_price": price, "last_seen_price": price})
            if is_fresh_listing(listing_created_at(item), created_cutoff):
                caption = f"[{keyword}] {name}\n💴 {price_txt}\n🔗 {item_url}"
                new_items.append({"alert_id": f"new:{item_id}", "caption": caption, "photo": photo})
                new_count += 1
            else:
                # 등록 시각이 기준선보다 한참 예전인 매물입니다. 상태 파일에서 밀려났거나
                # 검색 정렬이 흔들려 이제야 눈에 띈 것뿐이므로, 기준선만 저장하고 넘어갑니다.
                stale_count += 1
        else:
            # last_alert_price: 실제로 알림을 보낸 적 있는 '역대 최저가' 기준입니다.
            #   가격이 올랐다가 다시 내려와도 이 값보다 위에 머무는 한 알림을 보내지 않고,
            #   기준가를 절대 위로 올리지 않습니다 (그래야 잦은 가격 변동에도 기준이 안 꼬입니다).
            # last_seen_price: 참고용으로 저장하는 가장 최근 관찰가로, 알림 판단에는 쓰지 않습니다.
            last_alert_price = record["last_alert_price"]
            last_seen_price = price if isinstance(price, int) else record["last_seen_price"]

            if isinstance(last_alert_price, int) and isinstance(price, int):
                if price <= last_alert_price - PRICE_DROP_ALERT_THRESHOLD:
                    caption = (
                        f"💰[가격 인하] [{keyword}] {name}\n"
                        f"¥{last_alert_price:,} → ¥{price:,}\n🔗 {item_url}"
                    )
                    new_items.append(
                        {
                            "alert_id": f"drop:{item_id}:{last_alert_price}:{price}",
                            "caption": caption,
                            "photo": photo,
                        }
                    )
                    drop_count += 1
                    last_alert_price = price  # 역대 최저가 갱신 (알림을 보냈을 때만 내려감)
                # else: 역대 최저가보다 충분히 싸지지 않음 -> 기준가 유지 (가격이 올라도 그대로)
            else:
                last_alert_price = price

            remember(
                seen,
                item_id,
                {"last_alert_price": last_alert_price, "last_seen_price": last_seen_price},
            )

        if fingerprint:
            remember(
                relist_fingerprints,
                fingerprint,
                {
                    "item_id": item_id,
                    "last_alert_price": seen[item_id]["last_alert_price"],
                    "last_seen_price": seen[item_id]["last_seen_price"],
                },
            )

    print(
        f"[{keyword}] 검색 {len(items)}개 확인 "
        f"(신규 {new_count}개, 재출품 {relist_count}개, 가격인하 {drop_count}개, "
        f"오래된 매물 {stale_count}개 조용히 기록)"
    )
    return True


async def collect_updates() -> None:
    mercari = Mercapi()
    seen, pending, sent_alerts, relist_fingerprints, known_keywords, keyword_checked_at = load_state()
    is_first_run = len(seen) == 0
    new_items: list = []
    now = datetime.now().timestamp()

    for search in SEARCHES:
        keyword = search["query"]
        keyword_is_new = keyword not in known_keywords
        # 조회 시각 기록이 아직 없는 키워드는 '신규' 판정의 기준선이 없는 상태입니다.
        # 새로 추가한 키워드일 수도 있고, 이 기능을 배포한 직후의 첫 실행일 수도 있습니다.
        # 어느 쪽이든 이번 조회분은 기준선만 저장하고 넘어가야 알림 폭탄을 피할 수 있습니다.
        baseline_only = keyword_is_new or keyword not in keyword_checked_at
        keyword_items: list = []
        checked = await check_keyword(
            mercari,
            keyword,
            search["categories"],
            seen,
            relist_fingerprints,
            keyword_items,
            created_cutoff=new_item_cutoff(keyword_checked_at, keyword, now),
        )

        if baseline_only:
            # 기존에 이미 올라와 있던 매물이 전부 '신규'로 잡혀 알림 폭탄이 되는 걸 막기 위해,
            # 기준선(seen)만 저장하고 이번 조회분의 신규 알림은 보내지 않습니다.
            # 다음 조회부터는 정상적으로 알림이 옵니다. (가격 인하 알림은 이미 추적 중인
            # 매물에만 해당하므로 그대로 내보냅니다.)
            suppressed = sum(1 for e in keyword_items if e["alert_id"].startswith("new:"))
            if suppressed:
                reason = "새로 추가된 키워드" if keyword_is_new else "기준선 최초 기록"
                print(f"[{keyword}] {reason} 첫 조회: 매물 {suppressed}개 기준선만 저장, 알림 생략")
            keyword_items = [e for e in keyword_items if not e["alert_id"].startswith("new:")]
            if keyword_is_new and checked:
                known_keywords.add(keyword)

        new_items.extend(keyword_items)
        # 조회에 실패한 키워드는 시각을 갱신하지 않습니다.
        # 갱신해 버리면 검색이 실패한 그 구간에 올라온 매물을 다음 실행에서 '오래된 매물'로 보고 건너뜁니다.
        if checked:
            keyword_checked_at[keyword] = now
        await asyncio.sleep(1)

    if is_first_run:
        print(f"첫 실행: 기존 매물 {len(seen)}개를 기준으로 저장했습니다 (알림 생략)")
    else:
        pending = deduplicate_pending(pending + new_items, sent_alerts)
        print(f"새 알림 {len(new_items)}건 발견 (저장될 대기열 {len(pending)}건)")

    save_state(seen, pending, sent_alerts, relist_fingerprints, known_keywords, keyword_checked_at)


async def send_pending() -> None:
    seen, pending, sent_alerts, relist_fingerprints, known_keywords, keyword_checked_at = load_state()
    pending = deduplicate_pending(pending, sent_alerts)

    if not pending:
        print("전송할 대기 알림이 없습니다")
        save_state(seen, pending, sent_alerts, relist_fingerprints, known_keywords, keyword_checked_at)
        return

    try:
        pending, sent_alerts = await flush_pending(
            seen, pending, sent_alerts, relist_fingerprints, known_keywords, keyword_checked_at
        )
    finally:
        save_state(seen, pending, sent_alerts, relist_fingerprints, known_keywords, keyword_checked_at)

    if pending:
        print(f"[대기열에 {len(pending)}건 남음 -> 다음 실행에 재시도]", file=sys.stderr)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Mercari 알림 수집/전송")
    parser.add_argument(
        "--mode",
        choices=("collect", "send"),
        default="collect",
        help="collect는 검색 결과를 저장하고, send는 저장된 대기열을 텔레그램으로 전송합니다.",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    if args.mode == "collect":
        await collect_updates()
    else:
        await send_pending()


if __name__ == "__main__":
    asyncio.run(main())
