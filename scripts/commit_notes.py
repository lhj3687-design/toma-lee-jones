#!/usr/bin/env python3
"""상태 커밋 메시지에 실린 실행 계수기를 셉니다.

**무엇을 푸는 도구인가.** 봇이 1분마다 도니 하루면 실행이 1,400번이고, 개발 환경에서는
Actions 로그 묶음 내려받기가 막혀 있어 로그를 한 건씩 API로만 꺼낼 수 있습니다. 그래서
'실행 로그에만 있는 숫자'는 사실상 셀 수 없었습니다 — `[재출품] … 못 물어봄`이 그
자리였습니다. `check_mercari.py`가 그 숫자를 상태 커밋 제목 끝에 실어 두면, 여기서
`git log`로 공짜로 셉니다.

    queue Mercari alerts (재출품 5: 있음2/없음3/못물어봄0, 다음페이지 1회 신규112)

**읽을 때 조심할 것.** 꼬리표가 없는 커밋은 두 가지입니다 — ① 실을 것이 0건이었던
실행, ② 이 기능이 배포되기 전의 커밋. 그래서 이 도구는 **꼬리표가 붙은 첫 커밋 시각을
같이 찍습니다.** 그보다 앞 구간의 '0건'은 측정이 아니라 침묵입니다.

**'못 물어봄'과 '아예 못 물어봄'을 섞지 마세요.** 앞쪽은 물어봤는데 답이 안 온 것이라
차단의 신호이고, 뒤쪽은 실행당 상한에 걸려 **묻지도 못한** 것입니다. 뒤쪽은 다시
'건수 상한'과 '시간 상한'으로 갈리는데(한 판의 '물어본 것'이 상한과 같으면 건수, 작으면
시간) 처방이 서로 다릅니다. 이 도구가 셋을 갈라 찍습니다.

    python scripts/commit_notes.py --calibrate          # 눈금 먼저
    python scripts/commit_notes.py origin/main --since='2026-09-16 00:00'
    python scripts/commit_notes.py origin/main --since='2026-09-16 00:00' --by-hour

**눈금을 믿기 전에 고장을 내 보세요.** 2026-09-17에 고장 15개를 넣으니 `--calibrate`가
8개를 그대로 통과시켰습니다 — 자릿수 고침이 재출품 칸에만 들어 있었고, 눈금이 부르지
않는 `walk()`·`report()`는 아예 안 지켜지고 있었습니다.
`tests/test_commit_notes.py`의 `CalibrationCatchesFaultsTests`가 그 고장들을 CI에서
계속 넣어 봅니다.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import re
import subprocess
import sys
import tempfile
import types
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

RELIST_PATTERN = re.compile(
    r"재출품 (?P<asked>\d+)(?:/(?P<targets>\d+))?: "
    r"있음(?P<alive>\d+)/없음(?P<gone>\d+)/못물어봄(?P<unknown>\d+)"
    r"(?: 눈금⛔(?P<blocked>[^,)]+))?"
)
NEXT_PAGE_PATTERN = re.compile(r"다음페이지 (?P<fired>\d+)회 신규(?P<fresh>\d+)")
NEAR_MISS_PATTERN = re.compile(r"다음페이지 근접 신규(?P<fresh>\d+)")
SEARCH_FAIL_PATTERN = re.compile(r"조회실패 (?P<failures>\d+)")


def parse_note(subject: str) -> dict | None:
    """커밋 제목에서 꼬리표를 읽어 냅니다. 꼬리표가 없으면 None입니다.

    **None과 빈 dict를 가르는 것이 이 함수의 요점입니다.** 둘을 뭉치면 '실을 것이
    없었던 실행'과 '꼬리표를 못 읽은 실행'이 같아집니다.
    """
    start = subject.find(" (")
    if start < 0 or not subject.rstrip().endswith(")"):
        return None
    body = subject.rstrip()[start + 2:-1]
    note: dict = {}
    relist = RELIST_PATTERN.search(body)
    if relist:
        asked = int(relist.group("asked"))
        targets = int(relist.group("targets") or asked)
        note["relist"] = {
            "asked": asked,
            "targets": targets,
            "alive": int(relist.group("alive")),
            "gone": int(relist.group("gone")),
            "unknown": int(relist.group("unknown")),
            "blocked": (relist.group("blocked") or "").split("+") if relist.group("blocked") else [],
        }
    fired = NEXT_PAGE_PATTERN.search(body)
    if fired:
        note["next_page"] = {"fired": int(fired.group("fired")), "fresh": int(fired.group("fresh"))}
    near = NEAR_MISS_PATTERN.search(body)
    if near:
        note["near_miss"] = int(near.group("fresh"))
    failures = SEARCH_FAIL_PATTERN.search(body)
    if failures:
        note["search_failures"] = int(failures.group("failures"))
    return note or None


def walk(rev_args: list[str], repo: Path):
    log = subprocess.run(
        ["git", "-C", str(repo), "log", "--first-parent", "--format=%ct%x09%s", *rev_args],
        check=True, capture_output=True,
    ).stdout.decode()
    for line in log.splitlines():
        timestamp, _, subject = line.partition("\t")
        if timestamp.isdigit():
            yield int(timestamp), subject


class Tally:
    """세는 자. `Counter`는 0을 더해도 키가 남아 '아무 일 없었다'가 빈 결과가 아닙니다
    (README "병합에서 뒤집히는 값" 참고). 그 함정을 피하려고 0은 아예 넣지 않습니다."""

    def __init__(self) -> None:
        self.commits = 0
        self.with_note = 0
        self.first_note_at: int | None = None
        self.last_note_at: int | None = None
        self.relist = Counter()
        self.relist_runs = 0
        self.blocked = Counter()
        self.next_page = Counter()
        self.next_page_runs = 0
        self.near_miss: list[int] = []
        self.search_failures = 0
        self.search_failure_runs = 0
        # 생산자 불변식이 깨진 꼬리표 수. `asked`는 `len(survival)`이고 있음·없음·
        # 못물어봄은 그 survival을 셋으로 가른 것이라(check_mercari.py 2362~2366줄)
        # 합이 반드시 asked 입니다. 깨지면 **운영 데이터가 이상한 것이 아니라 이 파서가
        # 칸을 잘못 읽고 있는 것**입니다. 눈금은 만들어 둔 제목만 보지만 이 점검은
        # 매번 진짜 꼬리표 전부를 봅니다.
        self.inconsistent = 0
        self.by_hour: dict[int, Counter] = defaultdict(Counter)
        # 아예 못 물어본 판의 (물어볼 자리, 물어본 것). 합계로 뭉치면 '건수 상한'과
        # '시간 상한'이 구분되지 않습니다 — **처방이 다릅니다**(아래 report 참고).
        self.capped: list[tuple[int, int]] = []

    def add(self, timestamp: int, note: dict | None) -> None:
        self.commits += 1
        if note is None:
            return
        self.with_note += 1
        self.first_note_at = min(self.first_note_at or timestamp, timestamp)
        self.last_note_at = max(self.last_note_at or timestamp, timestamp)
        hour = datetime.fromtimestamp(timestamp, timezone.utc).hour
        relist = note.get("relist")
        if relist:
            self.relist_runs += 1
            if relist["alive"] + relist["gone"] + relist["unknown"] != relist["asked"]:
                self.inconsistent += 1
            for key in ("targets", "asked", "alive", "gone", "unknown"):
                if relist[key]:
                    self.relist[key] += relist[key]
            for mark in relist["blocked"]:
                self.blocked[mark] += 1
            bucket = self.by_hour[hour]
            bucket["runs"] += 1
            for key in ("targets", "asked", "unknown"):
                if relist[key]:
                    bucket[key] += relist[key]
            if relist["targets"] > relist["asked"]:
                bucket["skipped"] += relist["targets"] - relist["asked"]
                self.capped.append((relist["targets"], relist["asked"]))
        fired = note.get("next_page")
        if fired:
            self.next_page_runs += 1
            self.next_page["fired"] += fired["fired"]
            self.next_page["fresh_max"] = max(self.next_page["fresh_max"], fired["fresh"])
        if note.get("near_miss"):
            self.near_miss.append(note["near_miss"])
        if note.get("search_failures"):
            self.search_failure_runs += 1
            self.search_failures += note["search_failures"]
            self.by_hour[hour]["search_failures"] += note["search_failures"]


def stamp(value: int | None) -> str:
    if value is None:
        return "—"
    return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def report(tally: Tally, by_hour: bool = False) -> None:
    print(f"상태 커밋 {tally.commits:,}개 / 꼬리표가 붙은 커밋 {tally.with_note:,}개")
    print(f"꼬리표 첫 커밋 {stamp(tally.first_note_at)} · 마지막 {stamp(tally.last_note_at)}")
    if not tally.with_note:
        print("\n⛔ 이 구간에는 꼬리표가 한 건도 없습니다. '0건'이 아니라 **측정이 없는 것**"
              "입니다 — 계수기가 배포되기 전 구간인지 먼저 확인하세요.")
        return

    print("\n[재출품] 예전 매물에게 직접 물어본 결과")
    if not tally.relist_runs:
        print("  물어볼 자리가 있었던 실행 0회 — 분모가 없으므로 비율을 읽지 마세요.")
    else:
        asked = tally.relist["asked"]
        targets = tally.relist["targets"]
        unknown = tally.relist["unknown"]
        print(f"  물어본 실행 {tally.relist_runs:,}회 / 물어볼 자리 {targets:,}건 "
              f"/ 실제로 물어본 것 {asked:,}건")
        print(f"  아직 있음 {tally.relist['alive']:,}건 · 없어짐 {tally.relist['gone']:,}건 "
              f"· 못 물어봄 {unknown:,}건")
        if asked:
            print(f"  못 물어봄 비율 {unknown / asked * 100:.2f}% (물어본 것 기준)")
        if targets > asked:
            print(f"  아예 못 물어본 것 {targets - asked:,}건 "
                  "(실행당 상한이거나 눈금 불신 — 아래 줄을 보세요)")
            # 원인이 셋인데 겉모양이 같습니다. 눈금 불신은 아래 줄이 갈라 주고,
            # 나머지 둘(건수 상한 / 시간 상한)은 **한 판의 '물어본 것'이 상한과 같은지**로
            # 갈립니다 — 시간 상한은 상한에 닿기 전에 끊으므로 그보다 작게 나옵니다.
            # 처방이 다릅니다: 건수 상한은 MAX_RELIST_LOOKUPS_PER_RUN, 시간 상한은
            # MAX_RELIST_LOOKUP_SECONDS_PER_RUN 을 봐야 합니다.
            cap = _lookup_cap()
            worst = max((t for t, _a in tally.capped), default=0)
            if not cap:
                print(f"     그런 실행 {len(tally.capped):,}회 · 한 판 최다 물어볼 자리 {worst}건")
                print("     ⛔ 봇의 실행당 상한 값을 못 읽어 '건수 상한'과 '시간 상한'을"
                      " 가르지 못합니다 — 처방이 다르므로 이 둘을 뭉친 채로 읽지 마세요.")
            else:
                by_count = sum(1 for _targets, asked in tally.capped if asked >= cap)
                by_time = len(tally.capped) - by_count
                print(f"     그런 실행 {len(tally.capped):,}회 — 건수 상한({cap}건) {by_count:,}회"
                      f" / 시간 상한 {by_time:,}회 · 한 판 최다 물어볼 자리 {worst}건")
                print("     '건수 상한'이면 상한을 올리는 것이 처방이고, '시간 상한'이면"
                      " 올려도 듣지 않습니다(그 실행은 이미 초를 다 쓴 것입니다).")
            print("     ⚠ 넘친 자리는 보류로 남는데, **다음 실행이 다시 본다는 보장은"
                  " 없습니다** — 그 매물이 검색 창에서 밀려나면 보류가 그대로 굳었다가"
                  " TTL로 사라집니다(2026-09-17 실측: 18건이 17.3시간 동안 안 움직임).")
        if tally.blocked:
            marks = ", ".join(f"{k} {v:,}회" for k, v in sorted(tally.blocked.items()))
            print(f"  ⛔ 눈금을 못 믿어 판정을 통째로 미룬 실행: {marks}")
            print("     '일반/숍스'는 지금 살아 있는 매물이 '있음'으로 안 나온 것 — 메루카리가"
                  " 우리를 막고 있을 때의 모양입니다. 403을 '사라짐'으로 읽는 판정 전체가"
                  " 흔들립니다.")
            print("     '…없음'은 눈금을 세울 매물이 이번 검색에 없던 것 — 차단이 아니라"
                  " 표본 부족입니다. 처방이 다릅니다.")
        else:
            print("  ⛔ 눈금 불신 0회 (지금 살아 있는 매물이 '있음'으로 답한 실행만 셌습니다)")
        # 진짜 꼬리표 전부에 대는 점검입니다. 눈금(--calibrate)은 만들어 둔 제목만 보므로
        # 운영에서 형식이 갈리는 것은 여기서만 잡힙니다.
        if tally.inconsistent:
            print(f"  ⛔ 있음+없음+못물어봄 ≠ 물어본 수인 꼬리표 {tally.inconsistent:,}개 "
                  "— 운영 데이터가 아니라 **이 파서가 칸을 잘못 읽고 있다**는 뜻입니다. "
                  "위 숫자를 읽지 마세요.")
        else:
            print(f"  ✅ 있음+없음+못물어봄 = 물어본 수 — 꼬리표 {tally.relist_runs:,}개 전부 성립")

    print("\n[다음 페이지 조건 #33]")
    if tally.next_page_runs:
        print(f"  발동 {tally.next_page['fired']:,}회 / 발동한 실행 {tally.next_page_runs:,}회 "
              f"/ 한 페이지 최다 신규 {tally.next_page['fresh_max']}건")
    else:
        print("  발동 0회.")
    if tally.near_miss:
        top = sorted(tally.near_miss, reverse=True)[:5]
        print(f"  발동선(100건)에 다가선 실행 {len(tally.near_miss):,}회 "
              f"— 상위 {', '.join(str(v) for v in top)}건")
        print("     이 줄이 자주 뜨는데 발동이 0이면 문턱(20건)이 빡빡한 것입니다.")
    else:
        print(f"  근접({int(NEAR_MISS_FRESH)}건 이상)도 0회 — 창이 애초에 차지 않았습니다. "
              "발동 0회를 '문턱이 높다'로 읽으면 안 됩니다.")

    print("\n[조회 실패] (추천순만 실패한 실행은 상태 파일에 흔적이 없습니다)")
    print(f"  실패한 페이지 요청 {tally.search_failures:,}건 / 그런 실행 {tally.search_failure_runs:,}회")

    if by_hour:
        print("\n[시각대별] '못 물어봄'이 특정 대에 몰리면 그건 차단의 모양입니다.")
        print("  UTC  실행  물어볼자리  물어본것  아예못물어봄  못물어봄  조회실패")
        for hour in sorted(tally.by_hour):
            c = tally.by_hour[hour]
            print(f"  {hour:02d}시 {c['runs']:5,}회 {c['targets']:9,} {c['asked']:9,}"
                  f" {c['skipped']:11,} {c['unknown']:9,} {c['search_failures']:9,}")
        print("  ⚠ 꼬리표가 붙는 실행 비율은 시각대마다 다릅니다(실을 것이 0이면 안 붙습니다).")
        print("    한 줄의 '0'은 '그 대에 아무 일도 없었다'가 아니라 '실을 것이 없었다'일 수"
              " 있으니, 같은 줄의 '물어본 것'이 0인지부터 보세요.")


# 문턱과 상한은 봇 쪽 상수를 그대로 씁니다(두 값이 갈리면 표를 잘못 읽게 됩니다).
def _bot():
    sys.path.insert(0, str(ROOT))
    sys.modules.setdefault("mercapi", types.ModuleType("mercapi"))
    sys.modules["mercapi"].Mercapi = object  # type: ignore[attr-defined]
    import check_mercari  # noqa: PLC0415

    return check_mercari


def _near_miss_threshold() -> int:
    return int(_bot().NEXT_PAGE_NEAR_MISS_FRESH)


def _lookup_cap() -> int:
    """봇의 실행당 상한. **못 읽으면 0을 돌려줍니다.**

    세는 자는 `git log`만 있으면 돌지만 이 값은 봇 모듈을 읽어야 나옵니다(의존성이
    없는 환경도 있습니다). 못 읽었는데 아무 숫자나 돌려주면 '건수 상한'과 '시간 상한'을
    **틀리게** 갈라 찍습니다. 가를 수 없으면 숫자를 내지 말고 못 가른다고 말합니다.
    """
    try:
        return int(_bot().MAX_RELIST_LOOKUPS_PER_RUN)
    except Exception:  # noqa: BLE001 - 의존성이 없는 환경이면 그냥 못 읽는 것입니다.
        return 0


NEAR_MISS_FRESH = 80


def calibrate() -> int:
    """세는 자를 **아는 답**과 **자체 점검**에 먼저 대 봅니다.

    세 가지를 봅니다.

      ① 왕복: 봇이 실제로 쓰는 `run_note_text()`가 만든 꼬리표를 이 파서가 되읽어
         원래 숫자가 나오는가. 형식을 한쪽만 고치면 여기서 걸립니다.
      ② 거짓 양성: 꼬리표가 없는 제목, 괄호만 있는 제목, 숫자가 빠진 꼬리표가
         **None으로 나오는가.** (`Counter`처럼 0을 채워 넣으면 '아무 일도 없었다'가
         빈 결과가 아니게 되어 이 점검이 언제나 통과합니다.)
      ③ 아는 답: 손으로 적은 제목 묶음의 합계가 미리 적어 둔 값과 맞는가.
    """
    sys.path.insert(0, str(ROOT))
    sys.modules.setdefault("mercapi", types.ModuleType("mercapi"))
    sys.modules["mercapi"].Mercapi = object  # type: ignore[attr-defined]
    import check_mercari  # noqa: PLC0415

    failures: list[str] = []

    # ① 왕복
    cases = [
        ({"relist_targets": 5, "relist_asked": 5, "relist_alive": 2, "relist_gone": 3},
         {"asked": 5, "targets": 5, "alive": 2, "gone": 3, "unknown": 0, "blocked": []}),
        ({"relist_targets": 9, "relist_asked": 3, "relist_alive": 1, "relist_gone": 1,
          "relist_unknown": 1},
         {"asked": 3, "targets": 9, "alive": 1, "gone": 1, "unknown": 1, "blocked": []}),
        # 자릿수가 한 자리뿐인 눈금은 `\d+`를 `\d`로 줄여 놓아도 통과합니다(실제로 한 번
        # 통과했습니다). 두 자리 이상을 반드시 섞습니다.
        ({"relist_targets": 124, "relist_asked": 124, "relist_alive": 57, "relist_gone": 55,
          "relist_unknown": 12},
         {"asked": 124, "targets": 124, "alive": 57, "gone": 55, "unknown": 12, "blocked": []}),
    ]
    for counters, expected in cases:
        check_mercari.reset_run_counters()
        for key, value in counters.items():
            check_mercari.count_event(key, value)
        subject = "queue Mercari alerts" + check_mercari.run_note_text()
        got = (parse_note(subject) or {}).get("relist")
        if got != expected:
            failures.append(f"왕복 불일치: {subject!r} -> {got!r} != {expected!r}")

    check_mercari.reset_run_counters()
    check_mercari.count_event("relist_targets", 4)
    check_mercari.count_event("relist_asked", 0)
    check_mercari.note_flag("relist_canary_blocked", "일반")
    check_mercari.note_flag("relist_canary_blocked", "숍스없음")
    subject = "queue Mercari alerts" + check_mercari.run_note_text()
    got = (parse_note(subject) or {}).get("relist") or {}
    if got.get("blocked") != ["일반", "숍스없음"]:
        failures.append(f"눈금 표시를 못 읽었습니다: {subject!r} -> {got!r}")

    # 자릿수는 **칸마다** 섞어야 합니다. 재출품 칸에만 두 자리를 넣어 뒀더니
    # `조회실패 (?P<failures>\d+)`와 `다음페이지 (?P<fired>\d+)회`를 `\d`로 줄여 놓아도
    # 눈금이 그대로 통과했습니다(2026-09-17 확인). 한 칸을 고쳤다고 다른 칸이 덮이지
    # 않습니다.
    for counters, expected_page, expected_fail in (
        ({"next_page": 2, "search_failures": 3}, {"fired": 2, "fresh": 112}, 3),
        ({"next_page": 23, "search_failures": 45}, {"fired": 23, "fresh": 112}, 45),
    ):
        check_mercari.reset_run_counters()
        for key, value in counters.items():
            check_mercari.count_event(key, value)
        check_mercari.note_max("next_page_fresh", 112)
        subject = "queue Mercari alerts" + check_mercari.run_note_text()
        parsed = parse_note(subject) or {}
        if parsed.get("next_page") != expected_page or parsed.get("search_failures") != expected_fail:
            failures.append(f"다음페이지/조회실패 왕복 불일치: {subject!r} -> {parsed!r}")

    check_mercari.reset_run_counters()
    check_mercari.note_max("page_fresh_max", check_mercari.NEXT_PAGE_NEAR_MISS_FRESH)
    subject = "queue Mercari alerts" + check_mercari.run_note_text()
    if (parse_note(subject) or {}).get("near_miss") != check_mercari.NEXT_PAGE_NEAR_MISS_FRESH:
        failures.append(f"근접 왕복 불일치: {subject!r}")

    check_mercari.reset_run_counters()
    check_mercari.note_max("page_fresh_max", check_mercari.NEXT_PAGE_NEAR_MISS_FRESH - 1)
    if check_mercari.run_note_text() != "":
        failures.append("문턱 아래인데 꼬리표가 붙었습니다")

    if _near_miss_threshold() != NEAR_MISS_FRESH:
        failures.append(
            f"근접 문턱이 갈렸습니다: 봇 {_near_miss_threshold()} vs 도구 {NEAR_MISS_FRESH}"
        )
    if _lookup_cap() <= 0:
        failures.append(f"실행당 상한을 못 읽었습니다: {_lookup_cap()!r}")

    # ② 거짓 양성
    for subject in (
        "queue Mercari alerts",
        "record Mercari alert delivery",
        "Merge pull request #38 from lhj3687-design/claude/x (y)",
        "queue Mercari alerts (재출품: 있음/없음/못물어봄)",
        "queue Mercari alerts (재출품 5: 있음2/없음3",
    ):
        got = parse_note(subject)
        if got is not None:
            failures.append(f"거짓 양성: {subject!r} -> {got!r}")

    empty = Tally()
    for _ in range(3):
        empty.add(1_700_000_000, parse_note("queue Mercari alerts"))
    if empty.relist or empty.next_page or empty.with_note:
        failures.append("아무 일도 없었던 구간이 빈 결과가 아닙니다")

    # ③ 아는 답
    #
    # 눈금 값에 **자릿수·경계·0을 섞습니다.** 그리고 같은 칸에 **여러 판이 더해지도록**
    # 짭니다 — 한 판만 두면 `+=`를 `=`로 바꿔 놓아도(누적을 잃어도) 합이 같아서
    # 눈금이 통과합니다(2026-09-17에 조회실패·다음페이지 두 칸이 실제로 그랬습니다).
    fixture = [
        (1_700_000_000, "queue Mercari alerts (재출품 5: 있음2/없음3/못물어봄0)"),
        (1_700_000_060, "queue Mercari alerts (재출품 3/9: 있음1/없음1/못물어봄1 눈금⛔일반)"),
        (1_700_000_120, "queue Mercari alerts (재출품 34: 있음10/없음11/못물어봄13, 조회실패 2)"),
        (1_700_000_180, "queue Mercari alerts (다음페이지 1회 신규112)"),
        (1_700_000_240, "queue Mercari alerts (다음페이지 근접 신규93)"),
        (1_700_000_300, "queue Mercari alerts (재출품 12/40: 있음7/없음5/못물어봄0,"
                        " 다음페이지 23회 신규118, 조회실패 45)"),
        (1_700_000_360, "queue Mercari alerts"),
        (1_700_000_420, "record Mercari alert delivery"),
        # 한 시각대 뒤. 시각대별로 가르는 자리와 '마지막 꼬리표 시각'을 함께 겁니다.
        (1_700_003_600, "queue Mercari alerts (재출품 2: 있음2/없음0/못물어봄0)"),
    ]
    tally = Tally()
    for timestamp, subject in fixture:
        tally.add(timestamp, parse_note(subject))
    known = {
        "with_note": 7,
        "targets": 90,
        "asked": 56,
        "alive": 22,
        "gone": 20,
        "unknown": 14,
        "blocked": 1,
        "fired": 24,
        "fresh_max": 118,
        "near_miss": 1,
        "search_failures": 47,
        "search_failure_runs": 2,
        # 이 둘이 '0건'과 '아직 측정이 없음'을 가르는 값입니다. 눈금에 안 걸어 두면
        # min을 max로 바꿔 놓아도 통과합니다(2026-09-17 확인).
        "first_note_at": 1_700_000_000,
        "last_note_at": 1_700_003_600,
        "inconsistent": 0,
        "hours": [22, 23],
        "hour22_targets": 88,
        "hour23_targets": 2,
    }
    got = {
        "with_note": tally.with_note,
        "targets": tally.relist["targets"],
        "asked": tally.relist["asked"],
        "alive": tally.relist["alive"],
        "gone": tally.relist["gone"],
        "unknown": tally.relist["unknown"],
        "blocked": sum(tally.blocked.values()),
        "fired": tally.next_page["fired"],
        "fresh_max": tally.next_page["fresh_max"],
        "near_miss": len(tally.near_miss),
        "search_failures": tally.search_failures,
        "search_failure_runs": tally.search_failure_runs,
        "first_note_at": tally.first_note_at,
        "last_note_at": tally.last_note_at,
        "inconsistent": tally.inconsistent,
        "hours": sorted(tally.by_hour),
        "hour22_targets": tally.by_hour[22]["targets"],
        "hour23_targets": tally.by_hour[23]["targets"],
    }
    if got != known:
        diff = {k: (got[k], known[k]) for k in known if got.get(k) != known[k]}
        failures.append(f"아는 답 불일치(값: 받은 것/기대): {diff}")

    # ④ 보고가 내는 문장까지 봅니다.
    #
    # 여기까지 안 오면 `unknown / asked`를 `unknown / targets`로 바꿔 놓아도 눈금이
    # 통과합니다 — 이 요청의 결론 숫자가 바로 그 비율입니다.
    printed = io.StringIO()
    with contextlib.redirect_stdout(printed):
        report(tally, by_hour=True)
    text = printed.getvalue()
    for needle in (
        "못 물어봄 비율 25.00%",   # 14/56. targets(90)로 나누면 15.56%가 됩니다.
        "아예 못 물어본 것 34건",  # 90 - 56
        "있음+없음+못물어봄 = 물어본 수",
        "[시각대별]",
        "22시",
        "23시",
    ):
        if needle not in text:
            failures.append(f"보고에 '{needle}'가 없습니다")

    empty_out = io.StringIO()
    with contextlib.redirect_stdout(empty_out):
        report(Tally())
    if "측정이 없는 것" not in empty_out.getvalue():
        failures.append("꼬리표가 하나도 없는 구간을 '0건'으로 찍고 있습니다")

    # ⑤ 불변식 점검이 **깨진 것을 깨졌다고 하는가.**
    broken = Tally()
    broken.add(1_700_000_000,
               parse_note("queue Mercari alerts (재출품 12/40: 있음7/없음5/못물어봄1)"))
    if broken.inconsistent != 1:
        failures.append("있음+없음+못물어봄 ≠ 물어본 수인 꼬리표를 그냥 지나갑니다")

    # 상한의 두 원인이 갈리는가. 뭉치면 '상한을 올리면 된다'와 '올려도 안 된다'가
    # 같은 숫자로 보입니다.
    cap = _lookup_cap()
    split = Tally()
    split.add(1_700_000_000, parse_note(
        f"queue Mercari alerts (재출품 {cap}/40: 있음{cap}/없음0/못물어봄0)"))       # 건수
    split.add(1_700_000_060, parse_note(
        f"queue Mercari alerts (재출품 {cap - 3}/40: 있음{cap - 3}/없음0/못물어봄0)"))  # 시간
    if split.capped != [(40, cap), (40, cap - 3)]:
        failures.append(f"상한에 걸린 판을 못 모았습니다: {split.capped!r}")
    capped_out = io.StringIO()
    with contextlib.redirect_stdout(capped_out):
        report(split)
    if f"건수 상한({cap}건) 1회 / 시간 상한 1회" not in capped_out.getvalue():
        failures.append("건수 상한과 시간 상한을 갈라 찍지 않습니다")

    # 상한 값을 못 읽는 환경이면 **틀린 갈래를 찍지 말고 못 가른다고** 해야 합니다.
    blind = io.StringIO()
    saved = globals()["_lookup_cap"]
    globals()["_lookup_cap"] = lambda: 0
    try:
        with contextlib.redirect_stdout(blind):
            report(split)
    finally:
        globals()["_lookup_cap"] = saved
    if "가르지 못합니다" not in blind.getvalue() or "건수 상한(" in blind.getvalue():
        failures.append("상한 값을 못 읽었는데도 건수/시간을 갈라 찍었습니다")

    # ⑥ `walk()`가 **--first-parent로 걷는가.**
    #
    # 빼면 PR 브랜치의 커밋이 섞여 들어옵니다. 그 커밋들의 꼬리표는 브랜치를 딴 시점의
    # 것이라 같은 숫자를 두 번 세거나 없던 숫자를 만들어 냅니다(README "운영 이력을
    # 훑을 때"). 인자 목록을 눈으로 확인하는 대신 **진짜 저장소를 하나 만들어** 겁니다.
    with tempfile.TemporaryDirectory() as tmp:
        sandbox = Path(tmp)
        git = ["git", "-C", str(sandbox), "-c", "user.email=c@x", "-c", "user.name=c"]
        try:
            subprocess.run(["git", "init", "-q", str(sandbox)], check=True, capture_output=True)
            subprocess.run([*git, "commit", "-q", "--allow-empty", "-m",
                            "queue Mercari alerts (재출품 5: 있음5/없음0/못물어봄0)"],
                           check=True, capture_output=True)
            subprocess.run([*git, "checkout", "-q", "-b", "side"], check=True, capture_output=True)
            subprocess.run([*git, "commit", "-q", "--allow-empty", "-m",
                            "queue Mercari alerts (재출품 99: 있음99/없음0/못물어봄0)"],
                           check=True, capture_output=True)
            subprocess.run([*git, "checkout", "-q", "-"], check=True, capture_output=True)
            subprocess.run([*git, "merge", "-q", "--no-ff", "side", "-m", "Merge side"],
                           check=True, capture_output=True)
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            failures.append(f"--first-parent 점검용 저장소를 못 만들었습니다: {exc}")
        else:
            side = Tally()
            for timestamp, subject in walk(["HEAD"], sandbox):
                side.add(timestamp, parse_note(subject))
            if side.relist["asked"] != 5:
                failures.append(
                    "walk()가 --first-parent로 걷지 않습니다: 가지 커밋의 꼬리표까지 "
                    f"세었습니다(물어본 것 {side.relist['asked']}건, 5여야 합니다)"
                )

    if failures:
        print("눈금 실패:")
        for line in failures:
            print(f"  - {line}")
        return 1
    print("눈금 통과 — 왕복 9개 / 거짓 양성 6개 / 아는 답 18개 / 보고 7개"
          " / 불변식 1개 / 상한 갈래 3개 / --first-parent 1개")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--calibrate", action="store_true",
                        help="세는 자를 아는 답과 자체 점검에 대 보고 끝냅니다(어긋나면 1).")
    parser.add_argument("--by-hour", action="store_true",
                        help="시각대별로 갈라 찍습니다('못 물어봄'이 한 대에 몰리는지).")
    parser.add_argument("--repo", default=str(ROOT))
    parser.add_argument("rev", nargs="*", default=["origin/main"],
                        help="git log 에 그대로 넘길 인자 (예: origin/main --since='2026-09-16')")
    args = parser.parse_args()

    if args.calibrate:
        return calibrate()

    tally = Tally()
    for timestamp, subject in walk(args.rev or ["origin/main"], Path(args.repo)):
        tally.add(timestamp, parse_note(subject))
    report(tally, by_hour=args.by_hour)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
