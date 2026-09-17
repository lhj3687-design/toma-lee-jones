"""재출품이 물려받은 기준가가 `seen` 원본과 맞는지 운영 이력에서 셉니다 (수동 실행 전용).

PR #37의 실전 확인용입니다.

    git fetch --unshallow origin main       # 얕은 클론이면 먼저 이걸 해야 합니다
    python scripts/relist_inherit_audit.py --calibrate
    python scripts/relist_inherit_audit.py --since 2026-09-15T14:26 --list

무엇을 세는가
-------------

지문 값(`relist_fingerprints[fp]`)은 `process_items()`가 그 매물을 관측할 때
`seen[item_id]`에서 그대로 베낀 **사본**입니다. 사본은 두 방향으로 낡습니다.

  - **비싼 쪽(지문>매물)** — 판매자가 값을 내리면서 제목을 고치면 지문 키가 새로 생기고
    옛 키가 옛 가격을 들고 남습니다. 새 매물이 옛 키에 걸리면 이미 알린 값보다 비싼
    기준가를 물려받아, PR #23이 없앤 결함('역대 최저가'라며 이미 알린 최저가보다 비싼
    값을 말하는 인하 알림)이 재출품 경로로 되살아납니다.
  - **싼 쪽(지문<매물)** — 병합이 남의 기준가를 얹어 놓은 기록(PR #35가 새로 생기는 길은
    막았지만 이미 얹힌 것은 안 돌아옵니다). 그 사본을 물려받으면 인하 알림이 조용히
    삼켜집니다.

PR #37은 **저장된 값을 고치지 않고**, 쓰는 순간에만 주인의 원본을 봅니다
(`relist_baseline`). 그러니 확인할 것은 하나입니다 —
**물려받은 기준가가 `seen` 원본과 어긋나는 자리가 0건인가.**

`merge_audit.py`의 `지문 기준가 어긋남(지문>매물)`과 **헷갈리면 안 됩니다.** 그쪽은
머지 뒤에도 계속 나옵니다(저장된 값을 안 고치는 것이 의도적 선택이라서). 이제 그 신호는
'판매자가 제목을 고친 횟수'로 읽습니다. 여기서 세는 것은 그 낡은 사본이 **실제로 판정에
쓰였는가**입니다.

세는 층이 셋입니다 — 층을 섞으면 숫자가 다른 질문의 답이 됩니다
---------------------------------------------------------------

  1층  **물려받을 수 있었던 자리.** 지문 주인이 A -> B 로 바뀌고 B가 그 판에서 `seen`에
       처음 들어온 자리(`relist_audit.py`와 같은 정의).
  2층  그중 **사본이 원본과 달랐던 자리.** 두 값이 같으면 고침이 있으나 없으나 결과가
       같아서, 그런 자리만 쌓인 구간의 "어긋남 0건"은 **아무 말도 하지 않습니다.**
       방향(비쌈/쌈)으로 갈라 찍습니다 — 처방이 다릅니다.
  3층  그중 **사본이 쓰였는지 가려지는 자리.** 이것이 분모입니다.

3층을 어떻게 가르는가 — 가설을 세우고 기록으로 배제합니다
--------------------------------------------------------

기준가가 무엇이었는지는 상태 파일에 안 적힙니다. 대신 **그 기준가였다면 `seen[B]`가
어떤 모양이 되는지**는 `process_items()`가 정해 놓았습니다. 그래서 가설 셋을 세우고
관측된 기록으로 지웁니다.

    원본을 물려받음     기준가 = `seen[A]`  (고친 뒤 `relist_baseline`이 하는 일)
    사본을 물려받음     기준가 = 지문 사본  (고치기 전)
    안 물려받음         판정이 '별개의 매물'로 끝나 관측가가 그대로 기준

가설마다 예측이 둘입니다 — **기록**(`last_alert_price`, `last_seen_price`)과 **인하
알림**입니다. 알림 id 가 `drop:{매물}:{기준가}:{관측가}`라 기준가를 그대로 들고 있어서,
기록이 같아 보이는 두 가설도 알림으로 갈립니다. 나가야 할 알림이 안 나갔거나, 나가면
안 될 알림이 나갔으면 그 가설은 지워집니다.

알림은 **그 자리와 같은 판**의 대기열에서 찾습니다. 시각 창으로 찾으면 정지가 끼었을 때
놓치고, 놓치면 하필 '사본을 안 썼다'는 **안심되는 쪽**으로 틀립니다.

'안 물려받음'은 **원본 쪽에 붙입니다** — 그 자리도 낡은 사본을 쓰지 않았고, PR #37이
어긋났다고 할 자리가 아니기 때문입니다. 사본 가설과 나머지가 **둘 다 서면 못 가릅니다.**
그때 ✅로 세면 도구가 거짓말을 합니다(고치기 전 구간에도 그런 자리가 있습니다).

눈금
----

`--calibrate`가 넷을 확인하고, 하나라도 어긋나면 1로 끝냅니다.

  1. **아는 답(1층).** 고치기 전 창(2026-09-13 02:44:34 ~ 09-15 14:26 UTC, 상태 파일
     **7,338판**)에서 물려받을 수 있었던 자리가 **257건**이어야 합니다(PR #37 실측).
     판 수와 자리 수가 **함께** 맞아야 합니다 — 창을 3분만 넓히면 한 판에 몰린 32건이
     들어와 289건이 됩니다.
  2. **아는 답(2층).** 그중 사본이 **비싼 쪽**으로 낡은 자리가 **1건**이어야 합니다.
     README의 "257건 중 1건"이 이 값입니다. **'사본이 낡은 자리 3건'이 아닙니다** —
     나머지 2건은 싼 쪽이고, 그 질문의 답이 아닙니다.
  3. **아는 답(그 한 건의 값).** 그 1건은 ¥215,000을 물려줬는데 원본은 ¥200,000이었습니다.
     건수만 맞추면 다른 것을 세고도 통과할 수 있어서 값까지 봅니다.
  4. **못 잡는 도구가 아닌지.** 판을 일부러 어긋내 넣고 세는 자가 집어내는지 봅니다.
     자릿수는 **칸마다가 아니라 숫자마다** 섞습니다(0 · 두 자리 · 세 자리 · 여섯 자리 ·
     일곱 자리 · 문턱 경계 999/1,000). 이 저장소는 그 자리에서 세 라운드 연속 데었습니다.
"""
import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from check_mercari import PRICE_DROP_ALERT_THRESHOLD, price_record  # noqa: E402

# PR #37 실측값입니다(README "지문이 들고 있는 값은 사본입니다"). 도구가 같은 것을 세고
# 있는지 재는 눈금입니다. 창은 **판 수로 고정**했습니다 — README가 적은 "2.5일"을 분
# 단위로 잘라 맞추면 3분 차이로 한 판에 몰린 32건이 들락거립니다.
CALIBRATION = {
    "since": "2026-09-13T02:44:34",
    "until": "2026-09-15T14:26",
    "versions": 7338,     # README "상태 파일 7,338판"
    "sites": 257,         # README "물려받은 자리 257건"
    "stale_expensive": 1, # README "그중 1건"
    "copy": 215000,       # README "215,000을 물려줬는데"
    "origin": 200000,     # README "진짜는 200,000"
}

# 상태 커밋 제목 끝의 꼬리표(PR #38)에서 '없어짐' 건수를 읽습니다.
#     queue Mercari alerts (재출품 12/30: 있음12/없음0/못물어봄0, 직접조회 …)
# **자릿수를 한 자리로 적지 마세요.** 이 저장소가 세 라운드 연속으로 데인 자리입니다
# (`\d` 로 줄이면 '없음12'가 '없음1'로 읽힙니다).
RUN_GONE_PATTERN = re.compile(r"재출품 \d+(?:/\d+)?: 있음\d+/없음(?P<gone>\d+)")

# PR #37 머지 시각(UTC). 이 앞뒤로 같은 자리의 뜻이 달라집니다.
FIX_MERGED_AT = "2026-09-15T14:26"

# 판정 이름. 앞의 둘은 '고침이 결과를 바꿀 수 없었던 자리'(분모 밖)이고,
# 나머지가 '사본이 쓰였는가'에 답하는 자리입니다.
NO_ORIGIN = "주인이 seen 에 없음"
FRESH_COPY = "사본이 원본과 같음"
NOT_COPY = "사본을 쓰지 않음"
FROM_COPY = "사본을 물려받음"
UNDECIDED = "가를 수 없음"
UNEXPLAINED = "설명이 안 됨"
VERDICTS = (NO_ORIGIN, FRESH_COPY, NOT_COPY, FROM_COPY, UNDECIDED, UNEXPLAINED)
# 사본이 쓰였는지 **가려진** 자리. 이것이 분모입니다.
DECIDED = (NOT_COPY, FROM_COPY, UNEXPLAINED)


def run_gone(subject: str):
    """이 판에서 **직접 조회가 '없어짐'으로 답한 건수.** 꼬리표가 없으면 None.

    `survival[matched_id] is False`면 `confirm_disappearance()`가 곧바로 'gone'을 내고
    `record = relist_baseline(...)`이 됩니다. 그러니 **`없음0`인 판에서는 이번 라운드에
    물려받은 자리가 하나도 없습니다** — 상태 파일만으로 못 가르던 자리가 이걸로 갈립니다.

    꼬리표는 2026-09-15 16:18(PR #38 배포)부터 붙습니다. 그 앞 구간의 없음을
    **0으로 읽으면 안 됩니다** — '0건'이 아니라 '측정이 없음'입니다. 그래서 None 입니다.
    """
    match = RUN_GONE_PATTERN.search(subject or "")
    return int(match.group("gone")) if match else None


def to_epoch(text: str) -> float:
    """구간 epoch는 손으로 계산하지 않습니다 — 하루 어긋나 176건이 29건으로 나온 적이 있습니다."""
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()


def state_versions(since: float, until: float) -> list:
    """구간에 쓸 main first-parent 커밋의 (커밋, 시각)과, 구간 앞 씨앗 한 판.

    `--first-parent`가 필수입니다. 그냥 `git log`를 쓰면 PR 브랜치 커밋이 섞여 들어오고,
    그 커밋들의 상태 파일은 브랜치를 딴 시점의 옛날 것이라 상태가 뒤로 갔다 오는 것처럼
    보입니다(그것 때문에 "전송 기록 유실 930건"이라는 허깨비가 잡힌 적이 있습니다).

    **구간 바로 앞 한 판을 씨앗으로 같이 가져옵니다.** 물려받은 자리는 두 판을 비교해야
    보이는데, 씨앗이 없으면 구간 첫 판이 비교 상대를 잃어 경계에서 한 건이 조용히
    빠집니다(`merge_audit.py`에서 245건이 244건으로 나왔던 자리).

    돌려주는 목록의 길이는 `씨앗 + 구간 안 판 수`입니다. 씨앗은 세지 않습니다.
    """
    out = subprocess.run(
        ["git", "log", "--first-parent", "--format=%H %ct %s", "origin/main"],
        capture_output=True, text=True, check=True,
    ).stdout
    versions, seed = [], None
    for line in out.splitlines():
        if not line.strip():
            continue
        sha, ts, subject = (line.split(" ", 2) + [""])[:3]
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


def owners(state: dict) -> dict:
    return {fp: entry.get("item_id")
            for fp, entry in (state.get("relist_fingerprints") or {}).items()
            if isinstance(entry, dict)}


def copies(state: dict) -> dict:
    """지문이 들고 있는 기준가 **사본**."""
    return {fp: entry.get("last_alert_price")
            for fp, entry in (state.get("relist_fingerprints") or {}).items()
            if isinstance(entry, dict)}


def item_drops(state: dict, item_id) -> set:
    """이 판의 대기열·전송 기록에 들어 있는 그 매물의 인하 알림 {(기준가, 관측가)}.

    알림은 그 실행에서 곧바로 `pending`에 들어가고 **같은 판에 커밋됩니다.** 그래서
    시각 창이 아니라 같은 판만 봅니다 — 시각 창으로 찾으면 정지가 끼었을 때 놓치고,
    놓치면 하필 '사본을 안 썼다'는 안심되는 쪽으로 틀립니다.

    B는 이 판에서 처음 보는 매물이라 예전 알림이 섞일 일이 없습니다.
    """
    ids = {entry.get("alert_id") for entry in (state.get("pending") or [])
           if isinstance(entry, dict)}
    ids |= {alert for alert in (state.get("sent_alerts") or []) if isinstance(alert, str)}
    found = set()
    for alert_id in ids:
        if not isinstance(alert_id, str):
            continue
        parts = alert_id.split(":")
        if len(parts) != 4 or parts[0] != "drop" or parts[1] != str(item_id):
            continue
        try:
            found.add((int(parts[2]), int(parts[3])))
        except ValueError:
            continue
    return found


def sites(previous: dict, current: dict, ts: float = 0.0, sha: str = "",
          subject: str = "") -> list:
    """두 판 사이에 '재출품이 기준가를 물려받을 수 있었던' 자리와, 그 자리의 값 셋 (1층)."""
    previous_owner, owner = owners(previous), owners(current)
    if not previous_owner:
        return []
    previous_copy = copies(previous)
    previous_seen, seen = previous.get("seen") or {}, current.get("seen") or {}
    found = []
    for fp, new_id in owner.items():
        old_id = previous_owner.get(fp)
        if old_id is None or new_id is None or old_id == new_id:
            continue
        if new_id in previous_seen or new_id not in seen:
            continue  # B가 이 판에서 seen 에 처음 들어온 자리만 셉니다
        record = price_record(seen, new_id)
        found.append({
            "ts": ts, "sha": sha, "fp": fp, "old_id": old_id, "new_id": new_id,
            "copy": previous_copy.get(fp),
            "origin": price_record(previous_seen, old_id)["last_alert_price"],
            "origin_present": old_id in previous_seen,
            "new_alert": record["last_alert_price"],
            "new_seen": record["last_seen_price"],
            "drops": item_drops(current, new_id),
            "run_gone": run_gone(subject),
        })
    for site in found:
        site["run_sites"] = len(found)
    return found


def predicted(baseline, price) -> tuple:
    """`baseline`을 물려받았다면 이 판의 (`seen[B]` 기록, 나가야 할 인하 알림)은 무엇인가.

    `process_items()`의 그 대목을 그대로 옮긴 것입니다.

      - 기준가가 없으면 이번 관측가가 그대로 기준이 됩니다.
      - 문턱(1,000엔)만큼 싸면 인하 알림이 나가고 기준가가 관측가로 내려갑니다.
      - 아니면 기준가는 그대로 남고 관측가만 갱신됩니다.
    """
    if not isinstance(price, int):
        return None, None          # 가격을 모르는 판은 가설을 가를 수 없습니다
    if not isinstance(baseline, int):
        return (price, price), None
    if price <= baseline - PRICE_DROP_ALERT_THRESHOLD:
        return (price, price), (baseline, price)
    return (baseline, price), None


def consistent(baseline, site: dict) -> bool:
    """이 자리의 기록이 '`baseline`을 물려받았다'로 설명되는가.

    **인하 알림까지 봅니다.** 알림 id 가 `drop:{매물}:{기준가}:{관측가}`라 기준가를
    그대로 들고 있어서, 기록이 같아 보이는 두 가설도 알림으로 갈립니다. 나가야 할
    알림이 안 나갔거나 **나가면 안 될 알림이 나갔으면** 그 가설은 안 섭니다.
    """
    record, drop = predicted(baseline, site["new_seen"])
    if record is None:
        return True                # 가격을 모르는 판은 모든 가설을 살립니다
    if (site["new_alert"], site["new_seen"]) != record:
        return False
    if drop is None:
        # 이 가설은 "인하 알림이 나가면 안 된다"고도 말합니다. 나갔으면 안 섭니다.
        return not site["drops"]
    return drop in site["drops"]


def verdict(site: dict) -> str:
    """이 자리에서 **낡은 사본이 쓰였는가.**

    가설 둘을 세우고 관측된 기록으로 배제합니다 — 기준가가 `seen` 원본(고친 뒤)이었나,
    지문 사본(고치기 전)이었나. 셋째 가설 '아무것도 안 물려받음'(판정이 '별개의
    매물'로 끝난 자리)은 **원본 쪽에 붙입니다** — 그 자리도 낡은 사본을 쓰지 않았고,
    PR #37이 어긋났다고 할 자리가 아니기 때문입니다.

    둘 다 서면 **가를 수 없습니다.** 그때 ✅로 세면 도구가 거짓말을 합니다.
    """
    if not site["origin_present"]:
        # 주인이 seen 에서 밀려났으면 relist_baseline 도 사본을 씁니다. 고침이 닿지 않습니다.
        return NO_ORIGIN
    if site["copy"] == site["origin"]:
        # 사본이 안 낡았습니다. 고침이 있으나 없으나 결과가 같아 아무 말도 못 합니다.
        return FRESH_COPY
    copy_stands = site["copy_stands"]
    clean_stands = site["origin_stands"] or site["none_stands"]
    if copy_stands and clean_stands:
        # 상태 파일만으로는 못 가릅니다. 꼬리표가 '없음0'이라고 하면 **이 판에는
        # 물려받은 자리가 하나도 없었다**는 뜻이라, 사본 가설이 지워집니다.
        if site.get("run_gone") == 0:
            return NOT_COPY
        return UNDECIDED
    if copy_stands:
        return FROM_COPY
    if clean_stands:
        return NOT_COPY
    return UNEXPLAINED


def direction(site: dict) -> str:
    """사본이 어느 쪽으로 낡았는가. 처방이 다릅니다."""
    copy, origin = site["copy"], site["origin"]
    if not isinstance(copy, int) or not isinstance(origin, int) or copy == origin:
        return ""
    return "비쌈(지문>매물)" if copy > origin else "쌈(지문<매물)"


def pinned(site: dict) -> bool:
    """이 자리에 물려받음이 **못 박히는가.**

    꼬리표가 '없어짐 1'이라고 하고 그 판의 1층 자리가 **하나뿐**이면, 그 한 번의
    물려받음은 이 자리일 수밖에 없습니다. 둘 이상이거나 없어짐이 여럿이면 어느 자리가
    물려받았는지 못 고릅니다 — 그때 '못 박혔다'고 하면 도구가 거짓말을 합니다.

    `없어짐` 건수를 **한 자리로만 읽으면** '없어짐 12'인 판이 '1'로 읽혀 여기서
    엉뚱하게 못 박힙니다.
    """
    return site.get("run_gone") == 1 and site.get("run_sites") == 1


def stale_sites(found: list) -> list:
    """2층. 사본이 원본과 달랐던 자리 (= 고침이 결과를 바꿀 수 있었던 자리)."""
    return [site for site in found
            if site["origin_present"] and site["copy"] != site["origin"]]


def audit(since: float, until: float, quiet: bool = False) -> tuple:
    """구간을 한 번 훑어 (자리 목록, 비교한 쌍 수, 구간 안 판 수)를 돌려줍니다."""
    versions = state_versions(since, until)
    in_window = sum(1 for _sha, ts, _subject in versions if ts >= since)
    if not quiet:
        print(f"구간 안 first-parent 상태 파일 {in_window:,}판", file=sys.stderr)
    found, pairs, previous = [], 0, None
    for index, (sha, ts, subject, data) in enumerate(walk_states(versions)):
        if ts < since:
            previous = data   # 경계 앞 씨앗 한 판. 세지는 않고 비교 상대로만 씁니다.
            continue
        if previous is not None:
            pairs += 1
            found += sites(previous, data, ts, sha, subject)
        previous = data
        if not quiet and index and index % 2000 == 0:
            print(f"  {index:,}/{len(versions):,} 물려받을 수 있었던 자리 {len(found)}",
                  file=sys.stderr)
    for site in found:
        classify(site)
    return found, pairs, in_window


def classify(site: dict) -> dict:
    """가설 셋이 각각 서는지 적어 둡니다. `verdict()`가 이걸 읽습니다."""
    site["origin_stands"] = consistent(site["origin"], site)
    site["copy_stands"] = consistent(site["copy"], site)
    site["none_stands"] = consistent(None, site)
    return site


def describe(site: dict) -> str:
    when = datetime.fromtimestamp(site["ts"], timezone.utc).strftime("%m-%d %H:%M:%S")
    stands = [name for name, on in (("원본", site["origin_stands"]),
                                    ("사본", site["copy_stands"]),
                                    ("안물려받음", site["none_stands"])) if on]
    drops = ("·".join(f"drop {base}->{target}" for base, target in sorted(site["drops"]))
             or "인하알림 없음")
    gone = site.get("run_gone")
    tag = "꼬리표없음" if gone is None else f"그 판 없어짐{gone}/자리{site.get('run_sites')}"
    if pinned(site):
        tag += " 못박힘"
    return (f"{when} {verdict(site):<12} {direction(site):<14} [{tag}] "
            f"사본 {site['copy']} / 원본 {site['origin']} "
            f"-> 기록 {site['new_alert']}/{site['new_seen']}, {drops}\n"
            f"      서는 가설: {' + '.join(stands) or '없음'}   "
            f"{site['old_id']} -> {site['new_id']}  {site['fp'][:52]}")


def report(found: list, pairs: int, in_window: int, since: str, until: str,
           list_sites: bool = False) -> int:
    counts = {name: 0 for name in VERDICTS}
    for site in found:
        counts[verdict(site)] += 1
    stale = stale_sites(found)
    expensive = [s for s in stale if direction(s).startswith("비쌈")]
    cheap = [s for s in stale if direction(s).startswith("쌈")]
    decided = [s for s in stale if verdict(s) in DECIDED]
    wrong = [s for s in decided if verdict(s) != NOT_COPY]
    # 꼬리표가 '없어짐 >= 1'이라고 한 판의 자리. 그 판에서 실제로 무언가가 물려받았고,
    # 사본이 낡아 있었으니 **고침이 실제로 든 자리**입니다. 나머지는 고침이 있으나
    # 없으나 결과가 같아서, 거기서 나온 '어긋남 0건'은 아무 말도 하지 않습니다.
    exercised = [s for s in stale if (s.get("run_gone") or 0) >= 1]
    nailed = [s for s in exercised if pinned(s)]
    untagged = [s for s in stale if s.get("run_gone") is None]

    print(f"\n비교한 상태 파일 쌍 {pairs:,}개 / 구간 안 {in_window:,}판 "
          f"({since} ~ {until} UTC)\n")
    print(f"  1층  물려받을 수 있었던 자리            {len(found):>6,}건")
    print(f"  2층  그중 사본이 원본과 달랐던 자리     {len(stale):>6,}건"
          f"   (비쌈 {len(expensive)} / 쌈 {len(cheap)})")
    print(f"  3층  그중 사본이 쓰였는지 가려진 자리   {len(decided):>6,}건"
          f"   <- 이것이 분모입니다")
    for name in DECIDED:
        hit = sum(1 for s in stale if verdict(s) == name)
        print(f"         {'✅' if name == NOT_COPY else '⛔'} {name:<16} {hit:>6,}건")
    print(f"       (가를 수 없음 {sum(1 for s in stale if verdict(s) == UNDECIDED)}건"
          f" · 주인이 seen 에 없음 {counts[NO_ORIGIN]}건"
          f" · 사본이 원본과 같음 {counts[FRESH_COPY]}건)")
    print(f"\n  그중 **그 판에서 실제로 재출품 판정이 난** 자리   {len(exercised):>4,}건"
          f"   <- 고침이 실제로 든 자리")
    print(f"    그중 그 판의 자리가 하나뿐이라 **못 박히는** 것        {len(nailed):>4,}건")
    print(f"    (꼬리표가 '없어짐 0'이라 물려받은 자리가 아예 없던 판 "
          f"{len(stale) - len(exercised) - len(untagged)}건"
          f" · 꼬리표가 없어 못 가르는 판 {len(untagged)}건)")

    print()
    if not decided:
        print("  ⛔ 아직 못 잽니다. **분모가 0건**입니다 — 사본이 원본과 달랐고 그때 사본이")
        print("     쓰였는지 가려지는 자리가 이 구간에 없었습니다. 여기서 '어긋남 0건'은")
        print("     '깨끗해서'가 아니라 **'표본이 없어서'**입니다. 숫자를 결론에 쓰지 마세요.")
    else:
        print(f"  낡은 사본이 실제로 쓰인 자리: {len(wrong)}건 / {len(decided)}건")
        print("  ⛔ PR #37이 듣지 않는 자리가 있습니다." if wrong
              else "  ✅ 가려진 자리에서는 전부 낡은 사본을 쓰지 않았습니다.")
        if not exercised:
            print("  ⛔ 다만 **고침이 실제로 든 자리가 0건**입니다. 위 숫자는 '고침이 드는지'가")
            print("     아니라 '안 드는 자리에서 아무 일도 안 났는지'의 답입니다. 결론에 쓰지 마세요.")
        elif len(exercised) < 5:
            print(f"  ※ 고침이 실제로 든 자리가 {len(exercised)}건뿐입니다. **비율은 못 냅니다** —")
            print("     그 자리 하나하나를 아래에서 직접 읽으세요.")
    if exercised:
        print("\n  [고침이 실제로 든 자리 — 그 판에 재출품 판정이 났고 사본이 낡아 있었습니다]")
        for site in exercised:
            print("    " + describe(site))
    if stale and (list_sites or wrong or not decided):
        print("\n  [사본이 원본과 달랐던 자리 전부]")
        for site in stale:
            print("    " + describe(site))
    return 1 if wrong else 0


def _pair(copy, origin, new_alert, new_seen, *, fp="seller:851163117:t",
          owner_in_seen=True, owner_changes=True, new_seen_before=False, drops=(),
          subject="") -> tuple:
    """자체 점검용 두 판. 숫자는 **숫자마다** 자릿수를 섞습니다(0 · 두 자리 · … · 일곱 자리)."""
    previous = {
        "seen": {},
        "relist_fingerprints": {fp: {"item_id": "m-old", "last_alert_price": copy,
                                     "last_seen_price": copy}},
    }
    if owner_in_seen:
        previous["seen"]["m-old"] = {"last_alert_price": origin, "last_seen_price": origin}
    if new_seen_before:
        previous["seen"]["m-new"] = {"last_alert_price": 1250000, "last_seen_price": 1250000}
    current = {
        "seen": dict(previous["seen"]),
        "relist_fingerprints": {fp: {"item_id": "m-new" if owner_changes else "m-old",
                                     "last_alert_price": new_alert,
                                     "last_seen_price": new_seen}},
        "pending": [{"alert_id": f"drop:m-new:{base}:{target}"} for base, target in drops],
    }
    current["seen"]["m-new"] = {"last_alert_price": new_alert, "last_seen_price": new_seen}
    return previous, current, subject


# (이름, 두 판, 기대 판정, 기대 방향)
# 숫자를 **칸마다가 아니라 숫자마다** 섞었습니다 — 0 · 12 · 999 · 1,000 · 1,001 ·
# 47,600 · 79,200 · 215,000 · 1,250,000, 그리고 문턱 경계(정확히 1,000 내려간 인하).
# 자릿수를 잃는 고장은 **두 값이 그 고장 아래에서 같은 값으로 뭉개져야** 잡힙니다.
SELF_TEST_CASES = (
    ("사본을 물려받음(비쌈) — 고치기 전 실제로 난 모양",
     _pair(215000, 200000, 215000, 220000), FROM_COPY, "비쌈(지문>매물)"),
    ("사본을 물려받음(쌈) — 인하 알림이 삼켜지는 쪽",
     _pair(40700, 82600, 40700, 82600), FROM_COPY, "쌈(지문<매물)"),
    ("원본을 물려받음 — PR #37이 드는 모양",
     _pair(215000, 200000, 200000, 220000), NOT_COPY, "비쌈(지문>매물)"),
    ("기준가 0원도 셉니다",
     _pair(0, 1000, 0, 12), FROM_COPY, "쌈(지문<매물)"),
    ("문턱 근처 두 값(999 / 1,001)이 뭉개지면 안 됩니다",
     _pair(1001, 999, 1001, 12), FROM_COPY, "비쌈(지문>매물)"),
    ("일곱 자리와 여섯 자리(1,250,000 / 250,000)가 뭉개지면 안 됩니다",
     _pair(1250000, 250000, 1250000, 1300000), FROM_COPY, "비쌈(지문>매물)"),
    ("설명이 안 되는 자리 — ✅로 세면 안 됩니다",
     _pair(215000, 200000, 180000, 190000), UNEXPLAINED, "비쌈(지문>매물)"),
    ("사본이 원본과 같음 — 고침이 할 일이 없는 자리",
     _pair(1000, 1000, 1000, 1001), FRESH_COPY, ""),
    ("주인이 seen 에 없음 — 고침이 닿지 않는 자리",
     _pair(999, 0, 999, 12, owner_in_seen=False), NO_ORIGIN, ""),
    ("판정이 '별개의 매물'로 끝난 자리 — 사본은 안 썼습니다",
     _pair(22600, 34100, 106400, 106400), NOT_COPY, "쌈(지문<매물)"),
    ("인하 알림이 기준가를 들고 있습니다(원본) — 문턱 경계 정확히 1,000",
     _pair(48600, 79200, 78200, 78200, drops=((79200, 78200),)), NOT_COPY, "쌈(지문<매물)"),
    ("인하 알림이 기준가를 들고 있습니다(사본)",
     _pair(48600, 79200, 47600, 47600, drops=((48600, 47600),)), FROM_COPY, "쌈(지문<매물)"),
    ("나가면 안 될 알림이 나갔으면 '안 물려받음'은 안 섭니다",
     _pair(56000, 79200, 47600, 47600, drops=((56000, 47600),)), FROM_COPY, "쌈(지문<매물)"),
    # 사본이 마침 이번 관측가와 같으면 '사본을 물려받음'과 '안 물려받음'이 **같은
    # 기록**을 냅니다. title: 지문은 키에 가격이 박혀 있어 이 모양이 흔합니다.
    ("가를 수 없는 자리 — 두 가설이 같은 기록을 냅니다",
     _pair(5000, 8000, 5000, 5000), UNDECIDED, "쌈(지문<매물)"),
    ("꼬리표가 '없음0'이면 그 판에는 물려받은 자리가 없습니다",
     _pair(5000, 8000, 5000, 5000,
           subject="queue Mercari alerts (재출품 3: 있음3/없음0/못물어봄0)"),
     NOT_COPY, "쌈(지문<매물)"),
    # 상태 파일이 이미 '사본을 물려받음'이라고 가린 자리를, 꼬리표가 덮으면 안 됩니다.
    # 둘이 어긋나는 것 자체가 신호라 조용히 ✅로 만들면 그 신호가 사라집니다.
    ("꼬리표가 '없음0'이어도 이미 가려진 자리는 덮지 않습니다",
     _pair(215000, 200000, 215000, 220000,
           subject="queue Mercari alerts (재출품 2: 있음2/없음0/못물어봄0)"),
     FROM_COPY, "비쌈(지문>매물)"),
    ("꼬리표가 '없음12'면 못 가릅니다 — `\\d` 로 줄이면 '없음1'로 읽혀 여기서 갈라집니다",
     _pair(5000, 8000, 5000, 5000,
           subject="queue Mercari alerts (재출품 40/55: 있음28/없음12/못물어봄0)"),
     UNDECIDED, "쌈(지문<매물)"),
    ("title: 지문도 셉니다",
     _pair(215000, 200000, 215000, 220000, fp="title:t:215000"), FROM_COPY, "비쌈(지문>매물)"),
)

# 자리로 세면 안 되는 판들.
SELF_TEST_NON_SITES = (
    ("주인이 안 바뀌면 자리가 아닙니다", _pair(215000, 200000, 215000, 220000,
                                               owner_changes=False)),
    ("B가 이미 seen 에 있었으면 자리가 아닙니다", _pair(215000, 200000, 215000, 220000,
                                                       new_seen_before=True)),
)


def _one(board: tuple) -> dict | None:
    previous, current, subject = board
    found = sites(previous, current, 1000.0, "", subject)
    return classify(found[0]) if len(found) == 1 else None


def self_test() -> bool:
    """세는 자가 정말 잡아내는지, 판을 일부러 어긋내 넣고 봅니다.

    "문제 없음"이라고 말하기 전에 그 확인 방법이 문제를 잡는지부터 봐야 합니다 —
    이 저장소에서 열한 라운드 연속으로 가장 값진 발견이 그 자리에서 나왔습니다.
    """
    ok = True
    print("[자체 점검] 판을 일부러 어긋내 넣고 세는 자가 잡는지 봅니다")

    base = _pair(215000, 200000, 215000, 220000)[0]
    if sites(base, json.loads(json.dumps(base))):
        print("  ⛔ 아무것도 안 바꿨는데 자리가 잡혔습니다 — 거짓 양성")
        ok = False
    else:
        print("  ✅ 안 바꾼 판에서는 자리가 잡히지 않습니다")

    for name, (previous, current, subject) in SELF_TEST_NON_SITES:
        found = sites(previous, current, 1000.0, "", subject)
        good = not found
        ok = ok and good
        print(f"  {'✅' if good else '⛔'} {name}: {len(found)}건")

    for name, board, expected, expected_dir in SELF_TEST_CASES:
        site = _one(board)
        if site is None:
            print(f"  ⛔ {name}: 자리가 1건으로 안 잡혔습니다")
            ok = False
            continue
        got, got_dir = verdict(site), direction(site)
        good = got == expected and got_dir == expected_dir
        ok = ok and good
        print(f"  {'✅' if good else '⛔'} {name}: {got}"
              + (f" / {got_dir}" if got_dir else "")
              + ("" if good else f"  (기대 {expected}"
                                 + (f" / {expected_dir}" if expected_dir else "") + ")"))

    # 못 박히는가 — `없어짐` 건수를 **한 자리로** 읽으면 '없어짐 12'가 '1'로 읽혀
    # 엉뚱하게 못 박힙니다. 그래서 두 자리 판을 눈금에 둡니다.
    for name, board, expected_pin in (
        ("없어짐 1 · 자리 1 -> 못 박힘",
         _pair(240000, 220000, 220000, 220000,
               subject="queue Mercari alerts (재출품 1: 있음0/없음1/못물어봄0)"), True),
        ("없어짐 12 · 자리 1 -> 못 박히지 않음",
         _pair(240000, 220000, 220000, 220000,
               subject="queue Mercari alerts (재출품 40/55: 있음28/없음12/못물어봄0)"), False),
        ("꼬리표 없음 -> 못 박히지 않음",
         _pair(240000, 220000, 220000, 220000), False),
    ):
        site = _one(board)
        good = site is not None and pinned(site) is expected_pin
        ok = ok and good
        print(f"  {'✅' if good else '⛔'} {name}: {pinned(site) if site else '자리 없음'}")

    # 층을 가르는 자가 실제로 가르는지. **세 층이 서로 다른 값**이어야 층을 뭉개는
    # 고장이 잡힙니다(눈금 판이 모자라면 눈금이 못 지킵니다).
    everything = [_one(board) for _n, board, _v, _d in SELF_TEST_CASES]
    everything = [site for site in everything if site]
    layers = (len(everything), len(stale_sites(everything)),
              len([s for s in stale_sites(everything) if verdict(s) in DECIDED]))
    good = layers == (18, 16, 14) and len(set(layers)) == 3
    ok = ok and good
    print(f"  {'✅' if good else '⛔'} 층이 갈립니다: 1층 {layers[0]} / 2층 {layers[1]} "
          f"/ 3층 {layers[2]}건 (기대 18 / 16 / 14)")
    return ok


def calibrate() -> int:
    ok = self_test()
    print()
    found, _pairs, in_window = audit(to_epoch(CALIBRATION["since"]),
                                     to_epoch(CALIBRATION["until"]), quiet=True)
    stale = stale_sites(found)
    expensive = [s for s in stale if direction(s).startswith("비쌈")]
    checks = [
        ("구간 안 판 수", in_window, CALIBRATION["versions"]),
        ("1층 물려받을 수 있었던 자리", len(found), CALIBRATION["sites"]),
        ("2층 사본이 비싼 쪽으로 낡은 자리", len(expensive), CALIBRATION["stale_expensive"]),
    ]
    for label, actual, expected in checks:
        match = actual == expected
        ok = ok and match
        print(f"[눈금] {label}: {actual:,} / 기대 {expected:,}"
              f" -> {'일치' if match else '어긋남 — 세는 법이 달라졌습니다'}")

    # 건수만 맞추면 다른 것을 세고도 통과합니다. 그 한 건의 **값**까지 봅니다.
    values = ((expensive[0]["copy"], expensive[0]["origin"]) if len(expensive) == 1
              else (None, None))
    match = values == (CALIBRATION["copy"], CALIBRATION["origin"])
    ok = ok and match
    expect = f"{CALIBRATION['copy']} / {CALIBRATION['origin']}"
    print(f"[눈금] 그 한 건의 값: 사본 {values[0]} / 원본 {values[1]} -> "
          + ("일치" if match else f"어긋남 — 기대 {expect}"))
    if not ok:
        print("\n⛔ 눈금 실패. 이 도구가 낸 숫자를 읽지 마세요.")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", default=FIX_MERGED_AT, help="UTC, 예: 2026-09-15T14:26")
    parser.add_argument("--until", default="2099-01-01T00:00", help="UTC")
    parser.add_argument("--list", action="store_true", dest="list_sites",
                        help="사본이 원본과 달랐던 자리를 하나씩 찍습니다")
    parser.add_argument("--calibrate", action="store_true",
                        help="아는 답과 자체 점검을 확인하고 어긋나면 1로 끝납니다")
    args = parser.parse_args()

    if args.calibrate:
        return calibrate()
    found, pairs, in_window = audit(to_epoch(args.since), to_epoch(args.until))
    if not in_window:
        print("[중단] 구간에 커밋이 없습니다. 얕은 클론이면 "
              "`git fetch --unshallow origin main`을 먼저 하세요.", file=sys.stderr)
        return 2
    return report(found, pairs, in_window, args.since, args.until, args.list_sites)


if __name__ == "__main__":
    raise SystemExit(main())
