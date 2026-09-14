"""봇이 '애초에 볼 수 있는 범위'를 재 봅니다 (수동 실행 전용).

판정 감사(README "봇이 '옳은 것'을 알리는가")는 **상태 파일에 들어온 매물**만 대상으로
했습니다. 그래서 봇이 한 번도 못 본 매물은 통째로 감사 범위 밖이었습니다. 이 스크립트는
그 바깥을 봅니다.

봇의 시야를 좁히는 것은 두 가지입니다.

  1. **키워드당 정렬 방식마다 상위 120건**(`MAX_ITEMS_PER_KEYWORD`)만 봅니다.
  2. `SEARCHES`의 **카테고리 필터**가 일부 매물을 아예 빼 버립니다.

그래서 같은 키워드를 **더 깊은 페이지까지** / **필터 없이** 조회해서, 봇이 실제로
기록한 것(`seen_items.json`)과 대조합니다. 둘을 갈라서 봐야 "필터를 고쳐야 하는지,
창을 넓혀야 하는지"를 말할 수 있습니다.

읽기만 합니다 — 상태 파일을 건드리지 않고 알림도 보내지 않습니다.

    python scripts/coverage_scan.py --pages 5 --fresh-hours 24

'놓쳤다'의 기준은 **등록 시각**입니다. 봇은 기록을 시작한 뒤에 올라온 매물만 알림
대상으로 삼으므로, 오래된 매물이 상태 파일에 없는 건 정상입니다. 반대로 **최근에
올라왔는데 상태 파일에 없으면** 그건 신규 알림을 놓친 자리입니다.
"""
import argparse
import asyncio
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from check_mercari import (  # noqa: E402  (경로를 먼저 잡아야 합니다)
    MAX_ITEMS_PER_KEYWORD,
    SEARCHES,
    SEARCH_SORT_OPTIONS,
    SEEN_FILE,
    extract_item_id,
    item_fields,
    listing_created_at,
)

PAGE_PAUSE_SECONDS = 1.0  # 봇이 페이지 사이에 두는 간격과 같게 둡니다.


def load_seen_ids() -> set:
    """봇이 지금까지 기록한 매물 ID. 상태 파일을 읽기만 합니다."""
    if not SEEN_FILE.exists():
        print(f"[경고] {SEEN_FILE}가 없습니다. 전부 '처음 보는 매물'로 집계됩니다", file=sys.stderr)
        return set()
    data = json.loads(SEEN_FILE.read_text())
    seen = data.get("seen") or {}
    return set(seen if isinstance(seen, dict) else {str(item): None for item in seen})


async def walk(api, keyword: str, categories: list, pages: int) -> tuple[list, int]:
    """한 키워드를 등록순으로 pages 페이지까지 훑어 (필드 목록, 전체 건수)를 돌려줍니다.

    돌려주는 목록의 순서가 곧 순위입니다 — 봇은 이 중 앞 120건만 봅니다.
    """
    options = SEARCH_SORT_OPTIONS.get("created", {})
    collected: list = []
    total = None
    results = None
    for page_number in range(pages):
        try:
            if page_number == 0:
                results = await api.search(keyword, categories=categories, **options)
            else:
                results = await results.next_page()
        except Exception as exc:
            print(f"   [조회 실패: {keyword} {page_number + 1}페이지] {exc}", file=sys.stderr)
            break
        meta = getattr(results, "meta", None)
        if total is None:
            total = int(getattr(meta, "num_found", 0) or 0)
        page = [item_fields(item) for item in list(getattr(results, "items", []) or [])]
        collected.extend(page)
        if not page or not getattr(meta, "next_page_token", ""):
            break
        await asyncio.sleep(PAGE_PAUSE_SECONDS)
    return collected, (total or 0)


def split_by_window(fields_list: list) -> tuple[list, list]:
    """봇이 보는 창(상위 120건) 안과 밖으로 가릅니다."""
    return fields_list[:MAX_ITEMS_PER_KEYWORD], fields_list[MAX_ITEMS_PER_KEYWORD:]


def excluded_by_filter(filtered: list, unfiltered: list) -> list:
    """카테고리 필터가 빼 버린 매물.

    필터는 매물을 걸러내기만 하고 순서를 만들어 내지는 않습니다. 그래서 같은 깊이에서
    '필터 없는 목록에는 있는데 필터 목록에는 없는' 매물은 필터가 뺀 것이 확실합니다
    (필터를 통과했다면 순위가 앞당겨질 뿐 사라질 수 없습니다).
    """
    filtered_ids = {extract_item_id(fields) for fields in filtered}
    return [
        fields
        for fields in unfiltered[: len(filtered)]
        if extract_item_id(fields) not in filtered_ids
    ]


def count_missed(fields_list: list, seen_ids: set, fresh_after: float) -> tuple[int, int]:
    """(최근 등록분 개수, 그중 상태 파일에 없는 개수)."""
    fresh = [
        fields
        for fields in fields_list
        if (listing_created_at(fields) or 0) >= fresh_after
    ]
    missed = [fields for fields in fresh if extract_item_id(fields) not in seen_ids]
    return len(fresh), len(missed)


async def scan(pages: int, fresh_hours: float) -> None:
    from mercapi import Mercapi

    api = Mercapi()
    seen_ids = load_seen_ids()
    fresh_after = (datetime.now() - timedelta(hours=fresh_hours)).timestamp()
    print(
        f"봇이 보는 창: 키워드·정렬당 상위 {MAX_ITEMS_PER_KEYWORD}건 / "
        f"이번 조회 깊이: {pages}페이지({pages * MAX_ITEMS_PER_KEYWORD}건) / "
        f"상태 파일 {len(seen_ids):,}건 / '최근'의 기준: {fresh_hours}시간"
    )
    print()

    totals = {"window_missed": 0, "filter_missed": 0, "fresh": 0}
    rows = []
    for search in SEARCHES:
        keyword, categories = search["query"], search["categories"]
        filtered, found = await walk(api, keyword, categories, pages)
        await asyncio.sleep(PAGE_PAUSE_SECONDS)

        inside, outside = split_by_window(filtered)
        fresh_in, missed_in = count_missed(inside, seen_ids, fresh_after)
        fresh_out, missed_out = count_missed(outside, seen_ids, fresh_after)

        # 카테고리 필터가 빼는 매물: 같은 깊이에서 필터를 뺀 결과에만 있는 것들입니다.
        # 필터는 매물을 걸러내기만 하므로, 필터 없는 목록의 앞쪽에 있으면서 필터 목록에
        # 없다면 그건 필터가 빼 버린 것입니다.
        filter_missed = filter_fresh = 0
        unfiltered_found = None
        if categories:
            unfiltered, unfiltered_found = await walk(api, keyword, [], pages)
            await asyncio.sleep(PAGE_PAUSE_SECONDS)
            excluded = excluded_by_filter(filtered, unfiltered)
            filter_fresh, filter_missed = count_missed(excluded, seen_ids, fresh_after)

        totals["window_missed"] += missed_out
        totals["filter_missed"] += filter_missed
        totals["fresh"] += fresh_in + fresh_out
        rows.append(
            {
                "keyword": keyword,
                "categories": categories,
                "found": found,
                "unfiltered_found": unfiltered_found,
                "checked": len(filtered),
                "window_fresh": fresh_in,
                "window_missed": missed_in,
                "beyond_fresh": fresh_out,
                "beyond_missed": missed_out,
                "filter_fresh": filter_fresh,
                "filter_missed": filter_missed,
            }
        )
        print(
            f"[{keyword}] 전체 {found:,}건"
            + (f" (필터 없으면 {unfiltered_found:,}건)" if unfiltered_found is not None else "")
            + f" / 훑은 {len(filtered)}건"
            f" | 창 안 최근 {fresh_in}건 중 못 본 것 {missed_in}건"
            f" | 창 밖 최근 {fresh_out}건 중 못 본 것 {missed_out}건"
            + (f" | 필터가 뺀 최근 {filter_fresh}건 중 못 본 것 {filter_missed}건" if categories else "")
        )

    print()
    print("=" * 78)
    print(
        f"합계: 최근 {fresh_hours}시간 매물 {totals['fresh']:,}건을 확인했고, "
        f"창(상위 {MAX_ITEMS_PER_KEYWORD}건) 밖이라 못 본 것 {totals['window_missed']:,}건, "
        f"카테고리 필터 때문에 못 본 것 {totals['filter_missed']:,}건입니다."
    )
    worst_window = sorted(rows, key=lambda r: -r["beyond_missed"])[:5]
    worst_filter = sorted(rows, key=lambda r: -r["filter_missed"])[:5]
    print("\n창 밖이라 놓친 것이 많은 키워드:")
    for row in worst_window:
        print(f"   {row['keyword']:<34} {row['beyond_missed']:>5}건 (전체 {row['found']:,}건)")
    print("\n카테고리 필터가 빼는 것이 많은 키워드:")
    for row in worst_filter:
        print(f"   {row['keyword']:<34} {row['filter_missed']:>5}건 (카테고리 {row['categories']})")
    print("완료")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pages", type=int, default=5, help="키워드당 훑을 페이지 수(1페이지=120건)")
    parser.add_argument("--fresh-hours", type=float, default=24.0, help="'최근 등록'으로 볼 시간")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(scan(args.pages, args.fresh_hours))
