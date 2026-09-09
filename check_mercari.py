"""
메루카리(mercari.jp) 키워드 알림 봇
지정한 키워드로 새 매물이 올라오면 텔레그램으로 알림을 보냅니다.
GitHub Actions에서 주기적으로 이 스크립트를 실행하도록 설정되어 있습니다.
"""

import asyncio
import json
import os
import sys
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

import httpx
from mercapi import Mercapi

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

SEEN_FILE = Path("seen_items.json")
MAX_ITEMS_PER_KEYWORD = 120  # 키워드당 확인할 최근 매물 개수 (한 번에 불러오는 개수와 동일)

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


def load_state():
    """seen({매물ID: 마지막으로 확인한 가격})과 pending(전송 대기 중인 알림)을 불러옵니다."""
    if SEEN_FILE.exists():
        try:
            data = json.loads(SEEN_FILE.read_text())
            if isinstance(data, list):
                return {i: None for i in data}, []  # 구버전(ID 리스트만) 호환
            raw_seen = data.get("seen", [])
            if isinstance(raw_seen, list):
                seen = {i: None for i in raw_seen}  # 가격 추적 도입 전 버전 호환
            else:
                seen = raw_seen
            return seen, data.get("pending", [])
        except Exception:
            return {}, []
    return {}, []


def save_state(seen: dict, pending: list) -> None:
    data = {
        "seen": dict(list(seen.items())[-5000:]),  # 무한정 커지지 않도록 최근 5000개만 유지
        "pending": pending[-500:],  # 대기열도 상한선을 둠
    }
    SEEN_FILE.write_text(json.dumps(data, ensure_ascii=False))


def extract_field(item, candidates, default=None):
    """dataclass 필드명이 라이브러리 버전마다 다를 수 있어, 후보 키를 순서대로 시도합니다."""
    data = asdict(item)
    for key in candidates:
        value = data.get(key)
        if value:
            return value
    return default


async def send_telegram(caption: str, photo_url):
    """전송 성공 여부와, 레이트리밋일 경우 텔레그램이 알려준 대기 초를 반환합니다."""
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            if photo_url:
                resp = await client.post(
                    f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
                    data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption, "photo": photo_url},
                )
            else:
                resp = await client.post(
                    f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                    data={"chat_id": TELEGRAM_CHAT_ID, "text": caption},
                )
        except Exception as e:
            print(f"[텔레그램 전송 에러] {e}", file=sys.stderr)
            return False, None

        if resp.status_code == 200:
            return True, None

        retry_after = None
        try:
            retry_after = resp.json().get("parameters", {}).get("retry_after")
        except Exception:
            pass
        print(f"[텔레그램 전송 실패 {resp.status_code}] {resp.text}", file=sys.stderr)
        return False, retry_after


async def flush_pending(pending: list) -> list:
    """대기열의 알림을 순서대로 전송 시도합니다.
    - 짧은 레이트리밋(60초 이하)이면 그만큼 기다렸다가 같은 항목을 재시도합니다.
    - 3번 넘게 실패한 항목은 포기하고 건너뜁니다 (큐가 영구히 막히는 것 방지).
    - 그 외 실패/긴 레이트리밋이면 이번 실행은 중단하고, 남은 항목은 다음 실행 때 자동 재시도됩니다.
    """
    remaining = list(pending)
    sent = 0

    while remaining:
        entry = remaining[0]
        ok, retry_after = await send_telegram(entry["caption"], entry.get("photo"))

        if ok:
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

        # 복구 불가능해 보이는 실패이거나 대기 시간이 김 -> 이번 실행은 중단, 다음 실행에 이어서 시도
        break

    if sent:
        print(f"텔레그램 알림 {sent}건 전송 완료")
    return remaining


async def check_keyword(m: Mercapi, keyword: str, categories: list, seen: dict, new_items: list) -> None:
    try:
        results = await m.search(keyword, categories=categories)
    except Exception as e:
        print(f"[검색 실패: {keyword}] {e}", file=sys.stderr)
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

        # Mercari Shops(입점 상점) 상품은 /item/이 아니라 /shops/product/ 주소를 써야 함
        item_type = str(extract_field(item, ["item_type"], "")).upper()
        if "SHOP" in item_type:
            item_url = f"https://jp.mercari.com/shops/product/{item_id}"
        else:
            item_url = f"https://jp.mercari.com/item/{item_id}"
        price_txt = f"¥{price:,}" if isinstance(price, int) else "가격 확인 필요"

        if item_id not in seen:
            seen[item_id] = price
            caption = f"[{keyword}] {name}\n💴 {price_txt}\n🔗 {item_url}"
            new_items.append({"caption": caption, "photo": photo})
            new_count += 1
            continue

        old_price = seen.get(item_id)
        if isinstance(old_price, int) and isinstance(price, int) and price < old_price:
            caption = (
                f"💰[가격 인하] [{keyword}] {name}\n"
                f"¥{old_price:,} → ¥{price:,}\n🔗 {item_url}"
            )
            new_items.append({"caption": caption, "photo": photo})
            drop_count += 1
        seen[item_id] = price  # 항상 최신 가격으로 갱신 (인상이든 인하든)

    print(f"[{keyword}] 검색 {len(results.items)}개 확인 (신규 {new_count}개, 가격인하 {drop_count}개)")


async def main() -> None:
    m = Mercapi()
    seen, pending = load_state()
    is_first_run = len(seen) == 0
    new_items: list = []

    for kw in SEARCHES:
        await check_keyword(m, kw["query"], kw["categories"], seen, new_items)
        await asyncio.sleep(1)  # 메루카리 서버에 부담 주지 않도록 살짝 간격

    if is_first_run:
        # 첫 실행에서는 기존 매물 전부가 "새 매물"로 오인되므로, 알림 없이 기준점만 저장
        print(f"첫 실행: 기존 매물 {len(seen)}개를 기준으로 저장했습니다 (알림 생략)")
    else:
        pending.extend(new_items)
        print(f"새 매물 {len(new_items)}건 발견 (대기 중 {len(pending)}건)")

    if pending:
        pending = await flush_pending(pending)
        if pending:
            print(f"[대기열에 {len(pending)}건 남음 -> 다음 실행에 재시도]", file=sys.stderr)

    save_state(seen, pending)


if __name__ == "__main__":
    asyncio.run(main())
