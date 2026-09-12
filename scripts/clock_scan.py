#!/usr/bin/env python3
"""테스트를 여러 '가짜 벽시계' 시각에서 돌려, 시각에 따라 결과가 달라지는 곳을 찾습니다.

왜 필요한가
-----------
이 봇은 매 실행 전에 테스트를 돌리고, 실패하면 그 실행의 조회·전송을 건너뜁니다.
그래서 **테스트가 시계에 의존하면 곧바로 봇 장애**가 됩니다. 실제로 두 번 났습니다.

  - 2026-09-12 05:49~05:59 UTC: 고장 알림의 alert_id가 벽시계 구간(now // 6시간)에
    묶여 있어서, 경계 앞 10분에만 세 테스트가 깨졌습니다. 11회 연속 실패.
  - 2026-09-12 13:18 UTC: 픽스처의 등록 시각이 datetime.now() 그대로였고, 연속 생성이
    같은 값을 받는 일이 실측 약 1.7% 있었습니다.

평소 CI는 '지금 이 순간' 한 시각에서만 돌기 때문에 둘 다 통과합니다. 이 스크립트는
시각 축을 직접 훑어서 그 사각지대를 없앱니다.

쓰는 법
-------
    python scripts/clock_scan.py --day      # 하루를 5분 간격으로 (288개 시각)
    python scripts/clock_scan.py --week     # 7일을 31분 간격으로 (325개 시각)
    python scripts/clock_scan.py --repeat 200   # 같은 시각에서 200회 (경합 탐지)

--day/--week는 한 번에 몇 분 걸리므로 봇의 매분 실행 경로에는 넣지 않습니다.
상시 방어선은 tests의 test_outage_alert_ids_never_depend_on_the_wall_clock이 맡고,
이 스크립트는 시각을 다루는 코드를 건드렸을 때 사람이 직접 돌리는 용도입니다.

한계 (중요)
-----------
--day/--week는 datetime.now()를 '계산을 한 겹 더 거치는' 함수로 바꿔 끼웁니다.
그 계산이 연속 호출 사이에 시간을 벌어 주기 때문에 **시계 해상도 경합(위 13:18 사고)은
이 모드에서 재현되지 않습니다.** 그 축은 시계를 건드리지 않는 --repeat이 담당합니다.
두 모드는 서로를 대체하지 못하니 둘 다 돌려 주세요.
"""
import argparse
import contextlib
import datetime as dt
import importlib
import io
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

REAL_DATETIME = dt.datetime


def load_modules():
    """테스트 모듈을 먼저 import합니다.

    테스트 모듈이 sys.modules에 mercapi 스텁을 심은 뒤 check_mercari를 import합니다.
    순서를 뒤집으면 진짜 mercapi를 끌어와, 이 스크립트가 검사와 무관한 import 오류로
    죽습니다.
    """
    tests = importlib.import_module("test_check_mercari")
    return tests, tests.mercari


def shifted_datetime(offset: float):
    """now()가 offset초만큼 밀린 datetime 대체 클래스."""

    class Shifted(REAL_DATETIME):
        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(REAL_DATETIME.now(tz).timestamp() + offset, tz)

    return Shifted


def run_suite(tests_module) -> list[str]:
    """테스트 전체를 한 번 돌리고 실패한 테스트 이름을 돌려줍니다.

    봇 코드는 실행마다 진행 상황과 경고를 stdout·stderr 양쪽에 찍습니다. 수백 번 돌리는
    도구에서는 그게 정작 필요한 '어느 시각에 깨졌는지'를 묻어 버리므로 둘 다 삼킵니다.
    """
    suite = unittest.defaultTestLoader.loadTestsFromModule(tests_module)
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
    return sorted({case.id().rsplit(".", 1)[-1] for case, _ in result.failures + result.errors})


def scan(offsets, *, shift: bool) -> int:
    tests_module, bot = load_modules()
    failed_runs = 0

    for offset in offsets:
        if shift:
            # 프로덕션 코드와 픽스처가 같은 가짜 시각을 보게 합니다(운영과 같은 조건).
            fake = shifted_datetime(offset)
            bot.datetime = fake
            tests_module.datetime = fake

        failures = run_suite(tests_module)
        if failures:
            failed_runs += 1
            when = (REAL_DATETIME.now() + dt.timedelta(seconds=offset)).strftime("%a %H:%M")
            print(f"[실패] offset {offset:>8.0f}초 ({when}) :: {', '.join(failures)}", flush=True)

    return failed_runs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--day", action="store_true", help="하루를 5분 간격으로 훑습니다")
    group.add_argument("--week", action="store_true", help="7일을 31분 간격으로 훑습니다")
    group.add_argument("--repeat", type=int, metavar="N", help="시계를 그대로 두고 N회 반복")
    args = parser.parse_args()

    if args.day:
        offsets, shift, label = [i * 300 for i in range(288)], True, "하루 / 5분 간격"
    elif args.week:
        offsets, shift, label = [i * 1860 for i in range(325)], True, "7일 / 31분 간격"
    else:
        offsets, shift, label = [0.0] * args.repeat, False, f"같은 시각 {args.repeat}회"

    print(f"[시계 스캔] {label} — {len(offsets)}회 실행", flush=True)
    failed_runs = scan(offsets, shift=shift)

    if failed_runs:
        print(f"\n{len(offsets)}회 중 {failed_runs}회에서 테스트가 깨졌습니다.", file=sys.stderr)
        return 1
    print(f"{len(offsets)}회 전부 통과 — 이 축에서는 시각 의존성이 보이지 않습니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
