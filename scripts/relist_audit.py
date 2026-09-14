"""재출품 판정이 실제로 어떻게 났는지 운영 이력에서 다시 셉니다 (수동 실행 전용).

PR #25가 "판정 68건 중 36건이 오판"을 낼 때 쓴 계산을 스크립트로 남겨 둔 것입니다.
그때는 임시 스크립트로 돌리고 버렸는데, 다음 라운드에서 같은 숫자가 다시 필요해지자
처음부터 다시 만들어야 했습니다. 여기 둡니다.

    git fetch --unshallow origin main       # 얕은 클론이면 먼저 이걸 해야 합니다
    python scripts/relist_audit.py --since 2026-09-11T16:41 --until 2026-09-13T12:48

무엇을 세는가
-------------

재출품 판정이 나면 새 ID가 예전 지문의 **가격 이력을 물려받습니다**. 그 자리를
상태 파일 이력에서 찾습니다. 판정 그 자체는 상태 파일에 남지 않지만, 물려받은
흔적은 남습니다.

  1. 지문(`relist_fingerprints`)의 주인이 예전 ID -> 새 ID로 바뀌고,
  2. 그 새 ID가 이번 판에서 `seen`에 **처음** 들어왔고,
  3. 가격 이력을 물려받은 증거가 있다.

3번의 증거는 둘 중 하나입니다.

  - `last_alert_price != last_seen_price`. 새로 기록하는 매물은 두 값이 같습니다
    (`remember(seen, item_id, {"last_alert_price": price, "last_seen_price": price})`).
    다르다면 기준가가 다른 데서 왔다는 뜻입니다.
  - **첫 관측과 같은 실행에서 인하 알림이 나갔다.** 기준가가 없는 매물은 인하 알림을
    보낼 수 없습니다. 나갔다면 기준가를 물려받았다는 뜻입니다. (값만 보면 이 경우를
    놓칩니다 — 알림을 보내면서 기준가가 관측가로 내려가 두 값이 같아지기 때문입니다.)

**두 번째 증거를 빠뜨리면 안 됩니다.** 값만 보면 68건이 아니라 49건이 나오고,
"물려받은 판정 중 첫 관측에서 곧바로 인하 알림이 나간 19건"이 통째로 빠집니다.

눈금
----

같은 구간(2026-09-11 16:41 ~ 09-13 12:48 UTC)에서 **판정 68건 / 그중 첫 관측 인하
19건**이 나와야 PR #25와 같은 것을 세고 있는 것입니다. `--calibrate`가 확인합니다.

무엇을 못 세는가 (읽을 때 꼭 보세요)
-----------------------------------

'예전 매물이 그 뒤 다시 관측됐는가'는 **지문의 주인이 그 ID로 되돌아오는 순간**으로만
읽습니다. 거짓 양성이 없는 신호가 그것뿐이기 때문입니다(dict 순서로 복원하는 방식은
두 가지 다 거짓 양성이 났습니다 — CLAUDE.md 참고).

대신 **거짓 음성이 있습니다.** `process_items()`는 매물을 등록 시각 오름차순으로
갱신하므로, 예전 매물과 새 매물이 **같은 실행에서 함께 관측되면** 예전 매물이 먼저
지문을 쓰고 새 매물이 곧바로 덮어씁니다. 그 관측은 상태 파일에 흔적을 남기지 않습니다.

따라서

  - 오판 건수는 **하한**입니다. 실제로는 더 많을 수 있습니다.
  - '안 보인 공백'은 **상한**입니다. 실제로는 더 짧았을 수 있습니다.

'정상 판정'이라는 말도 쓰지 않습니다. 정확한 표현은 **"생존을 확인하지 못했다"**입니다.
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

from check_mercari import RELIST_ABSENCE_SECONDS  # noqa: E402

# PR #25가 같은 구간에서 낸 값입니다. 도구가 같은 것을 세고 있는지 재는 눈금입니다.
CALIBRATION = {"since": "2026-09-11T16:41", "until": "2026-09-13T12:48",
               "inherited": 68, "drop_at_first_sight": 19}


def inheritance_evidence(event: dict, drops_by_item: dict) -> str | None:
    """이 자리가 '가격 이력을 물려받은' 자리인지, 무슨 증거로 그렇게 보는지.

    돌려주는 값은 "값" / "인하알림" / None 입니다.
    """
    if event["new_alert"] != event["new_seen"]:
        # 새로 기록하는 매물은 두 값이 같습니다. 다르면 기준가가 다른 데서 왔습니다.
        return "값"
    for _base, target, ts in drops_by_item.get(event["new_id"], ()):
        # 기준가가 없는 매물은 인하 알림을 보낼 수 없습니다. 첫 관측과 같은 실행에서
        # 나갔다면 기준가를 물려받은 것입니다.
        if abs(ts - event["ts"]) <= 180 and target == event["new_seen"]:
            return "인하알림"
    return None


def parse_drop_alerts(alerts: dict) -> dict:
    """알림 id -> 처음 나타난 시각 사전을, 매물별 인하 알림 목록으로 바꿉니다."""
    drops: dict = collections.defaultdict(list)
    for alert_id, ts in alerts.items():
        parts = alert_id.split(":")
        if parts[0] == "drop" and len(parts) == 4:
            try:
                base, target = int(parts[2]), int(parts[3])
            except ValueError:
                continue  # defaultdict에 먼저 넣으면 빈 항목이 남습니다
            drops[parts[1]].append((base, target, ts))
    return drops


def first_return(old_id: str, after: float, owner_became: dict) -> float | None:
    """예전 매물이 그 뒤 다시 관측된 첫 시각.

    지문 주인이 그 ID로 바뀌는 순간만 봅니다. **어느 지문이든** 봅니다 — `title:` 지문은
    키에 가격이 박혀 있어 값이 바뀌면 키 자체가 달라지므로, 같은 지문만 보면 놓칩니다.
    """
    return next((ts for ts in owner_became.get(old_id, ()) if ts > after), None)


def state_versions(since: float, until: float) -> list:
    """구간 안 main first-parent 커밋의 (커밋, 시각).

    `--first-parent`가 필수입니다. 그냥 `git log`를 쓰면 PR 브랜치 커밋이 섞여 들어오는데,
    그 커밋들의 상태 파일은 브랜치를 딴 시점의 옛날 것이라 상태가 뒤로 갔다 오는 것처럼
    보입니다(그것 때문에 "전송 기록 유실 930건"이라는 허깨비가 잡힌 적이 있습니다).
    """
    out = subprocess.run(
        ["git", "log", "--first-parent", "--format=%H %ct", "origin/main"],
        capture_output=True, text=True, check=True,
    ).stdout
    versions = []
    for line in out.splitlines():
        if not line.strip():
            continue
        sha, ts = line.split()
        if since <= float(ts) <= until:
            versions.append((sha, float(ts)))
    versions.reverse()
    return versions


def walk_states(versions: list):
    """상태 파일을 커밋마다 체크아웃하지 않고 스트리밍으로 훑습니다.

    커밋마다 체크아웃하면 몇 시간이 걸립니다. `git cat-file --batch`면 1만 판에 몇 분입니다.
    """
    proc = subprocess.Popen(["git", "cat-file", "--batch"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        for sha, ts in versions:
            proc.stdin.write(f"{sha}:seen_items.json\n".encode())
            proc.stdin.flush()
            header = proc.stdout.readline().decode().strip()
            if " blob " not in header:
                continue
            raw = proc.stdout.read(int(header.split()[2]))
            proc.stdout.read(1)
            try:
                yield sha, ts, json.loads(raw)
            except ValueError:
                continue
    finally:
        proc.stdin.close()
        proc.wait()


def collect(versions: list, progress=None) -> tuple[list, dict, dict]:
    """이력을 한 번 훑어 (판정 후보, 지문 주인이 된 시각, 인하 알림)을 모읍니다."""
    events, alerts = [], {}
    owner_became: dict = collections.defaultdict(list)
    previous_owner, previous_seen, previous_price = {}, set(), {}
    known_alerts: set = set()
    for index, (sha, ts, data) in enumerate(walk_states(versions)):
        seen = data.get("seen") or {}
        fingerprints = data.get("relist_fingerprints") or {}
        owner = {fp: entry.get("item_id")
                 for fp, entry in fingerprints.items() if isinstance(entry, dict)}
        seen_keys = set(seen)

        current_alerts = {entry["alert_id"] for entry in (data.get("pending") or [])
                          if isinstance(entry, dict) and entry.get("alert_id")}
        current_alerts |= {a for a in (data.get("sent_alerts") or []) if isinstance(a, str)}
        for alert_id in current_alerts - known_alerts:
            alerts[alert_id] = ts
        known_alerts |= current_alerts

        if previous_owner:
            for fp, new_id in owner.items():
                old_id = previous_owner.get(fp)
                if old_id is None or new_id is None or old_id == new_id:
                    continue
                owner_became[new_id].append(ts)
                if new_id in previous_seen or new_id not in seen_keys:
                    continue
                record = seen.get(new_id) or {}
                events.append({
                    "ts": ts, "sha": sha, "fp": fp, "old_id": old_id, "new_id": new_id,
                    "old_fp_alert": previous_price.get(fp),
                    "new_alert": record.get("last_alert_price"),
                    "new_seen": record.get("last_seen_price"),
                })
        previous_owner, previous_seen = owner, seen_keys
        previous_price = {fp: entry.get("last_alert_price")
                          for fp, entry in fingerprints.items() if isinstance(entry, dict)}
        if progress and index % 500 == 0:
            progress(index, len(events))
    return events, owner_became, alerts


def audit(since: float, until: float, calibrate: bool, list_ids: bool = False) -> int:
    versions = state_versions(since, until)
    print(f"구간 안 first-parent 상태 파일 {len(versions):,}판", file=sys.stderr)
    if not versions:
        print("[중단] 구간에 커밋이 없습니다. 얕은 클론이면 "
              "`git fetch --unshallow origin main`을 먼저 하세요.", file=sys.stderr)
        return 2

    events, owner_became, alerts = collect(
        versions,
        progress=lambda i, n: print(f"  {i}/{len(versions)} 후보 {n}", file=sys.stderr),
    )
    drops = parse_drop_alerts(alerts)
    tagged = [(event, inheritance_evidence(event, drops)) for event in events]
    inherited = [event for event, evidence in tagged if evidence]
    by_drop = sum(1 for _, evidence in tagged if evidence == "인하알림")

    print()
    print(f"지문 주인 교체 + 새 ID 첫 등장      {len(events)}건")
    print(f"그중 가격 이력을 물려받은 판정      {len(inherited)}건"
          f" (값 {len(inherited) - by_drop} / 첫 관측 인하 알림 {by_drop})")

    if list_ids:
        # 판정이 '사라졌다'로 읽은 예전 매물들입니다. 그 매물이 지금도 올라와 있는지는
        # 상태 파일로는 알 수 없고 메루카리에 직접 물어봐야 합니다. 이 목록을
        # Coverage Scan(Actions 탭)의 `track` 입력에 그대로 넣으세요 — 대조군까지
        # 함께 돌면서 '없는 매물에게 물으면 없다고 답하는가'부터 확인해 줍니다.
        print(",".join(sorted({event["old_id"] for event in inherited})))
        return 0

    verdicts = []
    for event in inherited:
        back = first_return(event["old_id"], event["ts"], owner_became)
        verdicts.append({**event, "returned_at": back,
                         "gap": (back - event["ts"]) if back else None})
    misjudged = [v for v in verdicts if v["gap"] is not None]
    beyond_window = [v for v in misjudged if v["gap"] > RELIST_ABSENCE_SECONDS]

    print(f"  그중 예전 매물이 뒤에 다시 관측됨 = 오판   {len(misjudged)}건  (하한입니다)")
    print(f"    그중 공백이 {RELIST_ABSENCE_SECONDS // 60}분을 넘은 것            "
          f"{len(beyond_window)}건")
    print()
    print("  ※ 이 공백은 **예전 판정**이 어디서 걸렸는지를 보여 주는 값입니다.")
    print("    지금 판정은 시간을 기다리지 않고 예전 매물에게 직접 물어보므로, 그 매물이")
    print("    아직 올라와 있기만 하면 공백이 얼마든 전부 '별개의 매물'로 갈립니다.")
    print("    실제로 그러는지는 상태 파일로 알 수 없습니다 — 아래 --list-ids 로 예전 매물")
    print("    목록을 뽑아 Coverage Scan의 track 에 넣고 직접 물어보세요.")

    if beyond_window:
        print("\n공백이 창보다 길었던 것들 (긴 순):")
        for verdict in sorted(beyond_window, key=lambda v: -v["gap"]):
            when = datetime.fromtimestamp(verdict["ts"], timezone.utc).strftime("%m-%d %H:%M")
            print(f"   {when}  공백 {verdict['gap'] / 60:7.0f}분"
                  f"  예전={verdict['old_id']:<24} 새={verdict['new_id']:<24}"
                  f"  {verdict['fp'][:44]}")
        sellers = collections.Counter(v["fp"].split(":")[1] for v in beyond_window
                                      if v["fp"].startswith("seller:"))
        print(f"   판매자별: {dict(sellers)}")

    print("\n※ 오판 건수는 하한이고 공백은 상한입니다. 예전 매물과 새 매물이 같은 실행에서"
          "\n   함께 관측되면 나중에 갱신되는 새 매물이 지문을 덮어써서, 예전 매물의 관측이"
          "\n   상태 파일에 흔적을 남기지 않습니다.")

    if calibrate:
        ok = (len(inherited) == CALIBRATION["inherited"]
              and by_drop == CALIBRATION["drop_at_first_sight"])
        print(f"\n[눈금] PR #25가 같은 구간에서 낸 값: 판정 {CALIBRATION['inherited']}건 /"
              f" 첫 관측 인하 {CALIBRATION['drop_at_first_sight']}건"
              f" -> {'일치' if ok else '어긋남 — 세는 법이 달라졌습니다'}")
        return 0 if ok else 1
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", default=CALIBRATION["since"], help="UTC, 예: 2026-09-11T16:41")
    parser.add_argument("--until", default=CALIBRATION["until"], help="UTC, 예: 2026-09-13T12:48")
    parser.add_argument("--calibrate", action="store_true",
                        help="PR #25의 눈금과 맞는지 확인하고 어긋나면 1로 끝납니다")
    parser.add_argument("--list-ids", action="store_true",
                        help="판정이 '사라졌다'로 읽은 예전 매물 ID만 쉼표로 출력합니다"
                             " (Coverage Scan의 track 입력에 그대로 넣으세요)")
    return parser.parse_args()


def to_epoch(text: str) -> float:
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()


if __name__ == "__main__":
    args = parse_args()
    raise SystemExit(audit(to_epoch(args.since), to_epoch(args.until),
                           args.calibrate, args.list_ids))
