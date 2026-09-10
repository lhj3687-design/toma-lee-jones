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


def save_state(seen: dict, pending: list, sent_alerts: list) -> None:
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
    data = asdict(item)
    for key in candidates:
        value = data.get(key)
        if value:
            return value
    return default


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


async def flush_pending(pending: list, sent_alerts: list) -> tuple[list, list]:
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
            new_items.append({"alert_id": f"new:{item_id}", "caption": caption, "photo": photo})
            new_count += 1
            continue

        old_price = seen.get(item_id)
        if isinstance(old_price, int) and isinstance(price, int) and price < old_price:
            caption = (
                f"💰[가격 인하] [{keyword}] {name}\n"
                f"¥{old_price:,} → ¥{price:,}\n🔗 {item_url}"
            )
            new_items.append(
                {"alert_id": f"drop:{item_id}:{old_price}:{price}", "caption": caption, "photo": photo}
            )
            drop_count += 1
        seen[item_id] = price

    print(f"[{keyword}] 검색 {len(results.items)}개 확인 (신규 {new_count}개, 가격인하 {drop_count}개)")


async def collect_updates() -> None:
    mercari = Mercapi()
    seen, pending, sent_alerts = load_state()
    is_first_run = len(seen) == 0
    new_items: list = []

    for search in SEARCHES:
        await check_keyword(mercari, search["query"], search["categories"], seen, new_items)
        await asyncio.sleep(1)

    if is_first_run:
        print(f"첫 실행: 기존 매물 {len(seen)}개를 기준으로 저장했습니다 (알림 생략)")
    else:
        pending = deduplicate_pending(pending + new_items, sent_alerts)
        print(f"새 알림 {len(new_items)}건 발견 (저장될 대기열 {len(pending)}건)")

    save_state(seen, pending, sent_alerts)


async def send_pending() -> None:
    seen, pending, sent_alerts = load_state()
    pending = deduplicate_pending(pending, sent_alerts)

    if not pending:
        print("전송할 대기 알림이 없습니다")
        save_state(seen, pending, sent_alerts)
        return

    try:
        pending, sent_alerts = await flush_pending(pending, sent_alerts)
    finally:
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
