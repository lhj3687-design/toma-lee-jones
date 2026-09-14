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

정렬 패스가 둘이고, **다른 질문에 답합니다.** 그래서 집계를 합치지 않습니다.

  - `created`(등록순, 매 실행) — "새로 올라온 매물을 놓쳤는가."
  - `score`(추천순, 전체 조회에서만) — "이미 추적 중인 매물이 아직 보이는가."
    재출품 판정이 '예전 매물이 이번 결과에 없으면 사라졌다'로 읽기 때문에,
    추적 중인 매물이 이 창 밖에 있으면 살아 있어도 '사라졌다'가 됩니다.

읽기만 합니다 — 상태 파일을 건드리지 않고 알림도 보내지 않습니다.

    python scripts/coverage_scan.py --pages 5 --fresh-hours 24 --sort both

**창 깊이를 재는 방법에는 전제가 있습니다.** '창 밖으로 밀려난 것 중 가장 최근 매물의
나이'는 새 매물이 맨 앞으로 들어와 뒤로만 밀린다는 **컨베이어 가정** 위에 서 있습니다.
등록순에서는 서지만 추천순에서는 설 이유가 없습니다. 그래서 이 도구는 가정이 깨진 것을
스스로 잡아내고(최근 매물이 창 밖까지 꽂히면 표에 ⛔를 답니다), 가정을 쓰지 않는 자를
따로 들고 있습니다 — 같은 창을 시간을 두고 반복해 찍는 종단 측정입니다.

    python scripts/coverage_scan.py --sort both --snapshots 12 --interval 150 \
        --pages 2 --keywords "Margiela,Chrome Hearts"

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
    MERCARI_ITEM_ID_PATTERN,
    MAX_ITEMS_PER_KEYWORD,
    MAX_LOOKBACK_SECONDS,
    SEARCHES,
    SEARCH_SORT_OPTIONS,
    SEEN_FILE,
    extract_item_id,
    extract_seller_id,
    item_fields,
    listing_created_at,
)

PAGE_PAUSE_SECONDS = 1.0  # 봇이 페이지 사이에 두는 간격과 같게 둡니다.


def load_state() -> tuple[set, dict]:
    """봇이 기록한 매물 ID와, 키워드별로 마지막으로 조회한 시각.

    조회 시각이 왜 필요한가: 이 스캔은 **체크아웃된 상태 파일**과 **지금 메루카리**를
    맞대 봅니다. 둘 사이에는 항상 시차가 있습니다(브랜치에서 돌리면 그 브랜치를 딴
    시점에 멈춰 있고, main에서 돌려도 체크아웃 이후 시간이 흐릅니다). 그 시차 안에
    올라온 매물은 상태 파일에 없는 게 **당연합니다** — 봇이 아직 볼 기회가 없었으니까요.
    그걸 '못 봄'으로 세면 스캔을 늦게 돌릴수록 결함이 늘어나는 엉터리 숫자가 됩니다.

    실제로 2026-09-14 측정에서 이것 때문에 '창 안 못 본 것 8건'이 나왔습니다. 상태 파일은
    06:43, 스캔은 07:03이었고, 여덟 건 전부 그 사이에 올라온 매물이었습니다.
    """
    if not SEEN_FILE.exists():
        print(f"[경고] {SEEN_FILE}가 없습니다. 전부 '처음 보는 매물'로 집계됩니다", file=sys.stderr)
        return set(), {}
    data = json.loads(SEEN_FILE.read_text())
    seen = data.get("seen") or {}
    seen_ids = set(seen if isinstance(seen, dict) else {str(item): None for item in seen})
    checked_at = data.get("keyword_checked_at") or {}
    if not isinstance(checked_at, dict):
        checked_at = {}
    return seen_ids, checked_at


async def walk(
    api, keyword: str, categories: list, pages: int, sort: str = "created"
) -> tuple[list, int]:
    """한 키워드를 주어진 정렬로 pages 페이지까지 훑어 (필드 목록, 전체 건수)를 돌려줍니다.

    돌려주는 목록의 순서가 곧 순위입니다 — 봇은 이 중 앞 120건만 봅니다.

    `sort`는 봇이 실제로 쓰는 두 패스입니다(`check_mercari.build_search_options`).

      - created: 등록순. 매 실행 돕니다. 새 매물을 책임집니다.
      - score:   추천순. 전체 조회(5분)에서만 돕니다. **오래 올라와 있는 매물의 가격
                 인하를 계속 보는 것이 이쪽 일**이라, 재출품 판정이 기대는 '예전 매물이
                 아직 살아 있는가'도 사실상 이 패스가 답합니다.
    """
    options = SEARCH_SORT_OPTIONS.get(sort, {})
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


def count_missed(
    fields_list: list, seen_ids: set, fresh_after: float, checked_at: float | None = None
) -> tuple[int, int, int]:
    """(최근 등록분, 진짜 못 본 것, 상태 파일 스냅숏 이후에 올라온 것).

    `checked_at`은 봇이 이 키워드를 마지막으로 조회한 시각입니다. 그보다 **뒤에** 올라온
    매물은 상태 파일에 있을 수가 없으므로 '못 봄'이 아닙니다. 넘기지 않으면 예전처럼
    전부 '못 봄'으로 셉니다.
    """
    fresh = [
        fields
        for fields in fields_list
        if (listing_created_at(fields) or 0) >= fresh_after
    ]
    unseen = [fields for fields in fresh if extract_item_id(fields) not in seen_ids]
    if checked_at is None:
        return len(fresh), len(unseen), 0
    after = [f for f in unseen if (listing_created_at(f) or 0) > checked_at]
    return len(fresh), len(unseen) - len(after), len(after)


def age_minutes(fields: dict, now: float) -> float | None:
    """이 매물이 올라온 지 몇 분 됐는지 (등록 시각을 모르면 None)."""
    created = listing_created_at(fields)
    return None if created is None else (now - created) / 60.0


def window_dwell(fields_list: list, now: float, sizes: tuple = ()) -> list:
    """창 N건에 매물이 **얼마나 머무는지**를 분으로 돌려줍니다.

    창을 건수로만 보면 120은 그냥 큰 숫자입니다. 봇에게 의미가 있는 것은 시간입니다 —
    새 매물이 올라올수록 기존 매물은 뒤로 밀리므로, '창 밖으로 밀려난 것 중 가장 최근
    매물의 나이'가 곧 매물이 창 안에 남아 있는 시간입니다. 봇이 그 시간 안에 한 번만
    돌면 놓치지 않습니다.

    **순위 N번째 매물의 나이로 재면 안 됩니다.** 처음에 그렇게 쟀다가 'Hermes 30건=2.1시간치
    / 60건=16.7일치 / 120건=15.5시간치'처럼 깊이가 들쭉날쭉한 표가 나왔습니다. 창을 넓혔는데
    깊이가 얕아질 수는 없으니 도구가 틀린 것이고, 원인은 등록순 결과에 예전 매물이 섞여
    들어오기 때문입니다(2026-09-14 실측: 598건 중 역전 253건). 그 자리에 예전 매물이 앉아
    있으면 값이 통째로 널뜁니다. 아래 방식은 밖으로 밀려난 쪽의 **최솟값**을 보므로 섞여
    들어온 예전 매물에 흔들리지 않습니다.

    돌려주는 값은 (창 크기, 분) 목록입니다. 창 밖에 아무것도 없으면(창이 남으면) None입니다 —
    그 키워드에서는 창이 제약이 아니라는 뜻입니다.
    """
    sizes = sizes or (30, 60, MAX_ITEMS_PER_KEYWORD)
    profile = []
    for size in sizes:
        ages = [age_minutes(fields, now) for fields in fields_list[size:]]
        ages = [age for age in ages if age is not None]
        profile.append((size, min(ages) if ages else None))
    return profile


def oldest_in_window(fields_list: list, now: float, window: int = 0) -> float | None:
    """창 안에서 가장 오래된 매물의 나이(분).

    봇의 '다음 페이지도 본다'(wants_another_page)는 **페이지 전체가 기준선 이후
    등록분일 때만** 발동합니다. 즉 이 값이 조회 간격보다 작아야 발동할 수 있습니다.
    등록순 결과에 예전 매물이 섞여 들어오면 이 값이 며칠 단위가 되고, 그러면 그
    장치는 영영 발동하지 않습니다. 그게 문제인지 아닌지는 '가장 몰렸을 때' 값과
    함께 봐야 합니다 — 한 번에 120건 넘게 쏟아지지 않는다면 발동할 필요도 없습니다.
    """
    window = window or MAX_ITEMS_PER_KEYWORD
    ages = [age_minutes(fields, now) for fields in fields_list[:window]]
    ages = [age for age in ages if age is not None]
    return max(ages) if ages else None


def field_time(fields: dict, key: str) -> float | None:
    """dict에서 시각 필드 하나를 epoch 초로 꺼냅니다(datetime이든 숫자든)."""
    value = fields.get(key)
    if isinstance(value, datetime):
        try:
            return value.timestamp()
        except Exception:
            return None
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    return None


def order_inversions(fields_list: list, key: str = "created") -> int:
    """조회 결과가 정말 그 시각의 내림차순인지 셉니다(바로 앞보다 최근인 항목 수).

    `created`로 셌을 때 0이 아니면 '앞 120건 = 가장 최근 120건'이라는 전제가 깨집니다.
    창을 시간으로 환산하는 계산은 전부 그 전제 위에 서 있으므로, 먼저 이것부터 봐야
    합니다. `updated`로도 함께 세는 이유는 원인을 가리기 위해서입니다 — 이쪽이 0에
    가깝다면 메루카리의 '새로운 순'은 등록 시각이 아니라 **마지막 수정 시각** 순이고,
    앞자리에 앉아 있는 예전 매물은 '다시 올라온' 것이라는 뜻입니다.
    """
    times = [field_time(fields, key) for fields in fields_list]
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


def young_item_ranks(fields_list: list, now: float, max_age_minutes: float = 60.0) -> list:
    """갓 올라온 매물이 **몇 등에 꽂히는지**. 창 깊이 계산의 전제를 검사하는 자입니다.

    '창 밖으로 밀려난 것 중 가장 최근 매물의 나이'로 깊이를 재는 방법은 **컨베이어
    가정** 위에 서 있습니다 — 새 매물은 맨 앞(0등)에 들어오고, 뒤에서 밀려 나가기만
    한다는 가정입니다. 그 가정이 맞으면 '밖으로 밀려난 것 중 가장 어린 것의 나이'가
    곧 '창을 한 번 통과하는 데 걸리는 시간'입니다.

    등록순에서는 이 가정이 대체로 맞습니다(예전 매물이 섞여 들어와도 **새 매물**은
    여전히 앞자리에 들어옵니다). 추천순에서는 맞을 이유가 없습니다 — 순위를 만드는
    것이 시각이 아니기 때문입니다. 맞는지 아닌지는 **재 보고** 판단합니다.

    돌려주는 값은 `max_age_minutes`보다 어린 매물들의 순위 목록입니다. 컨베이어라면
    이 값들이 0 근처에 몰립니다.
    """
    ranks = []
    for rank, fields in enumerate(fields_list):
        age = age_minutes(fields, now)
        if age is not None and age <= max_age_minutes:
            ranks.append(rank)
    return ranks


def conveyor_holds(ranks: list, window: int = 0) -> bool:
    """갓 올라온 매물이 전부 창 안 앞쪽에 들어오는가 (컨베이어 가정이 서는가).

    하나라도 창 밖에 꽂히면 '새 매물은 맨 앞으로 들어온다'가 깨진 것이고, 그러면
    window_dwell()이 돌려주는 값은 '창을 통과하는 시간'이 아닙니다.
    """
    window = window or MAX_ITEMS_PER_KEYWORD
    return bool(ranks) and max(ranks) < window


def dwell_grows_with_the_window(dwell: list) -> bool | None:
    """창을 넓혔는데 깊이가 그대로면 그 값은 깊이가 아닙니다.

    컨베이어에서는 창 크기와 체류 시간이 **비례**합니다 — 창이 30건에서 120건으로
    넓어지면 매물이 밖으로 밀려나기까지 4배 오래 걸립니다. 그래서 30 < 60 < 120은
    반드시 성립합니다.

    2026-09-14 추천순 실측에서는 거의 모든 키워드가 `30건=60건=120건`으로 **똑같이**
    나왔습니다(마르지엘라 2분 / 크롬하츠 7분 / 더로우 54분). 컨베이어라면 나올 수 없는
    모양이고, 같은 표의 등록순 쪽은 37분 → 68분 → 2.0시간으로 제대로 자랍니다.
    PR #30에서 '비단조인 표'가 방법이 틀렸다는 신호였던 것과 같은 자리입니다 —
    이번에는 **평평한 표**가 그 신호입니다.

    창이 남는 키워드(값이 None)는 판단하지 않고 None을 돌려줍니다.
    """
    values = [minutes for _, minutes in dwell]
    if any(value is None for value in values) or len(values) < 2:
        return None
    return all(earlier < later for earlier, later in zip(values, values[1:]))


def predicted_survival(dwell_minutes: float | None, lag_minutes: float) -> float | None:
    """창 깊이 D가 맞다면 lag 분 뒤에 창에 남아 있을 비율 (컨베이어 가정의 예측).

    컨베이어에서는 유입 속도 r = 창크기 / D 이고, lag 동안 r*lag 건이 들어와 같은 수가
    밀려 나갑니다. 즉 남는 비율은 1 - lag/D 입니다. **이 예측을 실제 관측과 맞대 보는
    것이 이 도구의 검산입니다** — 등록순에서 맞고 추천순에서 틀리면, 그 방법을
    추천순에 그대로 쓰면 안 된다는 뜻입니다.
    """
    if dwell_minutes is None or dwell_minutes <= 0:
        return None
    return max(0.0, 1.0 - lag_minutes / dwell_minutes)


def survival_curve(snapshots: list, window: int = 0) -> dict:
    """스냅숏 묶음에서 '창에 실제로 얼마나 머무는가'를 종단으로 잽니다.

    snapshots는 (찍은 시각, 순위대로 늘어놓은 매물 ID 목록, 그때의 한 장짜리 추정)
    목록입니다. 한 장만 보고 추정하는 window_dwell()과 달리, 이쪽은 **같은 창을 시간을
    두고 다시 찍어** 누가 남았는지 직접 셉니다. 정렬 방식이 무엇이든(시간순이 아니어도)
    뜻이 흔들리지 않는 것이 이 방법의 요점입니다.

    간격은 스냅숏 **번호 차이**로 묶습니다. 실제 걸린 시간으로 묶으면 조회가 느린 판과
    빠른 판이 다른 칸에 떨어져 같은 간격이 쪼개집니다.

    돌려주는 값:
      lags      : [(몇 칸 뒤, 평균 간격(분), 남아 있는 비율)]
      reentry   : 창 밖으로 나갔다가 **다시 들어온** 매물 수. 봇 입장에서 '보였다
                  안 보였다' 하는 양입니다. **정렬을 가르는 잣대는 아닙니다** —
                  2026-09-14 실측에서 등록순도 4~8건이 나왔습니다(창 경계에서
                  순위가 떠는 것이 자연스럽습니다). 크기로 읽으세요: 같은 35분에
                  Margiela가 등록순 7건, 추천순 **33건**입니다.
      displaced : 창에서 빠졌지만 훑은 범위 안에는 아직 있는 것(순위에 밀려난 것)
      vanished  : 훑은 범위에서 통째로 사라진 것(팔렸거나 훨씬 뒤로 밀림)
    """
    window = window or MAX_ITEMS_PER_KEYWORD
    buckets: dict = {}
    for i in range(len(snapshots)):
        t_i, order_i, _ = snapshots[i]
        inside_i = set(order_i[:window])
        if not inside_i:
            continue
        for j in range(i + 1, len(snapshots)):
            t_j, order_j, _ = snapshots[j]
            bucket = buckets.setdefault(j - i, [0, 0, 0.0])
            bucket[0] += len(inside_i & set(order_j[:window]))
            bucket[1] += len(inside_i)
            bucket[2] += (t_j - t_i) / 60.0
    lags = [
        (step, total_lag / max(1, sum(1 for i in range(len(snapshots) - step))), kept / total)
        for step, (kept, total, total_lag) in sorted(buckets.items())
        if total
    ]

    reentry = 0
    displaced = vanished = 0
    first_window = set(snapshots[0][1][:window]) if snapshots else set()
    outside_once: set = set()
    for index, (_, order, _) in enumerate(snapshots):
        inside = set(order[:window])
        if index:
            reentry += len(outside_once & inside)
            outside_once -= inside
            gone = first_window - inside
            outside_once |= gone
            if index == len(snapshots) - 1:
                everything = set(order)
                displaced = len([item for item in gone if item in everything])
                vanished = len(gone) - displaced
    return {"lags": lags, "reentry": reentry, "displaced": displaced,
            "vanished": vanished, "window_size": len(first_window)}


def missed_ages(
    fields_list: list,
    seen_ids: set,
    fresh_after: float,
    now: float,
    checked_at: float | None = None,
) -> list:
    """상태 파일에 없는 최근 매물이 '올라온 지 몇 분 됐는지'를 오래된 순으로.

    갓 올라온 매물이 아직 상태 파일에 없는 것은 결함이 아닙니다 — 다음 실행(1분 뒤)에
    잡습니다. '못 본 것 1건'이 진짜 누락인지 조회 타이밍인지는 이 나이를 봐야 갈립니다.
    """
    ages = [
        age_minutes(fields, now)
        for fields in fields_list
        if (listing_created_at(fields) or 0) >= fresh_after
        and extract_item_id(fields) not in seen_ids
        and (checked_at is None or (listing_created_at(fields) or 0) <= checked_at)
    ]
    return sorted((age for age in ages if age is not None), reverse=True)


def format_span(minutes: float | None) -> str:
    """분을 읽기 쉬운 단위로. 창 깊이는 분·시간·일이 뒤섞여 나옵니다.

    음수는 **잰 방법이 틀렸다는 뜻**이라 그렇게 찍습니다. 매물이 조회보다 나중에
    올라올 수는 없으므로, 나이가 음수라면 기준 시각이 조회보다 앞선 것입니다.
    실제로 2026-09-14에 그 일이 났습니다 — 스캔 시작 시각 하나로 모든 패스의 나이를
    쟀는데, 추천순 패스는 그보다 몇 분 뒤에 돌기 때문에 '-3분치' 같은 값이 나왔습니다.
    그대로 뒀으면 '추천순 창은 3분치'라는 그럴듯한 오답이 표에 실렸을 것입니다.
    """
    if minutes is None:
        return "창이 남음"
    if minutes < 0:
        return "⛔잰 방법 틀림"
    if minutes < 90:
        return f"{minutes:.0f}분치"
    if minutes < 60 * 48:
        return f"{minutes / 60:.1f}시간치"
    return f"{minutes / 60 / 24:.1f}일치"


def sort_options_are_distinct() -> tuple[bool, str]:
    """두 정렬 패스가 정말 **서로 다른 조회**인지 확인합니다.

    이 확인이 없으면 이 도구는 조용히 거짓말을 합니다. `build_search_options()`는
    mercapi를 import하지 못하면 예외를 삼키고 `{"created": {}, "score": {}}`로
    물러납니다(봇이 죽지 않게 하려는 설계입니다). 그러면 두 패스 모두 **옵션 없는
    같은 조회**가 되는데, mercapi의 기본 정렬이 `SORT_SCORE`라서 결과는 둘 다
    추천순입니다. 그 상태로 표를 찍으면 '등록순 대 추천순'이라는 이름만 붙은 같은
    숫자 두 벌이 나오고, "추천순도 등록순과 똑같더라"는 결론이 나옵니다.

    실제로 이 저장소에서 값진 발견은 전부 '도구가 문제를 잡을 수 있는지'를 먼저
    본 자리에서 나왔습니다. 그래서 여기서 막습니다.
    """
    created = SEARCH_SORT_OPTIONS.get("created") or {}
    score = SEARCH_SORT_OPTIONS.get("score") or {}
    if not created or not score:
        return False, "검색 옵션이 비어 있습니다 (build_search_options가 기본 검색으로 물러났습니다)"
    if created.get("sort_by") == score.get("sort_by"):
        return False, f"두 패스의 sort_by가 같습니다 ({created.get('sort_by')!r})"
    return True, ""


SORT_LABELS = {"created": "등록순", "score": "추천순"}


def sort_label(sort: str) -> str:
    return SORT_LABELS.get(sort, sort)


async def census(
    api,
    sort: str,
    pages: int,
    fresh_hours: float,
    seen_ids: set,
    keyword_checked_at: dict,
    check_filter: bool,
    track: set | None = None,
    sellers: set | None = None,
) -> tuple[list, dict, list, dict]:
    """한 정렬 패스로 전 키워드를 훑어 집계합니다.

    등록순과 추천순은 **다른 질문에 답합니다.** 그래서 합치지 않고 따로 셉니다.

      - 등록순: "새로 올라온 매물을 놓쳤는가." 창 밖으로 밀려나기 전에 한 번 지나가면
        되므로, 판단 기준은 '창에 머무는 시간 > 봇이 한 바퀴 도는 간격'입니다.
      - 추천순: "이미 추적 중인 매물이 아직 보이는가." 재출품 판정이 '예전 매물이
        이번 결과에 없다'로 사라짐을 재기 때문에, 추적 중인 매물이 이 창 밖에 있으면
        살아 있어도 '사라졌다'로 보입니다. 그래서 이쪽은 **seen 적중률**을 셉니다.
    """
    rows, excluded_all = [], []
    track = track or set()
    sellers = sellers or set()
    found_at: dict = {}      # 쫓는 매물 -> [(키워드, 순위, 훑은 건수)]
    unique = {"window_missed": set(), "filter_missed": set(), "fresh": set(),
              "window_ids": set(), "tracked_in_window": set(),
              "tracked_scanned": set()}
    for search in SEARCHES:
        keyword, categories = search["query"], search["categories"]
        filtered, found = await walk(api, keyword, categories, pages, sort)
        # 나이는 **이 키워드를 조회하고 난 시각**으로 잽니다. 스캔 시작 시각 하나로
        # 전부 재면, 뒤에 도는 패스일수록 기준이 과거라 나이가 음수로 나옵니다.
        scanned_at = datetime.now().timestamp()
        fresh_after = scanned_at - fresh_hours * 3600
        await asyncio.sleep(PAGE_PAUSE_SECONDS)

        inside, outside = split_by_window(filtered)
        checked_at = keyword_checked_at.get(keyword)
        checked_at = float(checked_at) if isinstance(checked_at, (int, float)) else None
        fresh_in, missed_in, after_in = count_missed(inside, seen_ids, fresh_after, checked_at)
        fresh_out, missed_out, after_out = count_missed(outside, seen_ids, fresh_after, checked_at)
        dwell = window_dwell(filtered, scanned_at)
        inversions = order_inversions(filtered)
        inversions_updated = order_inversions(filtered, "updated")
        young_ranks = young_item_ranks(filtered, scanned_at)
        ages = missed_ages(filtered, seen_ids, fresh_after, scanned_at, checked_at)

        # 추적 중인 매물이 이 창 안에 있는가. 재출품 판정이 기대는 것이 바로 이것입니다.
        tracked_in = sum(1 for f in inside if extract_item_id(f) in seen_ids)
        tracked_out = sum(1 for f in outside if extract_item_id(f) in seen_ids)

        if track:
            for rank, fields in enumerate(filtered):
                item_id = extract_item_id(fields)
                if item_id in track:
                    found_at.setdefault(item_id, []).append(
                        (keyword, rank, len(filtered))
                    )

        # 특정 판매자의 매물이 창 안에 얼마나 들어오는가. 재출품 오판 36건 중 32건이
        # 판매자 한 명(119903670)에게서 나왔습니다 — 정규화한 제목이 브랜드+품목+정형구
        # 뿐이라 같은 판매자의 서로 다른 매물이 전부 같은 지문이 되기 때문입니다.
        # 그 판매자의 매물이 창 밖에 주로 앉아 있다면, '이번 결과에 없다'가 그 매물들에
        # 대해서는 **평소 상태**라는 뜻입니다.
        seller_in = seller_out = 0
        if sellers:
            for rank, fields in enumerate(filtered):
                if extract_seller_id(fields) in sellers:
                    if rank < MAX_ITEMS_PER_KEYWORD:
                        seller_in += 1
                    else:
                        seller_out += 1

        filter_missed = filter_fresh = 0
        unfiltered_found = None
        if categories and check_filter:
            unfiltered, unfiltered_found = await walk(api, keyword, [], pages, sort)
            await asyncio.sleep(PAGE_PAUSE_SECONDS)
            excluded = excluded_by_filter(filtered, unfiltered)
            filter_fresh, filter_missed, _ = count_missed(
                excluded, seen_ids, fresh_after, checked_at
            )
            fresh_excluded = fresh_only(excluded, fresh_after)
            excluded_all.extend(fresh_excluded)
            unique["filter_missed"].update(
                extract_item_id(f) for f in fresh_excluded
                if extract_item_id(f) not in seen_ids
                and (checked_at is None or (listing_created_at(f) or 0) <= checked_at)
            )

        unique["window_missed"].update(
            extract_item_id(f) for f in fresh_only(outside, fresh_after)
            if extract_item_id(f) not in seen_ids
            and (checked_at is None or (listing_created_at(f) or 0) <= checked_at)
        )
        unique["fresh"].update(extract_item_id(f) for f in fresh_only(filtered, fresh_after))
        unique["window_ids"].update(extract_item_id(f) for f in inside)
        unique["tracked_in_window"].update(
            extract_item_id(f) for f in inside if extract_item_id(f) in seen_ids
        )
        unique["tracked_scanned"].update(
            extract_item_id(f) for f in filtered if extract_item_id(f) in seen_ids
        )
        rows.append(
            {
                "sort": sort,
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
                "dwell": dwell,
                "inversions": inversions,
                "inversions_updated": inversions_updated,
                "oldest_in_window": oldest_in_window(filtered, scanned_at),
                "beyond_newer": newer_beyond_window(filtered),
                "peak_1m": peak_arrivals(filtered, 60),
                "peak_5m": peak_arrivals(filtered, 5 * 60),
                "missed_ages": ages,
                "young_ranks": young_ranks,
                "conveyor": conveyor_holds(young_ranks),
                "tracked_in": tracked_in,
                "tracked_out": tracked_out,
                "seller_in": seller_in,
                "seller_out": seller_out,
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
            + (f"(+상태 파일 이후 등록 {after_in}건)" if after_in else "")
            + f" | 창 밖 최근 {fresh_out}건 중 못 본 것 {missed_out}건"
            + (f"(+상태 파일 이후 등록 {after_out}건)" if after_out else "")
            + (f" | 필터가 뺀 최근 {filter_fresh}건 중 못 본 것 {filter_missed}건"
               if categories and check_filter else "")
        )
        print(
            f"   └ 추적 중(seen) 매물: 창 안 {tracked_in}건 / 창 밖 {tracked_out}건"
            + f" | 창에 머무는 시간: "
            + " / ".join(f"{size}건={format_span(minutes)}" for size, minutes in dwell)
            + f" | 가장 몰렸을 때 1분 {rows[-1]['peak_1m']}건·5분 {rows[-1]['peak_5m']}건"
        )
        print(
            f"   └ 등록 시각 역전 {inversions}건 · 수정 시각 역전 {inversions_updated}건"
            f" | 최근 1시간 매물이 꽂힌 순위: "
            + (f"{min(young_ranks)}~{max(young_ranks)}등({len(young_ranks)}건)"
               if young_ranks else "없음")
            + (" ✅창 안" if young_ranks and rows[-1]["conveyor"] else
               (" ⚠️창 밖까지" if young_ranks else ""))
            + (f" | 못 본 것 나이 {max(ages[0], 0):.0f}~{max(ages[-1], 0):.0f}분" if ages else "")
        )
    return rows, unique, excluded_all, found_at


def report_missed(unique: dict, rows: list, fresh_hours: float, sort: str,
                  check_filter: bool) -> None:
    print()
    print("-" * 78)
    print(
        f"[{sort_label(sort)}] 합계(매물 ID 기준 중복 제거):"
        f" 최근 {fresh_hours}시간 매물 {len(unique['fresh']):,}건을 확인했고,\n"
        f"  창(상위 {MAX_ITEMS_PER_KEYWORD}건) 밖이라 못 본 것   {len(unique['window_missed']):,}건"
        + (f"\n  카테고리 필터가 빼서 못 본 것      {len(unique['filter_missed']):,}건"
           if check_filter else "")
    )
    worst_window = sorted(rows, key=lambda r: -r["beyond_missed"])[:5]
    print("\n창 밖이라 놓친 것이 많은 키워드:")
    for row in worst_window:
        print(f"   {row['keyword']:<34} {row['beyond_missed']:>5}건")


async def scan(
    pages: int,
    fresh_hours: float,
    sorts: tuple = ("created",),
    check_filter: bool = True,
    track: set | None = None,
    sellers: set | None = None,
) -> None:
    from mercapi import Mercapi

    ok, why = sort_options_are_distinct()
    if len(sorts) > 1 or "score" in sorts:
        if not ok:
            print(f"[중단] 정렬 패스를 가를 수 없습니다 — {why}", file=sys.stderr)
            print("      이대로 돌리면 두 패스가 같은 조회가 되어 표가 거짓이 됩니다.",
                  file=sys.stderr)
            raise SystemExit(2)

    api = Mercapi()
    seen_ids, keyword_checked_at = load_state()
    now = datetime.now().timestamp()
    stamps = [v for v in keyword_checked_at.values() if isinstance(v, (int, float))]
    lag = (now - max(stamps)) / 60 if stamps else None
    print(
        f"봇이 보는 창: 키워드·정렬당 상위 {MAX_ITEMS_PER_KEYWORD}건 / "
        f"이번 조회 깊이: {pages}페이지({pages * MAX_ITEMS_PER_KEYWORD}건) / "
        f"상태 파일 {len(seen_ids):,}건 / '최근'의 기준: {fresh_hours}시간"
    )
    print(f"이번에 볼 정렬: {' + '.join(sort_label(s) for s in sorts)}")
    if lag is not None:
        print(
            f"체크아웃된 상태 파일은 {lag:.0f}분 전 것입니다 — 그 뒤에 올라온 매물은"
            " 봇이 아직 볼 기회가 없었으므로 '못 봄'에서 빼고 따로 셉니다."
        )

    all_rows, all_unique, all_found = {}, {}, {}
    excluded_all: list = []
    for sort in sorts:
        print()
        print("=" * 78)
        print(f"■ {sort_label(sort)}({sort}) 패스"
              + ("  — 매 실행. 새 매물을 책임집니다." if sort == "created"
                 else f"  — 전체 조회({FULL_SCAN_INTERVAL_SECONDS // 60}분)에서만."
                      " 오래 올라와 있는 매물을 봅니다."))
        print("=" * 78)
        rows, unique, excluded, found_at = await census(
            api, sort, pages, fresh_hours, seen_ids, keyword_checked_at,
            check_filter and sort == "created", track, sellers,
        )
        all_rows[sort], all_unique[sort] = rows, unique
        all_found[sort] = found_at
        excluded_all.extend(excluded)
        report_missed(unique, rows, fresh_hours, sort, check_filter and sort == "created")
        report_window_sizes(rows, sort)

    if check_filter and "created" in sorts:
        print("\n필터가 빼는 것이 '무엇인가' (최근 등록분, 카테고리별):")
        print(
            "※ '필터가 빼서 못 본 것'은 그 자체로는 결함이 아닙니다 — 필터는 원래 빼라고\n"
            "   걸어 둔 것이라 100%가 나올 수밖에 없습니다."
        )
        seen_sample = {extract_item_id(f) for f in excluded_all}
        print(f"   대상 {len(seen_sample):,}건")
        for category, count, names in category_breakdown(excluded_all, top=8):
            print(f"   카테고리 {str(category):<8} {count:>5}건   예: {' / '.join(names)}")

    if sellers:
        print()
        print("=" * 78)
        print(f"■ 지목한 판매자 {', '.join(sorted(sellers))}의 매물이 창에 들어오는가")
        print("=" * 78)
        for sort in sorts:
            rows = all_rows[sort]
            inside = sum(row["seller_in"] for row in rows)
            outside = sum(row["seller_out"] for row in rows)
            total = inside + outside
            print(
                f"   [{sort_label(sort)}] 훑은 범위에서 이 판매자 매물 {total}건"
                + (f" · 창 안 {inside}건({inside / total * 100:.0f}%)"
                   f" / 창 밖 {outside}건({outside / total * 100:.0f}%)" if total else "")
            )
        print(
            "\n   창 밖 비율이 높으면, 이 판매자의 매물에 대해서는 '이번 조회 결과에\n"
            "   없다'가 사라짐의 증거가 아니라 **평소 상태**입니다."
        )
    if track:
        report_tracked(track, all_found, seen_ids, sorts,
                       await still_on_sale(api, track))
    if len(sorts) > 1:
        report_combined(all_rows, all_unique, seen_ids)
    print("완료")


async def still_on_sale(api, item_ids: set) -> dict:
    """쫓는 매물이 **지금도 팔리지 않고 올라와 있는지** 하나씩 물어봅니다.

    왜 필요한가: 검색으로 '훑은 범위에 없음'이 나와도 두 가지가 갈리지 않습니다 —
    이미 팔려서 없는 것과, 아직 올라와 있는데 순위가 한참 뒤라 못 본 것입니다.
    재출품 오판을 설명하려면 **뒤쪽**이어야 합니다. 앞쪽이면 '사라졌다'가 맞는
    판정이었다는 뜻이니까요.

    `Mercapi.item()`은 봇이 쓰지 않는 표면이라 `tests/test_mercapi_surface.py`가
    지키지 않습니다. 그래서 실패를 삼키고 '확인 실패'로 돌려줍니다 — 이 조회 하나
    때문에 스캔 전체가 죽으면 안 됩니다.
    """
    states: dict = {}
    for item_id in sorted(item_ids):
        # 일반 매물(m+숫자)과 숍스 상품은 **다른 엔드포인트**입니다. 숍스 ID로
        # `item()`을 부르면 응답 모양이 달라 mercapi가 KeyError로 터집니다
        # (2026-09-14 실측: 쫓던 12건 전부 '확인 실패(KeyError)'였습니다).
        shop_item = not MERCARI_ITEM_ID_PATTERN.match(str(item_id))
        try:
            found = await (api.product(item_id) if shop_item else api.item(item_id))
        except Exception as exc:
            states[item_id] = f"확인 실패({type(exc).__name__})"
            await asyncio.sleep(PAGE_PAUSE_SECONDS)
            continue
        if found is None:
            states[item_id] = "없음(삭제)"
        elif shop_item:
            # 숍스 상품 응답에는 판매 상태 필드가 없습니다. 페이지가 아직 있다는
            # 것까지만 말할 수 있으니, 그 이상으로 적지 않습니다.
            states[item_id] = "상품 페이지 있음"
        else:
            status = str(getattr(found, "status", "") or "")
            states[item_id] = {
                "ITEM_STATUS_ON_SALE": "판매중",
                "ITEM_STATUS_SOLD_OUT": "판매완료",
                "ITEM_STATUS_STOP": "중지",
                "ITEM_STATUS_TRADING": "거래중",
            }.get(status, status or "상태 모름")
        await asyncio.sleep(PAGE_PAUSE_SECONDS)
    return states


def report_tracked(track: set, all_found: dict, seen_ids: set, sorts: tuple,
                   on_sale: dict | None = None) -> None:
    """이름을 대고 쫓는 매물이 **지금** 어느 창의 몇 등에 있는지.

    재출품 오판에서 '사라졌다'로 읽힌 예전 매물들을 여기에 넣습니다. 그 판정은
    **그때** 내려졌으므로 지금 순위가 그때 순위는 아닙니다 — 되돌려 볼 수 있는
    기록이 없습니다. 그래도 가릴 수 있는 것이 있습니다. 그 매물이 지금도 팔리지 않고
    올라와 있는데 **창 밖 깊은 자리**에 있다면, 봇이 '사라졌다'로 읽은 그 시각에도
    창 밖이었을 가능성이 높습니다. 반대로 지금 창 안 앞자리에 있다면 다른 설명을
    찾아야 합니다.
    """
    print()
    print("=" * 78)
    print(f"■ 이름을 대고 쫓은 매물 {len(track)}건 — 지금 어느 창의 몇 등에 있는가")
    print("=" * 78)
    print("   ※ 지금 순위이지 판정 당시 순위가 아닙니다. 되돌려 볼 기록은 없습니다.")
    for item_id in sorted(track):
        marks = []
        for sort in sorts:
            hits = (all_found.get(sort) or {}).get(item_id) or []
            if not hits:
                marks.append(f"{sort_label(sort)}: 훑은 범위에 없음")
                continue
            best = min(hits, key=lambda hit: hit[1])
            where = "창 안" if best[1] < MAX_ITEMS_PER_KEYWORD else "창 밖"
            marks.append(
                f"{sort_label(sort)}: [{best[0]}] {best[1] + 1}등({where})"
                f" · 걸린 키워드 {len(hits)}개"
            )
        state = (on_sale or {}).get(item_id) or (
            "추적 중" if item_id in seen_ids else "상태 파일에 없음")
        print(f"   {item_id:<26} {state:<14} | " + " | ".join(marks))
    if on_sale:
        failed = [i for i, state in on_sale.items() if state.startswith("확인 실패")]
        if len(failed) == len(on_sale):
            # 전부 실패했는데 '판매중 0건'이라고 찍으면, 확인이 안 된 것을 '다 팔렸다'로
            # 읽게 됩니다. 못 잰 것은 못 쟀다고 말해야 합니다.
            print(f"\n   ⛔ 상태 확인이 {len(failed)}건 전부 실패했습니다."
                  " 이 항목들에 대해서는 판매 여부를 **말할 수 없습니다.**")
            return
        alive = [i for i, state in on_sale.items()
                 if state in ("판매중", "상품 페이지 있음")]
        hidden = [i for i in alive
                  if not any((all_found.get(sort) or {}).get(i) for sort in sorts)]
        print(
            f"\n   아직 남아 있는 것 {len(alive)}/{len(track)}건"
            + (f" (확인 실패 {len(failed)}건)" if failed else "")
            + f", 그중 훑은 범위(두 패스 모두)에 **안 나오는 것 {len(hidden)}건**."
        )
        print(
            "   남아 있는데 검색에 안 나온다면 '사라졌다'가 아니라 '순위가 너무 뒤'입니다 —\n"
            "   재출품 판정이 읽은 그 '없음'의 정체가 이것입니다."
        )


def report_combined(all_rows: dict, all_unique: dict, seen_ids: set) -> None:
    """두 패스를 합쳐도 봇이 한 번에 볼 수 있는 것은 얼마인가.

    재출품 판정은 '예전 매물이 **이번 조회 결과에 없으면** 사라졌다'로 읽습니다.
    그래서 이 숫자가 판정의 바닥입니다 — 추적 중인 매물 중 한 번의 전체 조회에서
    실제로 눈에 들어오는 비율이 낮으면, '결과에 없다'는 사라짐의 증거가 될 수 없습니다.
    """
    visible, tracked_visible, tracked_scanned = set(), set(), set()
    for sort, unique in all_unique.items():
        visible |= unique["window_ids"]
        tracked_visible |= unique["tracked_in_window"]
        tracked_scanned |= unique["tracked_scanned"]
    for group in (visible, tracked_visible, tracked_scanned):
        group.discard(None)
    print()
    print("=" * 78)
    print("■ 두 패스를 합치면 — 한 번의 전체 조회에서 봇이 실제로 보는 것")
    print("=" * 78)
    print(f"   창 안에 들어온 매물(중복 제거)          {len(visible):,}건")
    print(f"   그중 추적 중(seen)인 매물               {len(tracked_visible):,}건")
    if seen_ids:
        share = len(tracked_visible) / len(seen_ids) * 100
        print(f"   상태 파일의 추적 대상                   {len(seen_ids):,}건")
        print(f"   → 한 번의 전체 조회에서 눈에 들어오는 비율  {share:.1f}%")
    print(
        "\n   이 비율이 낮으면 '이번 결과에 없다'는 **사라짐의 증거가 아닙니다.**\n"
        "   재출품 판정(confirm_disappearance)이 기대는 것이 바로 그 증거입니다."
    )
    # 위 비율에는 **이미 팔린 매물**이 섞여 있습니다. 상태 파일은 팔린 매물을 지우지
    # 않으므로, '안 보이는 것'에 '팔려서 안 보이는 것'이 함께 들어갑니다. 아래 값은
    # 그 혼입이 없습니다 — **이번 조회에 실제로 걸린**(= 아직 올라와 있는) 매물만
    # 놓고, 그중 몇 %가 창 안이었는지 봅니다.
    if tracked_scanned:
        share = len(tracked_visible) / len(tracked_scanned) * 100
        print(
            f"\n   팔린 매물을 걸러낸 값: 이번 조회에 걸린 추적 중 매물"
            f" {len(tracked_scanned):,}건 가운데\n"
            f"   창 안에 있던 것 {len(tracked_visible):,}건 ({share:.0f}%)."
            " 나머지는 아직 올라와 있는데 창 밖입니다."
        )
    for sort, rows in all_rows.items():
        # 분모는 '최근 매물이 하나라도 있어서 실제로 확인이 된 키워드'입니다.
        # 전체 키워드로 나누면, 최근 매물이 없어 확인조차 못 한 키워드가 '가정이
        # 성립한 쪽'으로 세어져 깨진 비율이 묽어집니다.
        measured = [row for row in rows if row["young_ranks"]]
        broken = [row["keyword"] for row in measured if not row["conveyor"]]
        worst = max((max(row["young_ranks"]) for row in measured), default=None)
        print(
            f"\n   [{sort_label(sort)}] 컨베이어 가정(새 매물은 창 안 앞자리로 들어온다)이"
            f" 깨진 키워드 {len(broken)}/{len(measured)}개"
            + (f" · 최근 1시간 매물이 꽂힌 가장 뒷자리 {worst + 1}등"
               if worst is not None else "")
            + (f"\n      {', '.join(broken)}" if broken else "")
        )


def report_window_sizes(rows: list, sort: str = "created") -> None:
    """창을 줄여도 되는지 판단할 근거를 한자리에 모읍니다.

    '1분마다 도는데 매번 120건까지 볼 필요가 있나'라는 질문의 답은 건수가 아니라
    **시간**에 있습니다. 창을 줄이면 조회가 가벼워지는 것이 아니라(페이지 크기는
    라이브러리가 120으로 고정), 매물이 창 안에 머무는 시간이 짧아집니다. 그 시간이
    봇이 한 바퀴 도는 간격보다 짧아지는 순간부터 놓치기 시작합니다.
    """
    sizes = [size for size, _ in (rows[0]["dwell"] if rows else [])]
    measured = [row for row in rows if row["young_ranks"]]
    broken = [row for row in measured if not row["conveyor"]]
    growth = [(row["keyword"], dwell_grows_with_the_window(row["dwell"])) for row in rows]
    flat = [keyword for keyword, grows in growth if grows is False]
    judged = [keyword for keyword, grows in growth if grows is not None]
    print(f"\n[{sort_label(sort)}] 창에 머무는 시간"
          " (건수가 아니라 '머무는 시간'으로 봅니다):")
    if broken:
        # 이 도구가 스스로를 못 믿겠다고 말하는 자리입니다. 아래 표를 그대로 읽으면
        # 안 된다는 뜻이고, 종단 측정(--snapshots)으로 다시 재야 합니다.
        print(
            f"   ⛔ **아래 표를 이 정렬에 그대로 쓰면 안 됩니다.** 최근 1시간 매물이 창\n"
            f"      밖에까지 꽂히는 키워드가 {len(broken)}/{len(measured)}개입니다. 이 계산은\n"
            f"      '새 매물은 맨 앞으로 들어와 뒤로만 밀린다'는 컨베이어 가정 위에 서\n"
            f"      있는데, 그게 깨지면 '밖으로 밀려난 것 중 가장 어린 것의 나이'는\n"
            f"      '창을 통과하는 시간'이 아닙니다. --snapshots로 종단 측정하세요."
        )
    if flat:
        # 두 번째 신호입니다. 앞의 것과 원인은 같지만 서로를 대체하지 못합니다 —
        # 이쪽은 최근 매물이 하나도 없는 키워드에서도 잡힙니다.
        print(
            f"   ⛔ 창을 넓혔는데 깊이가 **그대로인** 키워드가 {len(flat)}/{len(judged)}개입니다"
            f" ({', '.join(flat[:6])}).\n"
            f"      컨베이어라면 30건 < 60건 < 120건이 반드시 성립합니다. 평평하다는 것은\n"
            f"      이 값이 '창을 통과하는 시간'이 아니라는 뜻입니다."
        )
    print("\n창을 줄여도 되는가 (건수가 아니라 '머무는 시간'으로 봅니다):")
    print(
        "   ※ 페이지 크기는 mercapi가 120으로 고정해 보냅니다. 상한을 낮춰도 120건을\n"
        "      받아 온 뒤 잘라낼 뿐이라 호출 수도 트래픽도 줄지 않습니다. 달라지는 것은\n"
        "      '올라온 매물이 창 안에 얼마나 남아 있는가' 하나뿐입니다.\n"
        "   ※ 아래는 '창 밖으로 밀려난 것 중 가장 최근 매물의 나이'입니다. 순위 N번째\n"
        "      매물의 나이로 재면 안 됩니다 — 등록순 결과에 예전 매물이 섞여 들어와서\n"
        "      그 자리에 앉으면 값이 널뜁니다(위 '등록순 역전' 참고)."
    )
    header = "   " + f"{'키워드':<26}" + "".join(f"{str(size) + '건':>14}" for size in sizes)
    print(header)
    for row in sorted(rows, key=dwell_sort_key):
        cells = "".join(f"{format_span(minutes):>14}" for _, minutes in row["dwell"])
        print(f"   {row['keyword']:<26}{cells}")
    print(
        f"\n   봇이 한 바퀴 도는 간격: 정상 1분"
        f" / 전체 조회 {FULL_SCAN_INTERVAL_SECONDS // 60}분"
        f" / 긴 정지 뒤 최대 {MAX_LOOKBACK_SECONDS // 3600}시간"
    )
    worst = min(rows, key=dwell_sort_key, default=None)
    if worst is not None:
        shallow = ", ".join(
            f"{size}건={format_span(minutes)}" for size, minutes in worst["dwell"]
        )
        print(f"   가장 빨리 밀려나는 키워드: [{worst['keyword']}] {shallow}")


async def measure_residence(
    pages: int, snapshots: int, interval: float, sorts: tuple, keywords: list
) -> None:
    """같은 창을 반복해서 찍어 '매물이 창에 실제로 얼마나 머무는가'를 **직접** 잽니다.

    왜 따로 필요한가: 한 장짜리 추정(window_dwell)은 컨베이어 가정 위에 서 있습니다.
    등록순에서는 그 가정이 서지만 추천순에서는 설 이유가 없습니다 — 순위를 만드는 것이
    시각이 아니기 때문입니다. 그래서 **가정을 쓰지 않는 자**를 하나 들고 와서, 먼저
    등록순에서 두 자가 같은 값을 내는지 확인하고(눈금 맞추기), 그 다음에 추천순에
    들이댑니다. 등록순에서 안 맞으면 이 자 자체가 틀린 것입니다.

    검산은 숫자로 합니다. 컨베이어라면 깊이 D인 창에서 lag 분 뒤 남는 비율은
    1 - lag/D 입니다. 그 예측과 실측을 나란히 찍습니다.
    """
    from mercapi import Mercapi

    ok, why = sort_options_are_distinct()
    if (len(sorts) > 1 or "score" in sorts) and not ok:
        print(f"[중단] 정렬 패스를 가를 수 없습니다 — {why}", file=sys.stderr)
        raise SystemExit(2)

    api = Mercapi()
    targets = [s for s in SEARCHES if not keywords or s["query"] in keywords]
    if not targets:
        print(f"[중단] --keywords에 맞는 키워드가 없습니다: {keywords}", file=sys.stderr)
        return
    print(
        f"종단 측정: {len(targets)}개 키워드 × {len(sorts)}개 정렬 × {pages}페이지를"
        f" {interval:.0f}초 간격으로 {snapshots}번 찍습니다"
        f" (총 {snapshots * interval / 60:.0f}분)"
    )
    print(f"   키워드: {', '.join(s['query'] for s in targets)}")
    print(f"   정렬: {' + '.join(sort_label(s) for s in sorts)}\n")

    # (키워드, 정렬) -> [(찍은 시각, 순위대로 늘어놓은 ID, 그때의 dwell 추정)]
    series: dict = {}
    for shot in range(snapshots):
        started = datetime.now().timestamp()
        for search in targets:
            keyword, categories = search["query"], search["categories"]
            for sort in sorts:
                fields_list, _ = await walk(api, keyword, categories, pages, sort)
                await asyncio.sleep(PAGE_PAUSE_SECONDS)
                now = datetime.now().timestamp()
                order = [extract_item_id(f) for f in fields_list]
                dwell = window_dwell(fields_list, now)
                series.setdefault((keyword, sort), []).append((now, order, dwell))
        elapsed = datetime.now().timestamp() - started
        print(f"   [{shot + 1}/{snapshots}] {elapsed:.0f}초 걸림", flush=True)
        if shot + 1 < snapshots:
            await asyncio.sleep(max(0.0, interval - elapsed))

    print()
    print("=" * 78)
    print("■ 창에 실제로 얼마나 머무는가 (종단 실측 vs 한 장짜리 추정)")
    print("=" * 78)
    for sort in sorts:
        print(f"\n[{sort_label(sort)}]")
        for search in targets:
            keyword = search["query"]
            shots = series.get((keyword, sort)) or []
            if len(shots) < 2:
                continue
            result = survival_curve(shots)
            estimate = shots[0][2][-1][1]  # 첫 스냅숏의 120건 기준 추정 깊이(분)
            span = (shots[-1][0] - shots[0][0]) / 60.0
            print(f"   {keyword}")
            print(f"      한 장짜리 추정 깊이: {format_span(estimate)}")
            for _, lag, kept in result["lags"][:6]:
                predicted = predicted_survival(estimate, lag)
                print(
                    f"      {lag:.0f}분 뒤 남음 실측 {kept * 100:.0f}%"
                    + (f" / 추정이 말하는 값 {predicted * 100:.0f}%"
                       if predicted is not None else " / 추정 없음")
                )
            # 실측에서 거꾸로 뽑은 깊이. 가정을 쓰지 않는 값입니다.
            last_lag, kept_last = (result["lags"][-1][1], result["lags"][-1][2]) \
                if result["lags"] else (0.0, 1.0)
            left = 1 - kept_last
            observed = (last_lag / left) if left > 0 else None
            print(
                f"      → {span:.0f}분 동안 창 {result['window_size']}건 중"
                f" 빠진 것 {result['displaced'] + result['vanished']}건"
                f"(순위에 밀림 {result['displaced']} / 통째로 사라짐 {result['vanished']})"
                f" · 나갔다 **다시 들어온 것 {result['reentry']}건**"
            )
            print(
                "      → 실측이 말하는 깊이: "
                + (format_span(observed) if observed else "이 시간 안에는 거의 안 빠짐")
            )
    print(
        "\n※ 판단은 **추정과 실측이 갈리는가**로 하세요. 등록순에서 둘이 맞으면 이 자가\n"
        "   제대로 눈금이 잡힌 것이고, 그 상태에서 추천순만 갈린다면 한 장짜리 추정을\n"
        "   그 정렬에 쓰면 안 된다는 뜻입니다.\n"
        "※ '다시 들어온 것'은 잣대가 아닙니다 — 창 경계에서 순위가 떠는 것은 어느\n"
        "   정렬에서나 있습니다(2026-09-14 실측: 등록순도 4~8건). 크기로 읽으세요.\n"
        "   들락거리는 만큼 재출품 판정의 '이번 결과에 없다'가 헛돕니다."
    )
    print("완료")


def dwell_sort_key(row: dict) -> float:
    """매물이 가장 빨리 밀려나는 키워드부터 오도록. 창이 남는 키워드는 맨 뒤로."""
    last = row["dwell"][-1][1] if row.get("dwell") else None
    return float("inf") if last is None else last


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pages", type=int, default=5, help="키워드당 훑을 페이지 수(1페이지=120건)")
    parser.add_argument("--fresh-hours", type=float, default=24.0, help="'최근 등록'으로 볼 시간")
    parser.add_argument(
        "--sort", default="created",
        choices=["created", "score", "both"],
        help="어느 정렬 패스를 볼지. 봇은 등록순을 매 실행, 추천순을 전체 조회에서 돕니다",
    )
    parser.add_argument(
        "--no-filter-check", action="store_true",
        help="카테고리 필터 대조(필터 없는 조회)를 건너뜁니다 — 조회량이 절반이 됩니다",
    )
    parser.add_argument(
        "--snapshots", type=int, default=0,
        help="종단 측정: 같은 창을 이 횟수만큼 반복해 찍어 실제 체류를 잽니다(2 이상)",
    )
    parser.add_argument(
        "--interval", type=float, default=150.0,
        help="종단 측정에서 스냅숏 사이 간격(초)",
    )
    parser.add_argument(
        "--seller", default="",
        help="이 판매자(쉼표 구분)의 매물이 창 안에 얼마나 들어오는지 셉니다",
    )
    parser.add_argument(
        "--track", default="",
        help="이름을 대고 쫓을 매물 ID(쉼표 구분). 지금 어느 창의 몇 등에 있는지 찍습니다",
    )
    parser.add_argument(
        "--keywords", default="",
        help="종단 측정에서 볼 키워드(쉼표 구분). 비우면 SEARCHES 전부",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    passes = ("created", "score") if args.sort == "both" else (args.sort,)
    if args.snapshots >= 2:
        asyncio.run(
            measure_residence(
                pages=args.pages,
                snapshots=args.snapshots,
                interval=args.interval,
                sorts=passes,
                keywords=[k.strip() for k in args.keywords.split(",") if k.strip()],
            )
        )
    else:
        asyncio.run(
            scan(
                args.pages,
                args.fresh_hours,
                passes,
                not args.no_filter_check,
                {t.strip() for t in args.track.split(",") if t.strip()},
                {t.strip() for t in args.seller.split(",") if t.strip()},
            )
        )
