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

    python scripts/commit_notes.py --calibrate          # 눈금 먼저
    python scripts/commit_notes.py origin/main --since='2026-09-16 00:00'
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import types
from collections import Counter
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

    def add(self, timestamp: int, note: dict | None) -> None:
        self.commits += 1
        if note is None:
            return
        self.with_note += 1
        self.first_note_at = min(self.first_note_at or timestamp, timestamp)
        self.last_note_at = max(self.last_note_at or timestamp, timestamp)
        relist = note.get("relist")
        if relist:
            self.relist_runs += 1
            for key in ("targets", "asked", "alive", "gone", "unknown"):
                if relist[key]:
                    self.relist[key] += relist[key]
            for mark in relist["blocked"]:
                self.blocked[mark] += 1
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


def stamp(value: int | None) -> str:
    if value is None:
        return "—"
    return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def report(tally: Tally) -> None:
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


# 근접 문턱은 봇 쪽 상수를 그대로 씁니다(두 값이 갈리면 표를 잘못 읽게 됩니다).
def _near_miss_threshold() -> int:
    sys.path.insert(0, str(ROOT))
    sys.modules.setdefault("mercapi", types.ModuleType("mercapi"))
    sys.modules["mercapi"].Mercapi = object  # type: ignore[attr-defined]
    import check_mercari  # noqa: PLC0415

    return int(check_mercari.NEXT_PAGE_NEAR_MISS_FRESH)


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

    check_mercari.reset_run_counters()
    check_mercari.count_event("next_page", 2)
    check_mercari.note_max("next_page_fresh", 112)
    check_mercari.count_event("search_failures", 3)
    subject = "queue Mercari alerts" + check_mercari.run_note_text()
    parsed = parse_note(subject) or {}
    if parsed.get("next_page") != {"fired": 2, "fresh": 112} or parsed.get("search_failures") != 3:
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
    fixture = [
        "queue Mercari alerts (재출품 5: 있음2/없음3/못물어봄0)",
        "queue Mercari alerts (재출품 3/9: 있음1/없음1/못물어봄1 눈금⛔일반)",
        "queue Mercari alerts (재출품 34: 있음10/없음11/못물어봄13, 조회실패 2)",
        "queue Mercari alerts (다음페이지 1회 신규112)",
        "queue Mercari alerts (다음페이지 근접 신규93)",
        "queue Mercari alerts",
        "record Mercari alert delivery",
    ]
    tally = Tally()
    for subject in fixture:
        tally.add(1_700_000_000, parse_note(subject))
    known = {
        "with_note": 5,
        "targets": 48,
        "asked": 42,
        "alive": 13,
        "gone": 15,
        "unknown": 14,
        "blocked": 1,
        "fired": 1,
        "near_miss": 1,
        "search_failures": 2,
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
        "near_miss": len(tally.near_miss),
        "search_failures": tally.search_failures,
    }
    if got != known:
        failures.append(f"아는 답 불일치: {got} != {known}")

    if failures:
        print("눈금 실패:")
        for line in failures:
            print(f"  - {line}")
        return 1
    print("눈금 통과 — 왕복 7개 / 거짓 양성 6개 / 아는 답 10개")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--calibrate", action="store_true",
                        help="세는 자를 아는 답과 자체 점검에 대 보고 끝냅니다(어긋나면 1).")
    parser.add_argument("--repo", default=str(ROOT))
    parser.add_argument("rev", nargs="*", default=["origin/main"],
                        help="git log 에 그대로 넘길 인자 (예: origin/main --since='2026-09-16')")
    args = parser.parse_args()

    if args.calibrate:
        return calibrate()

    tally = Tally()
    for timestamp, subject in walk(args.rev or ["origin/main"], Path(args.repo)):
        tally.add(timestamp, parse_note(subject))
    report(tally)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
