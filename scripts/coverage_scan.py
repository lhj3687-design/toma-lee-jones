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
    FULL_SCAN_INTERVAL_SECONDS,
    MAX_ITEMS_PER_KEYWORD,
    MAX_LOOKBACK_SECONDS,
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


def category_breakdown(fields_list: list, top: int = 6) -> list:
    """카테고리별로 묶어 (카테고리, 건수, 제목 예시)를 돌려줍니다.

    '필터가 N건을 뺐다'만으로는 그게 결함인지 알 수 없습니다 — 필터는 원래 빼라고
    걸어 둔 것이기 때문입니다. 빠진 것이 **무엇인지** 봐야 판단이 됩니다. 향수·식기처럼
    애초에 원하지 않는 카테고리면 필터가 제 일을 한 것이고, 옷·가방이 빠지고 있으면
    필터가 잘못 걸린 것입니다.
    """
    buckets: dict = {}
    for fields in fields_list:
        key = fields.get("category_id")
        bucket = buckets.setdefault(key, {"count": 0, "names": []})
        bucket["count"] += 1
        if len(bucket["names"]) < 2:
            name = str(fields.get("name") or "")
            bucket["names"].append(name[:34])
    ordered = sorted(buckets.items(), key=lambda kv: -kv[1]["count"])[:top]
    return [(key, value["count"], value["names"]) for key, value in ordered]


def fresh_only(fields_list: list, fresh_after: float) -> list:
    """최근 등록분만 남깁니다."""
    return [f for f in fields_list if (listing_created_at(f) or 0) >= fresh_after]


def count_missed(fields_list: list, seen_ids: set, fresh_after: float) -> tuple[int, int]:
    """(최근 등록분 개수, 그중 상태 파일에 없는 개수)."""
    fresh = [
        fields
        for fields in fields_list
        if (listing_created_at(fields) or 0) >= fresh_after
    ]
    missed = [fields for fields in fresh if extract_item_id(fields) not in seen_ids]
    return len(fresh), len(missed)


def age_minutes(fields: dict, now: float) -> float | None:
    """이 매물이 올라온 지 몇 분 됐는지 (등록 시각을 모르면 None)."""
    created = listing_created_at(fields)
    return None if created is None else (now - created) / 60.0


def window_depth(fields_list: list, now: float, sizes: tuple = ()) -> list:
    """'창 N건'이 이 키워드에서 **몇 분치인지** 돌려줍니다.

    창 크기를 건수로만 보면 120은 그냥 큰 숫자입니다. 하지만 봇에게 중요한 것은
    시간 깊이입니다 — 직전 조회 이후에 올라온 매물이 전부 창 안에 들어와야 놓치지
    않기 때문입니다. 같은 120건이 매물이 쏟아지는 키워드에서는 몇 시간치고,
    한산한 키워드에서는 몇 달치입니다. 그래서 '120건이면 충분한가'는 키워드마다
    답이 다르고, 줄여도 되는지도 이 값으로만 말할 수 있습니다.

    돌려주는 값은 (창 크기, 분) 목록입니다. 매물 수가 그 크기에 못 미치면(창이 남으면)
    분 대신 None입니다 — 그 키워드에서는 창이 제약이 아니라는 뜻입니다.
    """
    sizes = sizes or (30, 60, MAX_ITEMS_PER_KEYWORD)
    profile = []
    for size in sizes:
        if len(fields_list) < size:
            profile.append((size, None))
            continue
        profile.append((size, age_minutes(fields_list[size - 1], now)))
    return profile


def order_inversions(fields_list: list) -> int:
    """등록순 결과가 정말 등록 시각 내림차순인지 셉니다(바로 앞보다 최근인 항목 수).

    이 값이 0이 아니면 '앞 120건 = 가장 최근 120건'이라는 전제가 깨집니다. 창을
    시간으로 환산하는 계산은 전부 그 전제 위에 서 있으므로, 먼저 이것부터 봐야 합니다.
    """
    times = [listing_created_at(fields) for fields in fields_list]
    inversions = 0
    previous = None
    for value in times:
        if value is None:
            continue
        if previous is not None and value > previous:
            inversions += 1
        previous = value
    return inversions


def newer_beyond_window(fields_list: list, window: int = 0) -> int:
    """창 밖인데 창 안 맨 뒤보다 더 최근인 매물 수. 정렬이 지켜졌다면 0입니다."""
    window = window or MAX_ITEMS_PER_KEYWORD
    inside, outside = fields_list[:window], fields_list[window:]
    tail = [t for t in (listing_created_at(f) for f in inside) if t is not None]
    if not tail:
        return 0
    boundary = min(tail)
    return sum(1 for f in outside if (listing_created_at(f) or 0) > boundary)


def peak_arrivals(fields_list: list, seconds: float) -> int:
    """등록 시각이 같은 구간(seconds)에 가장 많이 몰린 개수.

    한 번의 실행이 받아내야 하는 양입니다. 평균으로 보면 넉넉해 보여도 몰릴 때
    몰리므로, 창을 줄여도 되는지는 평균이 아니라 이 최대치로 판단해야 합니다.
    """
    times = sorted(t for t in (listing_created_at(f) for f in fields_list) if t is not None)
    best = start = 0
    for end in range(len(times)):
        while times[end] - times[start] > seconds:
            start += 1
        best = max(best, end - start + 1)
    return best


def missed_ages(fields_list: list, seen_ids: set, fresh_after: float, now: float) -> list:
    """상태 파일에 없는 최근 매물이 '올라온 지 몇 분 됐는지'를 오래된 순으로.

    갓 올라온 매물이 아직 상태 파일에 없는 것은 결함이 아닙니다 — 다음 실행(1분 뒤)에
    잡습니다. '못 본 것 1건'이 진짜 누락인지 조회 타이밍인지는 이 나이를 봐야 갈립니다.
    """
    ages = [
        age_minutes(fields, now)
        for fields in fields_list
        if (listing_created_at(fields) or 0) >= fresh_after
        and extract_item_id(fields) not in seen_ids
    ]
    return sorted((age for age in ages if age is not None), reverse=True)


def format_span(minutes: float | None) -> str:
    """분을 읽기 쉬운 단위로. 창 깊이는 분·시간·일이 뒤섞여 나옵니다."""
    if minutes is None:
        return "창이 남음"
    if minutes < 90:
        return f"{minutes:.0f}분치"
    if minutes < 60 * 48:
        return f"{minutes / 60:.1f}시간치"
    return f"{minutes / 60 / 24:.1f}일치"


async def scan(pages: int, fresh_hours: float) -> None:
    from mercapi import Mercapi

    api = Mercapi()
    seen_ids = load_seen_ids()
    now = datetime.now().timestamp()
    fresh_after = now - fresh_hours * 3600
    print(
        f"봇이 보는 창: 키워드·정렬당 상위 {MAX_ITEMS_PER_KEYWORD}건 / "
        f"이번 조회 깊이: {pages}페이지({pages * MAX_ITEMS_PER_KEYWORD}건) / "
        f"상태 파일 {len(seen_ids):,}건 / '최근'의 기준: {fresh_hours}시간"
    )
    print()

    # 같은 브랜드의 한/영 키워드가 같은 매물을 잡으므로, 합계는 매물 ID 기준으로
    # 중복을 걷어내고 셉니다(키워드별 숫자를 그냥 더하면 두 배로 부풀어 보입니다).
    unique = {"window_missed": set(), "filter_missed": set(), "fresh": set()}
    excluded_all: list = []
    rows = []
    for search in SEARCHES:
        keyword, categories = search["query"], search["categories"]
        filtered, found = await walk(api, keyword, categories, pages)
        await asyncio.sleep(PAGE_PAUSE_SECONDS)

        inside, outside = split_by_window(filtered)
        fresh_in, missed_in = count_missed(inside, seen_ids, fresh_after)
        fresh_out, missed_out = count_missed(outside, seen_ids, fresh_after)
        depth = window_depth(filtered, now)
        inversions = order_inversions(filtered)
        beyond_newer = newer_beyond_window(filtered)
        ages = missed_ages(filtered, seen_ids, fresh_after, now)

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
            fresh_excluded = fresh_only(excluded, fresh_after)
            excluded_all.extend(fresh_excluded)
            unique["filter_missed"].update(
                extract_item_id(f) for f in fresh_excluded if extract_item_id(f) not in seen_ids
            )

        unique["window_missed"].update(
            extract_item_id(f) for f in fresh_only(outside, fresh_after)
            if extract_item_id(f) not in seen_ids
        )
        unique["fresh"].update(
            extract_item_id(f) for f in fresh_only(filtered, fresh_after)
        )
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
                "depth": depth,
                "inversions": inversions,
                "beyond_newer": beyond_newer,
                "peak_1m": peak_arrivals(filtered, 60),
                "peak_5m": peak_arrivals(filtered, 5 * 60),
                "missed_ages": ages,
            }
        )
        # API가 말하는 전체 건수(num_found)는 실제로 받은 개수와 어긋납니다. 2026-09-14
        # 실측에서 'Hermes Margiela 전체 149건'이라면서 585건을 돌려줬고, YSL은 필터를
        # 걸었는데 안 건 쪽보다 큰 값이 나왔습니다(318 > 137). 걸러내기만 하는 필터로는
        # 불가능한 값입니다. 그래서 이 값은 참고로만 적고, 판단은 실제로 받은 항목과
        # 상태 파일 대조로만 합니다.
        mismatch = " ※전체 건수 표기가 실제와 어긋남" if len(filtered) > found > 0 else ""
        print(
            f"[{keyword}] API 표기 {found:,}건{mismatch}"
            + (f" (필터 없으면 {unfiltered_found:,}건)" if unfiltered_found is not None else "")
            + f" / 훑은 {len(filtered)}건"
            f" | 창 안 최근 {fresh_in}건 중 못 본 것 {missed_in}건"
            f" | 창 밖 최근 {fresh_out}건 중 못 본 것 {missed_out}건"
            + (f" | 필터가 뺀 최근 {filter_fresh}건 중 못 본 것 {filter_missed}건" if categories else "")
        )
        print(
            "   └ 창 깊이: "
            + " / ".join(f"{size}건={format_span(minutes)}" for size, minutes in depth)
            + f" | 가장 몰렸을 때 1분 {rows[-1]['peak_1m']}건·5분 {rows[-1]['peak_5m']}건"
            + (f" | 등록순 역전 {inversions}건(창 밖이 창 안보다 최근인 것 {beyond_newer}건)"
               if inversions else "")
            + (f" | 못 본 것 나이 {max(ages[0], 0):.0f}~{max(ages[-1], 0):.0f}분" if ages else "")
        )

    print()
    print("=" * 78)
    print(
        f"합계(매물 ID 기준 중복 제거): 최근 {fresh_hours}시간 매물 {len(unique['fresh']):,}건을 확인했고,\n"
        f"  창(상위 {MAX_ITEMS_PER_KEYWORD}건) 밖이라 못 본 것   {len(unique['window_missed']):,}건\n"
        f"  카테고리 필터가 빼서 못 본 것      {len(unique['filter_missed']):,}건"
    )
    print(
        "\n※ '필터가 빼서 못 본 것'은 그 자체로는 결함이 아닙니다 — 필터는 원래 빼라고\n"
        "   걸어 둔 것이라 100%가 나올 수밖에 없습니다. 아래 '무엇이 빠지는가'를 보고\n"
        "   원하던 매물이 빠지는지 판단하세요."
    )

    worst_window = sorted(rows, key=lambda r: -r["beyond_missed"])[:5]
    worst_filter = sorted(rows, key=lambda r: -r["filter_missed"])[:5]
    print("\n창 밖이라 놓친 것이 많은 키워드:")
    for row in worst_window:
        print(f"   {row['keyword']:<34} {row['beyond_missed']:>5}건")
    print("\n카테고리 필터가 빼는 것이 많은 키워드:")
    for row in worst_filter:
        print(f"   {row['keyword']:<34} {row['filter_missed']:>5}건 (카테고리 {row['categories']})")

    print("\n필터가 빼는 것이 '무엇인가' (최근 등록분, 카테고리별):")
    seen_sample = {extract_item_id(f) for f in excluded_all}
    print(f"   대상 {len(seen_sample):,}건")
    for category, count, names in category_breakdown(excluded_all, top=8):
        print(f"   카테고리 {str(category):<8} {count:>5}건   예: {' / '.join(names)}")

    report_window_sizes(rows)
    print("완료")


def report_window_sizes(rows: list) -> None:
    """창을 줄여도 되는지 판단할 근거를 한자리에 모읍니다.

    '1분마다 도는데 매번 120건까지 볼 필요가 있나'라는 질문의 답은 건수가 아니라
    **시간 깊이**에 있습니다. 창을 줄이면 조회가 가벼워지는 것이 아니라(페이지 크기는
    라이브러리가 120으로 고정), 거슬러 볼 수 있는 시간이 짧아집니다. 그 시간이 봇이
    메꿔야 하는 구간(정상 1분, 긴 정지 뒤 최대 24시간)보다 짧아지면 그때 놓칩니다.
    """
    sizes = [size for size, _ in (rows[0]["depth"] if rows else [])]
    print("\n창을 줄여도 되는가 (건수가 아니라 '시간 깊이'로 봅니다):")
    print(
        "   ※ 페이지 크기는 mercapi가 120으로 고정해 보냅니다. 상한을 낮춰도 120건을\n"
        "      받아 온 뒤 잘라낼 뿐이라 호출 수도 트래픽도 줄지 않습니다. 달라지는 것은\n"
        "      '얼마나 거슬러 볼 수 있는가' 하나뿐입니다."
    )
    header = "   " + f"{'키워드':<26}" + "".join(f"{str(size) + '건':>14}" for size in sizes)
    print(header)
    for row in sorted(rows, key=lambda r: depth_sort_key(r)):
        cells = "".join(f"{format_span(minutes):>14}" for _, minutes in row["depth"])
        print(f"   {row['keyword']:<26}{cells}")
    print(
        f"\n   봇이 메꿔야 하는 구간: 정상 실행 간격 1분"
        f" / 전체 조회 {FULL_SCAN_INTERVAL_SECONDS // 60}분"
        f" / 긴 정지 뒤 최대 {MAX_LOOKBACK_SECONDS // 3600}시간"
    )
    worst = min(rows, key=depth_sort_key, default=None)
    if worst is not None:
        shallow = ", ".join(
            f"{size}건={format_span(minutes)}" for size, minutes in worst["depth"]
        )
        print(f"   가장 얕은 키워드: [{worst['keyword']}] {shallow}")


def depth_sort_key(row: dict) -> float:
    """창이 가장 얕은(=빨리 차는) 키워드부터 오도록. 창이 남는 키워드는 맨 뒤로."""
    last = row["depth"][-1][1] if row.get("depth") else None
    return float("inf") if last is None else last


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pages", type=int, default=5, help="키워드당 훑을 페이지 수(1페이지=120건)")
    parser.add_argument("--fresh-hours", type=float, default=24.0, help="'최근 등록'으로 볼 시간")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(scan(args.pages, args.fresh_hours))
