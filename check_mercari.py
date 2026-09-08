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
MAX_ITEMS_PER_KEYWORD = 40  # 키워드당 확인할 최근 매물 개수

# 검색 키워드 목록. 영문/일본어를 함께 넣으면 놓치는 매물이 줄어듭니다.
# 필요하면 이 리스트에 자유롭게 추가/삭제하세요.
KEYWORDS = [
    "Carol Christian Poell",
    "Martin Margiela",
    "マルタンマルジェラ",
    "Hermes Margiela",
    "Hermes",
    "エルメス",
    "Chrome Hearts",
    "クロムハーツ",
    "Richard Avedon",
    "The Row",
    "ザ・ロウ",
]


def load_seen() -> set:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text()))
        except Exception:
            return set()
    return set()


def save_seen(seen: set) -> None:
    # 파일이 무한정 커지지 않도록 최근 5000개만 유지
    SEEN_FILE.write_text(json.dumps(list(seen)[-5000:], ensure_ascii=False))


def extract_field(item, candidates, default=None):
    """dataclass 필드명이 라이브러리 버전마다 다를 수 있어, 후보 키를 순서대로 시도합니다."""
    data = asdict(item)
    for key in candidates:
        value = data.get(key)
        if value:
            return value
    return default


async def send_telegram(caption: str, photo_url) -> None:
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
            if resp.status_code != 200:
                print(f"[텔레그램 전송 실패 {resp.status_code}] {resp.text}", file=sys.stderr)
        except Exception as e:
            print(f"[텔레그램 전송 에러] {e}", file=sys.stderr)


_DEBUG_PRINTED = False


async def check_keyword(m: Mercapi, keyword: str, seen: set, new_items: list) -> None:
    global _DEBUG_PRINTED
    try:
        results = await m.search(keyword)
    except Exception as e:
        print(f"[검색 실패: {keyword}] {e}", file=sys.stderr)
        return

    print(f"[{keyword}] 검색 결과 {len(results.items)}개 (전체 {results.meta.num_found}개 중)")

    for item in results.items[:MAX_ITEMS_PER_KEYWORD]:
        if not _DEBUG_PRINTED:
            print("[디버그] 상품 원본 필드:", json.dumps(asdict(item), default=str, ensure_ascii=False)[:1500])
            _DEBUG_PRINTED = True

        item_id = extract_field(item, ["id", "item_id", "itemId"])
        if not item_id or item_id in seen:
            continue
        seen.add(item_id)

        name = getattr(item, "name", None) or extract_field(item, ["name", "title"], "(제목 없음)")
        price = getattr(item, "price", None)
        photo = extract_field(item, ["thumbnails", "photos", "thumbnail", "image_url"])
        if isinstance(photo, (list, tuple)):
            photo = photo[0] if photo else None

        item_url = f"https://jp.mercari.com/item/{item_id}"
        if isinstance(price, (int, float, Decimal)):
            price_txt = f"¥{int(price):,}"
        else:
            price_txt = "가격 확인 필요"

        caption = f"[{keyword}] {name}\n💴 {price_txt}\n🔗 {item_url}"
        new_items.append((caption, photo))


async def main() -> None:
    m = Mercapi()
    seen = load_seen()
    is_first_run = len(seen) == 0
    new_items: list = []

    for kw in KEYWORDS:
        await check_keyword(m, kw, seen, new_items)
        await asyncio.sleep(1)  # 메루카리 서버에 부담 주지 않도록 살짝 간격

    if is_first_run:
        # 첫 실행에서는 기존 매물 전부가 "새 매물"로 오인되므로, 알림 없이 기준점만 저장
        print(f"첫 실행: 기존 매물 {len(seen)}개를 기준으로 저장했습니다 (알림 생략)")
    else:
        for caption, photo in new_items:
            await send_telegram(caption, photo)
            await asyncio.sleep(1)  # 텔레그램 레이트리밋 방지
        print(f"새 매물 {len(new_items)}건 알림 전송 완료")

    save_seen(seen)


if __name__ == "__main__":
    asyncio.run(main())
