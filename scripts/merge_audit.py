"""상태 병합(`merge_seen.py`)이 값을 뒤집은 자리를 운영 이력에서 셉니다 (수동 실행 전용).

PR #34가 "기준가가 병합에서 245건 올라갔다"를 낼 때 쓴 계산을, 나머지 상태 값 전부로
넓힌 것입니다. **판정 코드 안에서는 일어날 수 없는 일**만 셉니다 — 두 실행이 겹쳐
push가 충돌하면 상태 파일은 `merge_seen.py`가 **밖에서** 합치기 때문입니다.

    git fetch --unshallow origin main       # 얕은 클론이면 먼저 이걸 해야 합니다
    python scripts/merge_audit.py --calibrate
    python scripts/merge_audit.py --since 2026-09-14T19:07

무엇을 세는가
-------------

방향이나 불변식이 정해져 있어서 **뒤집히면 그 자체로 결함인** 값만 셉니다.

    seen 기준가 상승        `last_alert_price`는 알림을 보낼 때만 내려갑니다.
    지문 기준가 어긋남      지문은 `seen[item_id]`에서 그대로 베껴 씁니다. 가리키는
                            매물과 값이 다르면 판정 밖에서 누가 고쳐 쓴 것입니다.
                            (`title:` 지문은 키에 가격이 박혀 있어 가격이 바뀌면 키가
                             새로 생기고 옛 키가 옛 값을 들고 남습니다 — 정당한
                             어긋남이라 `seller:` 지문만 봅니다.)
    조회 시각 후퇴          뒤로 가면 이미 알린 매물을 다시 신규로 봅니다.
    전송 기록 유실          사라지면 이미 보낸 알림이 다시 나갑니다.
    대기열 부활             이미 보낸 알림이 대기열로 돌아온 자리.
    보류 시계 후퇴          `checks`는 오르기만, `since`는 앞으로만 갑니다. 지문의
                            주인이 바뀌면 시계는 다시 세는 게 맞지만, 그때도 **더
                            최근에 물어본 쪽**(`checked_at`)이 남아야 합니다.
    fresh 소실              '처음 봤을 때 갓 올라온 매물이었는가'가 꺼지면 신규 알림이
                            조용히 사라집니다.

**0건이 나오면 왜 0건인지 함께 봐야 합니다.** 상한에 닿은 적이 없어서 0인 값이 있고
(`sent_alerts`/`seen`은 상한을 넘겨야 잘립니다), 그 자리는 '문제 없음'이 아니라
'아직 재지 않음'입니다. 그래서 상한 도달 판 수를 같이 찍습니다.

눈금
----

`--calibrate`가 두 가지를 확인하고, 어긋나면 1로 끝냅니다.

  1. **아는 답.** 고치기 전 구간(2026-09-11 16:41 ~ 09-13 12:48 UTC)의 기준가 상승이
     **176건**(PR #23 감사값), PR #23 머지 뒤 구간이 **245건**(PR #34 실측)이어야 합니다.
  2. **못 잡는 도구가 아닌지.** 각 신호마다 일부러 어긋난 판을 만들어 넣고, 세는 자가
     실제로 그것을 집어내는지 봅니다. 이 저장소에서 가장 값진 발견이 전부 "도구를 먼저
     의심한 자리"에서 나왔습니다.
"""
import argparse
import collections
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from merge_seen import (  # noqa: E402
    MAX_RELIST_FINGERPRINTS, MAX_SEEN_ITEMS, MAX_SENT_ALERTS, asked_at, price_floor,
)

# PR #23 감사값 / PR #34 실측값입니다. 도구가 같은 것을 세고 있는지 재는 눈금입니다.
# 두 번째 줄의 끝이 17:52인 이유: README가 적은 "~ 09-14 17:51"은 분 단위 표기이고,
# 245번째 자리가 **17:51:15** 커밋입니다. 17:51로 자르면 244건이 나옵니다.
CALIBRATION = [
    ("고치기 전 44시간", "2026-09-11T16:41", "2026-09-13T12:48", 176),
    ("PR #23 머지 뒤",   "2026-09-13T17:12", "2026-09-14T17:52", 245),
]

SIGNALS = (
    "seen 기준가 상승", "지문 기준가 어긋남(지문<매물)", "지문 기준가 어긋남(지문>매물)",
    "조회 시각 후퇴", "전송 기록 유실", "대기열 부활", "보류 시계 후퇴", "fresh 소실",
)


def to_epoch(text: str) -> float:
    """구간 epoch는 손으로 계산하지 않습니다 — 하루 어긋나 176건이 29건으로 나온 적이 있습니다."""
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()


def state_versions(since: float, until: float) -> list:
    """구간에 쓸 main first-parent 커밋의 (커밋, 시각, 커밋 메시지).

    `--first-parent`가 필수입니다. 그냥 `git log`를 쓰면 PR 브랜치 커밋이 섞여 들어오고,
    그 커밋들의 상태 파일은 브랜치를 딴 시점의 옛날 것이라 상태가 뒤로 갔다 오는 것처럼
    보입니다(그것 때문에 "전송 기록 유실 930건"이라는 허깨비가 잡힌 적이 있습니다).

    **구간 바로 앞 커밋 한 판을 씨앗으로 같이 가져옵니다.** 값이 뒤집힌 자리는 두 판을
    비교해야 보이는데, 씨앗이 없으면 구간 첫 판이 비교 상대를 잃어 **경계에서 한 건이
    조용히 빠집니다**(눈금이 245건 대신 244건으로 나왔습니다).
    """
    out = subprocess.run(
        ["git", "log", "--first-parent", "--format=%H %ct %s", "origin/main"],
        capture_output=True, text=True, check=True,
    ).stdout
    versions, seed = [], None
    for line in out.splitlines():
        if not line.strip():
            continue
        sha, ts, subject = line.split(" ", 2)
        ts = float(ts)
        if since <= ts < until:
            versions.append((sha, ts, subject))
        elif ts < since and (seed is None or ts > seed[1]):
            seed = (sha, ts, subject)
    versions.reverse()
    return ([seed] if seed else []) + versions


def walk_states(versions: list):
    """커밋마다 체크아웃하지 않고 스트리밍으로 훑습니다(1만 판에 몇 분)."""
    proc = subprocess.Popen(["git", "cat-file", "--batch"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        for sha, ts, subject in versions:
            proc.stdin.write(f"{sha}:seen_items.json\n".encode())
            proc.stdin.flush()
            header = proc.stdout.readline().decode().strip()
            if " blob " not in header:
                continue
            raw = proc.stdout.read(int(header.split()[2]))
            proc.stdout.read(1)
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            if isinstance(data, dict) and isinstance(data.get("seen"), dict):
                yield sha, ts, subject, data
    finally:
        proc.stdin.close()
        proc.wait()


def fingerprint_gaps(state: dict) -> tuple[int, int]:
    """지문이 '그 지문이 가리키는 매물'과 다른 기준가를 들고 있는 자리 (지문<매물, 지문>매물).

    `process_items()`는 지문을 언제나 `seen[item_id]`에서 그대로 베껴 씁니다. 그러니
    `seller:` 지문이 seen 안의 매물을 가리키는 동안에는 두 값이 같아야 합니다.
    """
    seen = state.get("seen") or {}
    low = high = 0
    for key, value in (state.get("relist_fingerprints") or {}).items():
        if not key.startswith("seller:") or not isinstance(value, dict):
            continue
        owner = value.get("item_id")
        theirs, ours = price_floor(value), price_floor(seen.get(owner))
        if owner is None or theirs is None or ours is None:
            continue
        if theirs < ours:
            low += 1
        elif theirs > ours:
            high += 1
    return low, high


def compare(previous: dict, current: dict) -> dict:
    """두 판 사이에 '있을 수 없는 방향'으로 움직인 값을 셉니다."""
    found = collections.Counter()

    previous_seen, seen = previous.get("seen") or {}, current.get("seen") or {}
    for key, value in seen.items():
        was, now = price_floor(previous_seen.get(key)), price_floor(value)
        if was is not None and now is not None and now > was:
            found["seen 기준가 상승"] += 1

    was_low, was_high = fingerprint_gaps(previous)
    low, high = fingerprint_gaps(current)
    found["지문 기준가 어긋남(지문<매물)"] += max(0, low - was_low)
    found["지문 기준가 어긋남(지문>매물)"] += max(0, high - was_high)

    previous_checked = previous.get("keyword_checked_at") or {}
    for key, value in (current.get("keyword_checked_at") or {}).items():
        was = previous_checked.get(key)
        if isinstance(was, (int, float)) and isinstance(value, (int, float)) and value < was:
            found["조회 시각 후퇴"] += 1

    was_sent = {str(v) for v in (previous.get("sent_alerts") or [])}
    sent = {str(v) for v in (current.get("sent_alerts") or [])}
    found["전송 기록 유실"] += len(was_sent - sent)
    for entry in current.get("pending") or []:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("alert_id") or f"legacy:{entry.get('caption', '')}")
        if key in was_sent:
            found["대기열 부활"] += 1

    previous_pending = previous.get("pending_relists") or {}
    for item_id, entry in (current.get("pending_relists") or {}).items():
        was = previous_pending.get(item_id)
        if not isinstance(was, dict) or not isinstance(entry, dict):
            continue
        if bool(was.get("fresh")) and not bool(entry.get("fresh")):
            found["fresh 소실"] += 1
        if was.get("matched_id") != entry.get("matched_id"):
            # 다른 질문이라 시계를 다시 세는 것은 맞습니다. 그래도 **더 최근에 물어본
            # 쪽**이 남아야 합니다 — 그 매물을 이번 실행에서 보지도 않은 쪽이 이기면
            # checks 가 두 값 사이를 오가며 상한에 영영 닿지 못합니다.
            if asked_at(entry) < asked_at(was):
                found["보류 시계 후퇴"] += 1
            continue
        for key, worse in (("checks", lambda a, b: b < a), ("since", lambda a, b: b > a)):
            a, b = was.get(key), entry.get(key)
            if isinstance(a, (int, float)) and isinstance(b, (int, float)) and worse(a, b):
                found["보류 시계 후퇴"] += 1
    # Counter 는 0을 더해도 키가 남습니다. 그대로 두면 '아무 일도 없었다'가 빈 Counter 가
    # 아니라서, 거짓 양성이 있는지 보는 자체 점검이 언제나 통과해 버립니다.
    return collections.Counter({name: count for name, count in found.items() if count})


def audit(since: float, until: float, quiet: bool = False) -> tuple[collections.Counter, dict, int]:
    versions = state_versions(since, until)
    if not quiet:
        print(f"구간 안 first-parent 상태 파일 {len(versions):,}판", file=sys.stderr)
    found = collections.Counter()
    caps = collections.Counter()
    pairs = 0
    previous = None
    for index, (_sha, ts, _subject, data) in enumerate(walk_states(versions)):
        if ts < since:
            previous = data  # 경계 앞 씨앗 한 판. 세지는 않고 비교 상대로만 씁니다.
            continue
        for name, store, limit in (("seen", data.get("seen"), MAX_SEEN_ITEMS),
                                   ("relist_fingerprints", data.get("relist_fingerprints"),
                                    MAX_RELIST_FINGERPRINTS),
                                   ("sent_alerts", data.get("sent_alerts"), MAX_SENT_ALERTS)):
            if store is not None and len(store) >= limit:
                caps[name] += 1
        if previous is not None:
            pairs += 1
            found += compare(previous, data)
        previous = data
        if not quiet and index and index % 2000 == 0:
            print(f"  {index:,}/{len(versions):,}", file=sys.stderr)
    return found, caps, pairs


def self_test() -> bool:
    """세는 자가 정말 잡아내는지, 일부러 어긋난 판을 만들어 넣고 봅니다.

    "문제 없음"이라고 말하기 전에 그 확인 방법이 문제를 잡는지부터 봐야 합니다 —
    이 저장소에서 여섯 라운드 연속으로 가장 값진 발견이 그 자리에서 나왔습니다.
    """
    base = {
        "seen": {"m1": {"last_alert_price": 5000, "last_seen_price": 5000}},
        "pending": [], "sent_alerts": ["new:m1"],
        "relist_fingerprints": {"seller:A:t": {"item_id": "m1", "last_alert_price": 5000,
                                               "last_seen_price": 5000}},
        "keyword_checked_at": {"kw": 1000},
        "pending_relists": {"m2": {"matched_id": "m1", "since": 900, "checks": 2,
                                   "checked_at": 950, "fresh": True}},
    }

    def broken(**changes):
        state = json.loads(json.dumps(base))
        for path, value in changes.items():
            target = state
            *parents, leaf = path.split(".")
            for step in parents:
                target = target[step]
            target[leaf] = value
        return state

    cases = [
        ("seen 기준가 상승", broken(**{"seen.m1": {"last_alert_price": 6000,
                                                   "last_seen_price": 6000}})),
        ("지문 기준가 어긋남(지문<매물)",
         broken(**{"relist_fingerprints.seller:A:t": {"item_id": "m9",
                                                      "last_alert_price": 5000,
                                                      "last_seen_price": 9000},
                   "seen.m9": {"last_alert_price": 9000, "last_seen_price": 9000}})),
        ("조회 시각 후퇴", broken(**{"keyword_checked_at.kw": 500})),
        ("전송 기록 유실", broken(sent_alerts=[])),
        ("대기열 부활", broken(pending=[{"alert_id": "new:m1", "caption": "다시"}])),
        ("보류 시계 후퇴", broken(**{"pending_relists.m2": {"matched_id": "m1", "since": 900,
                                                            "checks": 1, "checked_at": 950,
                                                            "fresh": True}})),
        ("보류 시계 후퇴(주인이 바뀌며 옛 기록이 이김)",
         broken(**{"pending_relists.m2": {"matched_id": "m8", "since": 800, "checks": 0,
                                          "checked_at": 800, "fresh": True}})),
        ("fresh 소실", broken(**{"pending_relists.m2": {"matched_id": "m1", "since": 900,
                                                        "checks": 2, "checked_at": 950,
                                                        "fresh": False}})),
    ]

    ok = True
    print("[자체 점검] 일부러 어긋낸 판을 넣어 세는 자가 잡는지 봅니다")
    if any(compare(base, json.loads(json.dumps(base))).values()):
        print("  ⛔ 아무것도 안 바꿨는데 신호가 떴습니다 — 거짓 양성")
        ok = False
    else:
        print("  ✅ 안 바꾼 판에서는 아무 신호도 뜨지 않습니다")
    for name, state in cases:
        signal = name.split("(")[0] if name.startswith("보류") else name
        caught = compare(base, state).get(signal, 0)
        print(f"  {'✅' if caught else '⛔'} {name}: {caught}건")
        ok = ok and bool(caught)
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", default="2026-09-14T19:07", help="UTC, 예: 2026-09-14T19:07")
    parser.add_argument("--until", default="2099-01-01T00:00", help="UTC")
    parser.add_argument("--calibrate", action="store_true",
                        help="아는 답과 자체 점검을 확인하고 어긋나면 1로 끝납니다")
    args = parser.parse_args()

    if args.calibrate:
        ok = self_test()
        print()
        for label, since, until, expected in CALIBRATION:
            found, _caps, pairs = audit(to_epoch(since), to_epoch(until), quiet=True)
            actual = found["seen 기준가 상승"]
            match = actual == expected
            ok = ok and match
            print(f"[눈금] {label}({since} ~ {until} UTC): 기준가 상승 {actual}건 "
                  f"/ 기대 {expected}건 -> {'일치' if match else '어긋남 — 세는 법이 달라졌습니다'}")
        if not ok:
            print("\n⛔ 눈금이 안 맞습니다. 이 도구가 낸 숫자를 읽지 마세요.")
        return 0 if ok else 1

    found, caps, pairs = audit(to_epoch(args.since), to_epoch(args.until))
    print(f"\n비교한 상태 파일 쌍 {pairs:,}개 ({args.since} ~ {args.until} UTC)\n")
    for name in SIGNALS:
        print(f"  {name:32} {found.get(name, 0):>8,}건")
    print(f"\n  상한에 닿은 판: {dict(caps) or '없음'}")
    if not caps:
        print("  ※ 상한에 한 번도 안 닿았습니다. `전송 기록 유실`과 seen 순서가 만드는 손실은")
        print("     상한을 넘겨야 드러나므로, 그 자리의 0건은 '문제 없음'이 아니라 '아직 재지 않음'입니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
