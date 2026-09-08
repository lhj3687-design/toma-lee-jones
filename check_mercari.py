"""
메루카리(mercari.jp) 키워드 알림 봇
- 신규 매물 알림
- 가격 인하 / 끌어올림 매물 감지 알림
- GitHub Actions 동시성 제어 및 재시도 로직 포함
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
MAX_ITEMS_PER_KEYWORD = 120

SEARCHES = [
    {"query": "Carol Christian Poell", "categories": []},
    {"query": "Martin Margiela", "categories": [30]},
    {"query": "マルタンマルジェラ", "categories": [30]},
    {"query": "Hermes Margiela", "categories": []},
    {"query": "Hermes", "categories": [30, 11, 12, 13]},
    {"query": "エルメス", "categories": [30, 11, 12, 13]},
    {"query": "Chrome Hearts", "categories": [30, 11]},
    {"query": "クロムハーツ", "categories": [30, 11]},
    {"query": "Richard Avedon", "categories": [3088]},
    {"query": "The Row", "categories": [2]},
    {"query": "ザ・ロウ", "categories": [2]},
]


def load_state():
    """seen(아이디:가격 딕셔너리)과 pending(전송 실패 대기열)을 불러옵니다."""
    if SEEN_FILE.exists():
        try:
            data = json.loads(SEEN_FILE.read_text())
            if isinstance(data, list):
                # 구버전 리스트 형태 호환 (가격은 0으로 임시 저장)
                return {item_id: 0 for item_id in data}, []
            seen = data.get("seen", {})
            if isinstance(seen, list):
                seen = {item_id: 0 for item_id in seen}
            return seen, data.get("pending", [])
        except Exception:
            return {}, []
    return {}, []


def save_state(seen: dict, pending: list) -> None:
    # 데이터가 너무 커지지 않도록 최근 5000개만 유지
    items = list(seen.items())[-5000:]
    data = {
        "seen": dict(items),
        "pending": pending[-500:],
    }
    SEEN_FILE.write_text(json.dumps(data, ensure_ascii=False))


def extract_field(item, candidates, default=None):
    data = asdict(item)
    for key in candidates:
        value = data.get(key)
        if value:
            return value
    return default


async def send_telegram(caption: str, photo_url):
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
    price_drop_count = 0

    for item in results.items[:MAX_ITEMS_PER_KEYWORD]:
        item_id = extract_field(item, ["id_", "id", "item_id", "itemId"])
        if not item_id:
            continue

        price = getattr(item, "price", None)
        current_price = int(price) if isinstance(price, (int, float, Decimal)) else 0

        name = getattr(item, "name", None) or extract_field(item, ["name", "title"], "(제목 없음)")
        photo = extract_field(item, ["thumbnails", "photos", "thumbnail", "image_url"])
        if isinstance(photo, (list, tuple)):
            photo = photo[0] if photo else None
        item_url = f"https://jp.mercari.com/item/{item_id}"

        # 1. 아예 처음 보는 신규 매물
        if item_id not in seen:
            seen[item_id] = current_price
            price_txt = f"¥{current_price:,}" if current_price else "가격 확인 필요"
            caption = f"[{keyword}] {name}\n💴 {price_txt}\n🔗 {item_url}"
            new_items.append({"caption": caption, "photo": photo})
            new_count += 1

        # 2. 이미 본 매물이지만 가격이 낮아진 경우 (가격 인하 및 끌어올림)
        else:
            old_price = seen[item_id]
            
            # 구버전에서 넘어와 가격이 0으로 저장된 경우, 현재 가격으로 갱신만 수행 (알림 X)
            if old_price == 0 and current_price > 0:
                seen[item_id] = current_price
                
            # 정상적으로 기록된 이전 가격보다 현재 가격이 낮아진 경우
            elif old_price > 0 and current_price > 0 and current_price < old_price:
                seen[item_id] = current_price  # 변동된 신규 가격으로 업데이트
                caption = (
                    f"🔻 [가격 인하/끌올] [{keyword}]\n"
                    f"{name}\n"
                    f"💴 ¥{old_price:,} ➔ ¥{current_price:,}\n"
                    f"🔗 {item_url}"
                )
                new_items.append({"caption": caption, "photo": photo})
                price_drop_count += 1

    print(f"[{keyword}] 검색 {len(results.items)}개 확인 (신규 {new_count}개, 인하 {price_drop_count}개)")


async def main() -> None:
    m = Mercapi()
    seen, pending = load_state()
    is_first_run = len(seen) == 0
    new_items: list = []

    for kw in SEARCHES:
        await check_keyword(m, kw["query"], kw["categories"], seen, new_items)
        await asyncio.sleep(1)

    if is_first_run:
        print(f"첫 실행: 기존 매물 {len(seen)}개를 기준으로 저장했습니다 (알림 생략)")
    else:
        pending.extend(new_items)
        print(f"새 매물/인하 {len(new_items)}건 발견 (대기 중 {len(pending)}건)")

    if pending:
        pending = await flush_pending(pending)
        if pending:
            print(f"[대기열에 {len(pending)}건 남음 -> 다음 실행에 재시도]", file=sys.stderr)

    save_state(seen, pending)


if __name__ == "__main__":
    asyncio.run(main())
