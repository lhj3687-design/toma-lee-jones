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
import re
import subprocess
import sys
import unicodedata
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

import httpx
from mercapi import Mercapi
from mercapi.requests import SearchRequestData

# 일반 메루카리 개인 매물 ID는 항상 "m" + 숫자 형식입니다 (예: m90925725213).
# 이 형식이 아니면 메루카리 숍스(기업 판매자) 상품이므로 /shops/product/ 링크를 써야 합니다.
# item_type 필드만으로는 라이브러리가 숍스 여부를 늘 정확히 채워주지 않아 ID 형식을 보조 판단 기준으로 씁니다.
MERCARI_ITEM_ID_PATTERN = re.compile(r"^m\d+$")

# 제목 정규화 시 제거할 기호(전각/반각 공백, 대괄호·괄호·특수문자 등 꾸밈 차이 무시용)
_TITLE_NOISE_PATTERN = re.compile(r"[\s\u3000!-/:-@\[-`{-~、。・【】「」『』［］（）]+")

SEEN_FILE = Path("seen_items.json")
MAX_ITEMS_PER_KEYWORD = 120  # 키워드당 확인할 최근 매물 개수
MAX_SEEN_ITEMS = 5000
MAX_RELIST_FINGERPRINTS = 5000
PRICE_DROP_ALERT_THRESHOLD = 1000  # 마지막 알림 가격보다 이 금액(엔) 이상 떨어졌을 때만 알립니다.
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


def load_state() -> tuple[dict, list, list, dict, set]:
    if not SEEN_FILE.exists():
        return {}, [], [], {}, set(LEGACY_KEYWORDS_SEEDED_AT_UPGRADE)
    try:
        data = json.loads(SEEN_FILE.read_text())
    except Exception as exc:
        print(f"[상태 파일 읽기 실패] {exc}", file=sys.stderr)
        return {}, [], [], {}, set(LEGACY_KEYWORDS_SEEDED_AT_UPGRADE)

    if isinstance(data, list):
        return {item_id: None for item_id in data}, [], [], {}, set(LEGACY_KEYWORDS_SEEDED_AT_UPGRADE)

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

    return (
        seen if isinstance(seen, dict) else {},
        pending if isinstance(pending, list) else [],
        sent_alerts if isinstance(sent_alerts, list) else [],
        relist_fingerprints if isinstance(relist_fingerprints, dict) else {},
        known_keywords,
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


def save_state(seen: dict, pending: list, sent_alerts: list, relist_fingerprints: dict, known_keywords: set) -> None:
    sent_alerts = unique_recent(sent_alerts, MAX_SENT_ALERTS)
    pending = deduplicate_pending(pending, sent_alerts)
    data = {
        "seen": dict(list(seen.items())[-MAX_SEEN_ITEMS:]),
        "pending": pending,
        "sent_alerts": sent_alerts,
        "relist_fingerprints": dict(list(relist_fingerprints.items())[-MAX_RELIST_FINGERPRINTS:]),
        "known_keywords": sorted(known_keywords),
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
    다를 수 있어 평평한 필드와 중첩된 seller 객체를 모두 시도합니다."""
    data = asdict(item)
    direct = data.get("seller_id") or data.get("sellerId")
    if direct:
        return str(direct)
    seller = data.get("seller")
    if isinstance(seller, dict):
        nested = seller.get("id") or seller.get("id_") or seller.get("seller_id")
        if nested:
            return str(nested)
    return None


def relist_fingerprint(seller_id, name, price, photo) -> str | None:
    """같은 매물의 재출품(삭제 후 재등록)을 잡아내기 위한 지문을 만듭니다.

    판매자 ID를 알 수 있어도 제목만으로는 같은 판매자의 다른 상품을 잘못 합칠 수
    있으므로, 사진이 있으면 '판매자+정규화된 제목+대표 사진'을 함께 사용합니다.
    사진이 없으면 보수적으로 '판매자+정규화된 제목+가격'을 사용해 가격이 달라진
    별도 상품을 재출품으로 잘못 합치지 않습니다.
    판매자 ID를 못 가져오면 '정규화된 제목+정확히 같은 가격'으로 대체합니다.
    """
    normalized_title = normalize_title(name)
    if not normalized_title:
        return None
    if seller_id and photo:
        return f"seller-photo:{seller_id}:{normalized_title}:{photo}"
    if seller_id and isinstance(price, int):
        return f"seller-price:{seller_id}:{normalized_title}:{price}"
    if isinstance(price, int):
        return f"title:{normalized_title}:{price}"
    return None


def created_timestamp(item) -> float:
    """created 필드를 정렬용 숫자로 변환합니다 (필드가 없으면 가장 오래된 값)."""
    value = getattr(item, "created", None)
    if value is None:
        return 0.0
    if hasattr(value, "timestamp"):
        try:
            return float(value.timestamp())
        except (TypeError, ValueError, OverflowError):
            return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def sort_latest_first(items: list) -> list:
    """API 최신순 요청을 보완하기 위해 응답도 created 내림차순으로 정렬합니다."""
    return sorted(items, key=created_timestamp, reverse=True)


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


async def flush_pending(
    seen: dict, pending: list, sent_alerts: list, relist_fingerprints: dict, known_keywords: set
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
            save_state(seen, remaining, sent_alerts, relist_fingerprints, known_keywords)
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


async def check_keyword(
    m: Mercapi, keyword: str, categories: list, seen: dict, relist_fingerprints: dict, new_items: list
) -> bool:
    try:
        results = await m.search(
            keyword,
            categories=categories,
            sort_by=SearchRequestData.SortBy.SORT_CREATED_TIME,
            sort_order=SearchRequestData.SortOrder.ORDER_DESC,
        )
    except Exception as exc:
        print(f"[검색 실패: {keyword}] {exc}", file=sys.stderr)
        return False

    new_count = 0
    relist_count = 0
    drop_count = 0
    ordered_items = sort_latest_first(list(results.items))
    for item in ordered_items[:MAX_ITEMS_PER_KEYWORD]:
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
        seller_id = extract_seller_id(item)
        fingerprint = relist_fingerprint(seller_id, name, price, photo)

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
            # 새로 보이는 ID지만, 같은 판매자(또는 같은 제목+가격)의 지문이 이미 있다면
            # 삭제 후 재등록된 매물로 보고 예전 가격 이력을 새 ID로 이어받습니다.
            matched = relist_fingerprints.get(fingerprint) if fingerprint else None
            if matched and matched.get("item_id") != item_id:
                record = {
                    "last_alert_price": matched.get("last_alert_price"),
                    "last_seen_price": matched.get("last_seen_price"),
                }
                seen.pop(matched.get("item_id"), None)
                relist_count += 1
            else:
                record = None

        if record is None:
            seen[item_id] = {"last_alert_price": price, "last_seen_price": price}
            caption = f"[{keyword}] {name}\n💴 {price_txt}\n🔗 {item_url}"
            new_items.append({"alert_id": f"new:{item_id}", "caption": caption, "photo": photo})
            new_count += 1
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

            seen[item_id] = {"last_alert_price": last_alert_price, "last_seen_price": last_seen_price}

        if fingerprint:
            relist_fingerprints[fingerprint] = {
                "item_id": item_id,
                "last_alert_price": seen[item_id]["last_alert_price"],
                "last_seen_price": seen[item_id]["last_seen_price"],
            }

    print(
        f"[{keyword}] 검색 {len(ordered_items)}개 확인 "
        f"(신규 {new_count}개, 재출품 {relist_count}개, 가격인하 {drop_count}개)"
    )
    return True


async def collect_updates() -> None:
    mercari = Mercapi()
    seen, pending, sent_alerts, relist_fingerprints, known_keywords = load_state()
    is_first_run = len(seen) == 0
    new_items: list = []

    for search in SEARCHES:
        keyword = search["query"]
        keyword_is_new = keyword not in known_keywords
        keyword_items: list = []
        search_succeeded = await check_keyword(
            mercari, keyword, search["categories"], seen, relist_fingerprints, keyword_items
        )

        if keyword_is_new and search_succeeded:
            # 첫 성공 조회는 신규/가격인하/재출품 등 종류와 무관하게 모두 기준선으로만
            # 반영합니다. 검색 실패를 성공으로 기록하지 않아 다음 정상 조회가 안전합니다.
            suppressed = len(keyword_items)
            if suppressed:
                print(f"[{keyword}] 새 키워드 첫 성공 조회: 알림 {suppressed}건 기준선만 저장, 알림 생략")
            keyword_items = []
            known_keywords.add(keyword)

        new_items.extend(keyword_items)
        await asyncio.sleep(1)

    if is_first_run:
        print(f"첫 실행: 기존 매물 {len(seen)}개를 기준으로 저장했습니다 (알림 생략)")
    else:
        pending = deduplicate_pending(pending + new_items, sent_alerts)
        print(f"새 알림 {len(new_items)}건 발견 (저장될 대기열 {len(pending)}건)")

    save_state(seen, pending, sent_alerts, relist_fingerprints, known_keywords)


async def send_pending() -> None:
    seen, pending, sent_alerts, relist_fingerprints, known_keywords = load_state()
    pending = deduplicate_pending(pending, sent_alerts)

    if not pending:
        print("전송할 대기 알림이 없습니다")
        save_state(seen, pending, sent_alerts, relist_fingerprints, known_keywords)
        return

    try:
        pending, sent_alerts = await flush_pending(seen, pending, sent_alerts, relist_fingerprints, known_keywords)
    finally:
        save_state(seen, pending, sent_alerts, relist_fingerprints, known_keywords)

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
