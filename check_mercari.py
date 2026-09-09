"""
메루카리(mercari.jp) 키워드 알림 봇

GitHub Actions는 아래 순서로 이 파일을 두 번 실행합니다.
1. collect: 새 매물/가격 인하를 찾아 상태와 대기열을 먼저 저장합니다.
2. send: 이미 원격 저장소에 저장된 대기열만 텔레그램으로 전송합니다.

이 순서 덕분에 텔레그램 전송이 오래 걸리거나 실행이 중간에 겹쳐도,
같은 매물이나 같은 가격 인하가 다시 새 알림으로 등록되는 일을 막습니다.
"""

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

import httpx
from mercapi import Mercapi

SEEN_FILE = Path("seen_items.json")
MAX_ITEMS_PER_KEYWORD = 120  # 키워드당 확인할 최근 매물 개수
MAX_SEEN_ITEMS = 5000
MAX_PENDING_ALERTS = 500
MAX_SENT_ALERTS = 5000

# 검색 설정: 키워드마다 필요하면 카테고리를 지정합니다.
# categories가 빈 리스트면 카테고리 제한 없이 전체에서 검색합니다.
# 카테고리 ID: 멘즈=2, 멘즈>탑스=30, 레이디스>탑스=11,
#              레이디스>재킷·아우터=12, 레이디스>팬츠=13, 패션 전체=3088
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
]


def load_state() -> tuple[dict, list, list]:
    """seen, pending, sent_alerts를 불러오며 이전 상태 파일도 호환합니다."""
    if not SEEN_FILE.exists():
        return {}, [], []

    try:
        data = json.loads(SEEN_FILE.read_text())
    except Exception as exc:
        print(f"[상태 파일 읽기 실패] {exc}", file=sys.stderr)
        return {}, [], []

    if isinstance(data, list):
        return {item_id: None for item_id in data}, [], []

    raw_seen = data.get("seen", [])
    seen = {item_id: None for item_id in raw_seen} if isinstance(raw_seen, list) else raw_seen
    pending = data.get("pending", [])
    sent_alerts = data.get("sent_alerts", [])

    return (
        seen if isinstance(seen, dict) else {},
        pending if isinstance(pending, list) else [],
        sent_alerts if isinstance(sent_alerts, list) else [],
    )


def alert_key(entry: dict) -> str:
    """새 형식과 기존 대기열 형식 모두에 쓸 수 있는 알림 고유 키를 반환합니다."""
    value = entry.get("alert_id")
    if value:
        return str(value)
    return f"legacy:{entry.get('caption', '')}"


def unique_recent(values: list[str], limit: int) -> list[str]:
    """순서를 유지하며 중복을 제거한 최근 항목만 남깁니다."""
    result = []
    seen_values = set()
    for value in values:
        value = str(value)
        if value and value not in seen_values:
            result.append(value)
            seen_values.add(value)
    return result[-limit:]


def deduplicate_pending(pending: list, sent_alerts: list) -> list:
    """이미 전송됐거나 대기열에 있는 같은 알림을 한 건으로 정리합니다."""
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


def save_state(seen: dict, pending: list, sent_alerts: list) -> None:
    """상태 파일을 원자적으로 교체해 실행 중간의 손상을 피합니다."""
    sent_alerts = unique_recent(sent_alerts, MAX_SENT_ALERTS)
    pending = deduplicate_pending(pending, sent_alerts)
    data = {
        "seen": dict(list(seen.items())[-MAX_SEEN_ITEMS:]),
        "pending": pending,
        "sent_alerts": sent_alerts,
    }
    temporary_file = SEEN_FILE.with_suffix(".tmp")
    temporary_file.write_text(json.dumps(data, ensure_ascii=False))
    temporary_file.replace(SEEN_FILE)


def extract_field(item, candidates, default=None):
    """dataclass 필드명이 라이브러리 버전마다 다를 수 있어 후보 키를 순서대로 시도합니다."""
    data = asdict(item)
    for key in candidates:
        value = data.get(key)
        if value:
            return value
    return default


async def send_telegram(caption: str, photo_url):
    """전송 성공 여부와 레이트리밋일 경우 텔레그램이 알려준 대기 초를 반환합니다."""
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


async def flush_pending(pending: list, sent_alerts: list) -> tuple[list, list]:
    """대기열을 전송하고 성공한 알림 키를 sent_alerts에 기록합니다."""
    remaining = list(pending)
    sent = 0

    while remaining:
        entry = remaining[0]
        key = alert_key(entry)

        # 병합 재시도 중 예전 대기열이 되살아나도 재전송하지 않습니다.
        if key in sent_alerts:
            remaining.pop(0)
            continue

        ok, retry_after = await send_telegram(entry["caption"], entry.get("photo"))
        if ok:
            sent_alerts.append(key)
            remaining.pop(0)
            sent += 1
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

        # 복구 불가능해 보이는 실패이거나 대기 시간이 김 -> 다음 실행에 이어서 시도
        break

    if sent:
        print(f"텔레그램 알림 {sent}건 전송 완료")
    return remaining, unique_recent(sent_alerts, MAX_SENT_ALERTS)


async def check_keyword(m: Mercapi, keyword: str, categories: list, seen: dict, new_items: list) -> None:
    try:
        results = await m.search(keyword, categories=categories)
    except Exception as exc:
        print(f"[검색 실패: {keyword}] {exc}", file=sys.stderr)
        return

    new_count = 0
    drop_count = 0
    for item in results.items[:MAX_ITEMS_PER_KEYWORD]:
        item_id = extract_field(item, ["id_", "id", "item_id", "itemId"])
        if not item_id:
            continue

        name = getattr(item, "name", None) or extract_field(item, ["name", "title"], "(제목 없음)")
        price = getattr(item, "price", None)
        if isinstance(price, Decimal):
            price = int(price)
        photo = extract_field(item, ["thumbnails", "photos", "thumbnail", "image_url"])
        if isinstance(photo, (list, tuple)):
            photo = photo[0] if photo else None

        # Mercari Shops(입점 상점) 상품은 /item/이 아니라 /shops/product/ 주소를 써야 합니다.
        item_type = str(extract_field(item, ["item_type"], "")).upper()
        item_url = (
            f"https://jp.mercari.com/shops/product/{item_id}"
            if "SHOP" in item_type
            else f"https://jp.mercari.com/item/{item_id}"
        )
        price_txt = f"¥{price:,}" if isinstance(price, int) else "가격 확인 필요"

        if item_id not in seen:
            seen[item_id] = price
            caption = f"[{keyword}] {name}\n💴 {price_txt}\n🔗 {item_url}"
            new_items.append(
                {"alert_id": f"new:{item_id}", "caption": caption, "photo": photo}
            )
            new_count += 1
            continue

        old_price = seen.get(item_id)
        if isinstance(old_price, int) and isinstance(price, int) and price < old_price:
            caption = (
                f"💰[가격 인하] [{keyword}] {name}\n"
                f"¥{old_price:,} → ¥{price:,}\n🔗 {item_url}"
            )
            new_items.append(
                {
                    "alert_id": f"drop:{item_id}:{old_price}:{price}",
                    "caption": caption,
                    "photo": photo,
                }
            )
            drop_count += 1
        seen[item_id] = price  # 인상·인하와 무관하게 최신 가격으로 갱신

    print(f"[{keyword}] 검색 {len(results.items)}개 확인 (신규 {new_count}개, 가격인하 {drop_count}개)")


async def collect_updates() -> None:
    """검색 결과와 알림 대기열을 먼저 로컬 상태 파일에 기록합니다."""
    mercari = Mercapi()
    seen, pending, sent_alerts = load_state()
    is_first_run = len(seen) == 0
    new_items: list = []

    for search in SEARCHES:
        await check_keyword(mercari, search["query"], search["categories"], seen, new_items)
        await asyncio.sleep(1)  # 메루카리 서버에 부담을 주지 않도록 간격 유지

    if is_first_run:
        print(f"첫 실행: 기존 매물 {len(seen)}개를 기준으로 저장했습니다 (알림 생략)")
    else:
        pending = deduplicate_pending(pending + new_items, sent_alerts)
        print(f"새 알림 {len(new_items)}건 발견 (저장될 대기열 {len(pending)}건)")

    # 이 저장본은 바로 다음 Actions 단계에서 GitHub에 먼저 반영됩니다.
    save_state(seen, pending, sent_alerts)


async def send_pending() -> None:
    """GitHub에 먼저 저장된 대기열만 전송하고 결과를 다시 상태 파일에 기록합니다."""
    seen, pending, sent_alerts = load_state()
    pending = deduplicate_pending(pending, sent_alerts)

    if not pending:
        print("전송할 대기 알림이 없습니다")
        save_state(seen, pending, sent_alerts)
        return

    try:
        pending, sent_alerts = await flush_pending(pending, sent_alerts)
    finally:
        # 전송 도중 예외가 생겨도 이미 성공한 알림은 다음 실행에 재전송하지 않도록 저장합니다.
        save_state(seen, pending, sent_alerts)

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
