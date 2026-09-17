#!/usr/bin/env python3
"""평소엔 안 도는 경로들이 실제로 돌았는지, 돌 **자리**가 몇 번이었는지 셉니다.

왜 이 파일이 있는가
-------------------
고장 감지 장치 대부분은 평소에 아무 일도 하지 않습니다. 그래서 "이상 없음"과
"아직 한 번도 불린 적 없음"이 **같은 0으로 보입니다.** 2026-09-17 점검에서 이것이
그대로 나왔습니다 - empty_feed_alerts 는 배포 뒤 5,900판 동안 조건이 **한 번도**
서지 않았습니다. 그 0은 '잘 돈다'가 아니라 '아직 못 잼'입니다.

그래서 이 도구는 항상 **두 숫자를 같이** 찍습니다.

    자리(분모)  : 그 경로가 판단을 내릴 조건이 몇 번 섰나
    울림(분자)  : 실제로 알림이 몇 건 나갔나

분모를 안 세면 "고쳤는데 0건"과 "고장 난 채로 0건"이 구별되지 않습니다.

무엇을 읽는가
-------------
상태 파일(`seen_items.json`)의 커밋 이력 하나뿐입니다. 봇은 성공할 때마다 상태를
main 에 커밋하므로, 이력이 곧 '봇이 매 분 무엇을 보고 무엇을 판단했는지'입니다.
Actions 로그는 개발 환경에서 한 건씩만 꺼낼 수 있어 하루치를 셀 수 없습니다
(CLAUDE.md "확인 방법을 '실행 로그에서 한 줄 세기'로 잡기").

측정할 수 없는 구간을 0으로 읽지 않습니다
-----------------------------------------
`keyword_checked_at` 은 2026-09-12 02:31 이전 상태 파일에 **아예 없습니다.**
그 구간의 '자리 0건'은 0이 아니라 **측정이 없음**이고, 도구가 둘을 갈라 찍습니다.
이 갈라 찍기가 왜 필요한지는 만들면서 직접 겪었습니다 - 첫 판은 끝 경계 키
(`pending_relists`)가 없던 옛 파일에서 슬랩이 파일 끝까지 가 파싱에 실패했고,
그 실패가 조용히 '값 없음'으로 떨어져 **12,947판이 통째로 빠진 채** 09-14 이후만
본 표를 냈습니다. 눈금(`--calibrate`)의 '읽어낸 판 수'가 그것을 잡습니다.

쓰는 법
-------
    python scripts/live_paths_audit.py --calibrate          # 먼저. 어긋나면 1로 끝납니다
    python scripts/live_paths_audit.py origin/main --since '2026-09-15 00:00'

얕은 클론이면 `git fetch --unshallow origin main` 을 먼저 하세요.
"""
import argparse
import json
import re
import statistics
import subprocess
import sys
from datetime import datetime, timezone

# 상태 파일의 최상위 키 - 이 순서로 저장됩니다(save_state 참고).
TOP_KEYS = ["seen", "pending", "sent_alerts", "relist_fingerprints",
            "known_keywords", "keyword_checked_at", "pending_relists"]

CLOCK_KEYS = {
    "search_ok": "__last_search_ok__",
    "created_ok": "__last_created_ok__",
    "items_ok": "__last_items_ok__",
    "cadence_ok": "__last_cadence_ok__",
}

HEALTH = re.compile(rb"health:[^\"]+")

# check_mercari.py 와 같은 값이어야 합니다. 여기서 다시 적어 두는 이유는 이 도구가
# 옛 구간도 읽기 때문입니다 - 그때의 상수가 아니라 '지금 기준으로 얼마나 가까운가'를
# 봅니다. 상수가 바뀌면 이 줄도 같이 고치세요.
MAX_SEEN_ITEMS = 30000
MAX_RELIST_FINGERPRINTS = 30000
MAX_SENT_ALERTS = 20000
HEALTH_ALERT_AFTER_SECONDS = 10 * 60
EXPECTED_RUN_INTERVAL_SECONDS = 60
RUN_INTERVAL_SLACK = 3
KEYWORD_STUCK_AFTER_SECONDS = 60 * 60
WATCHDOG_STUCK_AFTER_SECONDS = 15 * 60
WATCHDOG_STALE_AFTER_SECONDS = 30 * 60

# 복구 알림과 그 짝이 되는 경고. PR #22가 '알린 적 없는 고장의 복구'를 막았습니다.
RECOVERY_PAIRS = {
    "recovered": "search-down",
    "created-ok": "created-missing",
    "feed-ok": "empty-feed",
    "cadence-ok": "cadence-slow",
}


def utc(epoch):
    return datetime.fromtimestamp(float(epoch), timezone.utc)


def stamp(epoch):
    return utc(epoch).strftime("%m-%d %H:%M:%S")


def parse_when(text):
    """'2026-09-15 00:00' 같은 문자열을 epoch 로. 손으로 계산하지 않습니다.

    CLAUDE.md "구간 epoch를 손으로 계산하기" - 하루를 어긋나게 잡아 176건이 29건으로
    나온 적이 있습니다.
    """
    if text is None:
        return None
    value = text.strip().replace("/", "-")
    if value.isdigit():
        return float(value)
    for suffix in ("", ":00", " +0000"):
        try:
            parsed = datetime.fromisoformat(value + suffix)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    raise SystemExit(f"[중단] 시각을 읽지 못했습니다: {text!r}")


# --------------------------------------------------------------------------
# 1. 상태 파일 한 판에서 필요한 값만 뽑습니다 (전체 JSON 파싱은 너무 느립니다)
# --------------------------------------------------------------------------

def slab(blob, key, following):
    """`"key":` 뒤부터 다음 최상위 키 앞까지를 잘라 돌려줍니다.

    끝 경계 키가 그 판에 **없을 수도** 있습니다(`pending_relists`는 2026-09-14
    03:14 에 생겼습니다). 그때 파일 끝까지 자르면 최상위 닫는 괄호가 딸려 와
    파싱이 실패하고, 그 실패가 조용히 '값 없음'이 됩니다. 그래서 꼬리 '}'를 뗍니다.
    """
    head = re.search(rb'"%s":\s*' % re.escape(key.encode()), blob)
    if head is None:
        return None
    start = head.end()
    for name in following:
        tail = re.search(rb',\s*"%s":\s*' % re.escape(name.encode()), blob)
        if tail is not None and tail.start() > start:
            return blob[start:tail.start()]
    rest = blob[start:].rstrip()
    return rest[:-1] if rest.endswith(b"}") else rest


def count_seen_entries(chunk):
    """`seen` 의 항목 수. 값의 모양이 **셋**이라 한 가지로 세면 안 됩니다.

    실측(2026-09-17 HEAD): dict 21,356 / 정수 1,200 / null 125 = 22,681.
    `"last_seen_price"` 만 세면 21,356 으로 **1,325건이 조용히 빠집니다.**
    항목마다 최상위 콜론이 dict 는 3개(키 + 안쪽 둘), 정수·null 은 1개이므로
    전체 콜론 수에서 dict 몫 2개씩을 빼면 항목 수가 나옵니다.
    """
    if chunk is None:
        return None
    dicts = chunk.count(b'"last_seen_price"')
    return chunk.count(b'": ') - 2 * dicts


def count_fingerprints(chunk):
    """`relist_fingerprints` 의 항목 수.

    값이 전부 dict(`item_id`·`last_alert_price`·`last_seen_price`)라 항목당
    콜론이 **4개**입니다. seen 의 공식을 그대로 쓰면 정확히 **두 배**가 나옵니다
    (실측으로 21,628 이 43,256 으로 나왔습니다). 세는 법은 칸마다 달라집니다.
    """
    if chunk is None:
        return None
    return chunk.count(b'"last_seen_price"')


def extract(blob):
    """한 판에서 필요한 값을 뽑습니다. 못 읽은 칸은 None 으로 두고 `notes`에 남깁니다."""
    notes = []
    start = {key: re.search(rb'"%s":\s*' % re.escape(key.encode()), blob) for key in TOP_KEYS}
    seen_chunk = slab(blob, "seen", ["pending", "sent_alerts"])
    pending_chunk = slab(blob, "pending", ["sent_alerts", "relist_fingerprints"])
    sent_chunk = slab(blob, "sent_alerts", ["relist_fingerprints", "known_keywords"])
    fp_chunk = slab(blob, "relist_fingerprints", ["known_keywords", "keyword_checked_at"])
    clock_chunk = slab(blob, "keyword_checked_at", ["pending_relists"])

    clocks = {}
    if clock_chunk is None:
        notes.append("시계없음")          # keyword_checked_at 자체가 없던 옛 판
    else:
        try:
            table = json.loads(clock_chunk.decode())
        except Exception:
            notes.append("시계파싱실패")   # 이것을 0으로 읽으면 안 됩니다
            table = {}
        for short, key in CLOCK_KEYS.items():
            value = table.get(key)
            clocks[short] = float(value) if isinstance(value, (int, float)) else None
        per_keyword = [v for k, v in table.items()
                       if not k.startswith("__") and isinstance(v, (int, float))]
        clocks["keywords"] = len(per_keyword)
        clocks["worst_keyword"] = min(per_keyword) if per_keyword else None

    return {
        "seen": count_seen_entries(seen_chunk),
        "fingerprints": count_fingerprints(fp_chunk),
        "sent": (sent_chunk.count(b'", "') + 1 if sent_chunk and sent_chunk.strip() != b"[]" else 0)
                if sent_chunk is not None else None,
        "pending": pending_chunk.count(b'"alert_id"') if pending_chunk is not None else None,
        "clocks": clocks,
        "health_sent": sorted({m.decode() for m in HEALTH.findall(sent_chunk)})
                       if sent_chunk is not None else None,
        "health_pending": sorted({m.decode() for m in HEALTH.findall(pending_chunk)})
                          if pending_chunk is not None else None,
        "notes": notes,
        "present": {k: v is not None for k, v in start.items()},
    }


# --------------------------------------------------------------------------
# 2. 이력을 흘려 읽습니다
# --------------------------------------------------------------------------

def iter_snapshots(rev, since=None, until=None, repo="."):
    """`--first-parent` 로 (시각, 제목, 뽑은 값)을 오래된 것부터 흘려 줍니다.

    `--first-parent` 가 아니면 PR 브랜치의 옛 상태 파일이 섞여 들어와 상태가 뒤로
    갔다 오는 것처럼 보입니다(README: 그것 때문에 '전송 기록 유실 930건'이라는
    허깨비가 잡혔습니다).
    """
    listing = subprocess.run(
        ["git", "-C", repo, "log", "--first-parent", "--format=%H %ct %s", rev],
        capture_output=True, text=True, check=True).stdout.splitlines()
    rows = [line.split(" ", 2) for line in listing]
    rows.reverse()
    rows = [r for r in rows if len(r) == 3]
    if since is not None:
        rows = [r for r in rows if float(r[1]) >= since]
    if until is not None:
        rows = [r for r in rows if float(r[1]) <= until]

    batch = subprocess.Popen(["git", "-C", repo, "cat-file", "--batch"],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        for sha, when, subject in rows:
            batch.stdin.write(f"{sha}:seen_items.json\n".encode())
            batch.stdin.flush()
            header = batch.stdout.readline().split()
            if len(header) < 3:
                yield float(when), subject, {"notes": ["파일없음"], "clocks": {},
                                             "seen": None, "fingerprints": None,
                                             "sent": None, "pending": None,
                                             "health_sent": None, "health_pending": None}
                continue
            blob = batch.stdout.read(int(header[2]))
            batch.stdout.read(1)
            yield float(when), subject, extract(blob)
    finally:
        batch.stdin.close()
        batch.wait()


# --------------------------------------------------------------------------
# 3. 세기
# --------------------------------------------------------------------------

def summarize(snapshots):
    """뽑은 값들을 받아 경로별 '자리'와 '울림'을 셉니다. git 을 타지 않습니다."""
    report = {
        "판": 0, "처음": None, "마지막": None,
        "시계를 읽은 판": 0, "시계가 없던 판": 0, "못 읽은 판": 0,
        "커밋 간격": [], "실행": [],
        "health": {}, "health 대기": set(),
        "seen 최대": None, "지문 최대": None, "전송기록 최대": None,
        "상한 초과": {"seen": 0, "지문": 0, "전송기록": 0},
        "매물0 자리": [], "등록시각 자리": [],
        # 경고 가지가 실제로 설 수 있었던 실행 수. **복구 가지는 세지 않습니다** -
        # 그 가지는 봇이 아예 안 돌았을 때도, 지연된 실행이 낡은 체크아웃을 읽었을
        # 때도 서기 때문에 상태 파일만으로는 분모를 못 셉니다(2026-09-17 실측:
        # recovered 5건 중 2건이 '다음 실행까지 56초·148초'인 자리에서 나왔습니다 -
        # 커밋 이력의 간격으로는 설명되지 않습니다).
        "경고 자리": {"empty-feed": 0, "created-missing": 0,
                    "cadence-slow": 0, "keyword-down": 0},
    }
    previous_when = None
    previous_run = None
    first_health = {}

    for when, _subject, values in snapshots:
        report["판"] += 1
        if report["처음"] is None:
            report["처음"] = when
        report["마지막"] = when

        if previous_when is not None:
            report["커밋 간격"].append((when, when - previous_when))
        previous_when = when

        notes = values.get("notes") or []
        if "시계없음" in notes:
            report["시계가 없던 판"] += 1
        if "시계파싱실패" in notes or "파일없음" in notes:
            report["못 읽은 판"] += 1

        for label, key in (("seen 최대", "seen"), ("지문 최대", "fingerprints"),
                           ("전송기록 최대", "sent")):
            value = values.get(key)
            if value is None:
                continue
            if report[label] is None or value > report[label]:
                report[label] = value
        for label, key, cap in (("seen", "seen", MAX_SEEN_ITEMS),
                                ("지문", "fingerprints", MAX_RELIST_FINGERPRINTS),
                                ("전송기록", "sent", MAX_SENT_ALERTS)):
            value = values.get(key)
            if value is not None and value > cap:
                report["상한 초과"][label] += 1

        for alert in values.get("health_sent") or []:
            if alert not in first_health:
                first_health[alert] = when
        for alert in values.get("health_pending") or []:
            report["health 대기"].add(alert)

        clocks = values.get("clocks") or {}
        search_ok = clocks.get("search_ok")
        if search_ok is None:
            continue
        report["시계를 읽은 판"] += 1
        if previous_run is not None and previous_run["search_ok"] == search_ok:
            continue   # 같은 실행이 여러 번 커밋합니다
        run = {"search_ok": search_ok, "clocks": clocks, "when": when}
        # 직전 실행의 시계는 **다시 덮기 전에** 붙들어 둡니다. 봇이 보는 last_ok 는
        # 이번 실행이 덮어쓰기 전의 값이라, 여기서 previous_run 을 먼저 갱신하면
        # 간격이 0으로 보여 경고 가지가 영영 안 서는 것처럼 세집니다.
        earlier = previous_run
        if earlier is not None:
            run["gap"] = search_ok - earlier["search_ok"]
        report["실행"].append(run)
        previous_run = run

        # 자리: 검색은 성공했는데 매물이 0건이면 __last_items_ok__ 가 전진하지 못합니다.
        items_ok = clocks.get("items_ok")
        if items_ok is not None and search_ok - items_ok > 0.5:
            report["매물0 자리"].append((search_ok, search_ok - items_ok))
            if search_ok - items_ok >= HEALTH_ALERT_AFTER_SECONDS:
                report["경고 자리"]["empty-feed"] += 1
        created_ok = clocks.get("created_ok")
        if created_ok is not None and search_ok - created_ok > 0.5:
            report["등록시각 자리"].append((search_ok, search_ok - created_ok))
            if search_ok - created_ok >= HEALTH_ALERT_AFTER_SECONDS:
                report["경고 자리"]["created-missing"] += 1
        # keyword_health_alerts 는 봇 전체가 오래 멈췄던 실행에서는 아무것도 내보내지
        # 않습니다(안 그러면 살아나는 순간 키워드 수만큼 쏟아집니다). 그 가드를 여기서도
        # 그대로 겁니다 - 안 걸면 7시간 정지의 복구 실행 하나가 '자리'로 잡힙니다.
        worst = clocks.get("worst_keyword")
        bot_was_down = earlier is not None and search_ok - earlier["search_ok"] >= KEYWORD_STUCK_AFTER_SECONDS
        if worst is not None and not bot_was_down and search_ok - worst >= KEYWORD_STUCK_AFTER_SECONDS:
            report["경고 자리"]["keyword-down"] += 1
        # cadence 는 봇이 쓰는 두 조건을 그대로 씁니다 - 간격이 벌어졌고(>기대x3),
        # 직전 정상 시각이 10분 이상 묵었을 때만 경고 가지에 닿습니다.
        previous_cadence = (earlier or {}).get("clocks", {}).get("cadence_ok")
        if ("gap" in run and previous_cadence is not None
                and search_ok - previous_cadence >= HEALTH_ALERT_AFTER_SECONDS
                and run["gap"] > EXPECTED_RUN_INTERVAL_SECONDS * RUN_INTERVAL_SLACK):
            report["경고 자리"]["cadence-slow"] += 1

    report["health"] = first_health
    report["health 대기"] = sorted(report["health 대기"] - set(first_health))
    return report


def families(first_health):
    grouped = {}
    for alert, when in first_health.items():
        grouped.setdefault(alert.split(":")[1], {})[alert] = when
    return grouped


def unpaired_recoveries(first_health):
    """짝이 되는 경고가 한 번도 안 나간 복구 알림을 찾습니다.

    복구 alert_id 는 `health:<가족>:<기준시각>`, 경고는 `health:<짝>:<기준시각>:<구간>`
    으로 기준시각을 공유합니다(outage_was_announced 와 같은 기준).
    """
    grouped = families(first_health)
    found = {}
    for recovery, warning in RECOVERY_PAIRS.items():
        warned = grouped.get(warning, {})
        loose = []
        for alert, when in sorted(grouped.get(recovery, {}).items(), key=lambda kv: kv[1]):
            base = alert.split(":")[2]
            if not any(w.startswith(f"health:{warning}:{base}:") for w in warned):
                loose.append((alert, when))
        found[recovery] = loose
    return found


# --------------------------------------------------------------------------
# 4. 찍기
# --------------------------------------------------------------------------

def render(report, out=sys.stdout):
    say = lambda *a: print(*a, file=out)
    total = report["판"]
    say(f"판 {total:,}개  {stamp(report['처음'])} ~ {stamp(report['마지막'])}"
        if total else "판 0개 — 볼 것이 없습니다")
    if not total:
        return
    read = report["시계를 읽은 판"]
    say(f"시계(keyword_checked_at)를 읽어낸 판 {read:,} / {total:,}"
        f"  · 시계가 없던 옛 판 {report['시계가 없던 판']:,}"
        f"  · 못 읽은 판 {report['못 읽은 판']:,}")
    if read == 0:
        say("⛔ 시계를 한 판도 읽지 못했습니다 — 아래 '자리'는 0이 아니라 '측정이 없음'입니다")

    gaps = report["커밋 간격"]
    if gaps:
        values = [g for _, g in gaps]
        long_gaps = [(w, g) for w, g in gaps if g >= WATCHDOG_STUCK_AFTER_SECONDS]
        stale_gaps = [(w, g) for w, g in gaps if g >= WATCHDOG_STALE_AFTER_SECONDS]
        say("")
        say("[상태 커밋 간격]  = 워치독이 볼 자리")
        say(f"  중앙값 {statistics.median(values):.0f}초 · p90 "
            f"{sorted(values)[int(len(values) * 0.9)]:.0f}초 · 최대 {max(values):.0f}초")
        say(f"  15분 이상(끊을 자리) {len(long_gaps)}건 · 30분 이상(이슈를 열 자리) {len(stale_gaps)}건")
        for when, gap in sorted(long_gaps, key=lambda x: -x[1])[:5]:
            say(f"    {stamp(when - gap)} -> {stamp(when)}  {gap / 60:.1f}분")

    runs = report["실행"]
    say("")
    say(f"[실행] 고유 실행 {len(runs):,}개"
        + (f"  {stamp(runs[0]['search_ok'])} ~ {stamp(runs[-1]['search_ok'])}" if runs else ""))

    say("")
    say("[경고 가지가 설 자리(분모)와 실제로 나간 알림(분자)]")
    say("  ※ 복구(✅) 가지의 분모는 **세지 않습니다** — 그 가지는 봇이 아예 안 돌았을 때도,")
    say("    지연된 실행이 낡은 체크아웃을 읽었을 때도 서기 때문에 상태 파일만으로는 못 셉니다.")
    grouped = families(report["health"])
    rang = lambda name: len(grouped.get(name, {}))
    places = report["경고 자리"]

    def line(label, place, warn_name, recover_name, blurb=""):
        warned, recovered = rang(warn_name), rang(recover_name)
        if place is None:
            shown = "⛔측정없음"
        else:
            shown = f"{place:,}회"
        mark = ("   ← 자리 0 = 이 경로는 아직 한 번도 불린 적이 없습니다"
                if place == 0 and warned == 0 else "")
        say(f"  {label:24s} 경고 자리 {shown:>9s}  ⚠️{warned}건 / ✅{recovered}건{mark}")
        if blurb:
            say(f"    {blurb}")

    empty = None if read == 0 else places["empty-feed"]
    worst = max((d for _, d in report["매물0 자리"]), default=0)
    line("empty_feed_alerts", empty, "empty-feed", "feed-ok",
         f"검색은 됐는데 매물 0건이던 실행 {len(report['매물0 자리'])}개 "
         f"(가장 길었던 것 {worst:.0f}초 / 임계 {HEALTH_ALERT_AFTER_SECONDS}초)")
    line("created_coverage_alerts", None if read == 0 else places["created-missing"],
         "created-missing", "created-ok")
    line("keyword_health_alerts", None if read == 0 else places["keyword-down"],
         "keyword-down", "keyword-up")

    gaps_between = [r["gap"] for r in runs if "gap" in r]
    line("cadence_alerts", None if read == 0 else places["cadence-slow"],
         "cadence-slow", "cadence-ok",
         (f"실행 간격 중앙값 {statistics.median(gaps_between):.0f}초 · "
          f"최대 {max(gaps_between):.0f}초 · 180초 초과 "
          f"{sum(1 for g in gaps_between if g > 180)}회") if gaps_between else "")

    over = report["상한 초과"]
    say(f"  {'state_capacity_alerts':24s} 경고 자리 {over['seen'] + over['지문'] + over['전송기록']:,}회"
        f"       ⚠️{rang('state-cap')}건"
        + ("   ← 자리 0 = 아직 한 번도 불린 적이 없습니다"
           if not (over["seen"] + over["지문"] + over["전송기록"]) and not rang("state-cap") else ""))
    if report["seen 최대"] is not None:
        say(f"    seen 최대 {report['seen 최대']:,}/{MAX_SEEN_ITEMS:,} · "
            f"지문 {report['지문 최대']:,}/{MAX_RELIST_FINGERPRINTS:,} · "
            f"전송기록 {report['전송기록 최대']:,}/{MAX_SENT_ALERTS:,}")
    say(f"  {'health_alerts(전량 실패)':24s} 경고 자리 ⛔측정없음  ⚠️{rang('search-down')}건 "
        f"/ ✅{rang('recovered')}건")
    say("    전량 실패한 실행은 상태가 바뀌지 않아 커밋도 남기지 않습니다 — 분모가 이력에 없습니다.")

    say("")
    say("[이 창의 상태 파일에 남아 있는 health 알림]")
    say("  ※ '처음 보인 판'은 **이 창 안에서** 처음 보인 때입니다 — 알림이 나간 때가 아닙니다.")
    if not grouped:
        say("  없음")
    for name in sorted(grouped):
        first = min(grouped[name].values())
        say(f"  {name:16s} {len(grouped[name]):3d}건  (이 창에서 처음 보인 판 {stamp(first)})")
    if report["health 대기"]:
        say(f"  대기열에 올랐다가 끝내 못 나간 것 {len(report['health 대기'])}건: "
            f"{report['health 대기'][:5]}")

    say("")
    say("[경고 없이 나간 복구 알림]  = 알린 적 없는 고장이 나았다는 말")
    for recovery, loose in unpaired_recoveries(report["health"]).items():
        total_of = rang(recovery)
        note = "  (cadence 가족은 일부러 가드를 걸지 않았습니다)" if recovery == "cadence-ok" else ""
        say(f"  {recovery:12s} {total_of:3d}건 중 짝 없음 {len(loose):3d}건{note}")
        for alert, when in loose:
            say(f"      {alert}  (이 창에서 처음 보인 판 {stamp(when)})")


# --------------------------------------------------------------------------
# 5. 눈금
# --------------------------------------------------------------------------
#
# 아는 답에 먼저 대 봅니다. 값이 **함께** 맞아야 통과입니다 - 하나만 보면 창을 조금만
# 옮겨도 맞는 숫자가 나옵니다(PR #43에서 3분 차이로 257이 289가 됐습니다).
#
# 자릿수는 칸마다가 아니라 **숫자마다** 섞습니다. 그리고 크기만 섞는 것으로는
# 부족합니다 - 그 고장 아래에서 **두 값이 같은 값으로 뭉개지는 판**이 있어야 잡힙니다.
# 여기서는 seen 22,681 과 지문 21,628 이 그 자리입니다(항목 수를 dict 만 세면 seen 이
# 21,356 으로 내려앉고, seen 공식을 지문에 그대로 쓰면 지문이 43,256 으로 두 배가
# 됩니다 - 둘 다 이 도구를 만들면서 실제로 낸 고장입니다).

# 눈금 창 둘. 하나로는 부족합니다 - 창을 옮기면 맞는 숫자가 나오고(PR #43: 3분 차이로
# 257이 289), 한 창 안에서는 두 문턱이 같은 값으로 뭉개집니다(아래 창 A 에서는 15분 이상도
# 30분 이상도 똑같이 1건입니다 - 문턱을 뒤바꾸는 고장이 그대로 통과합니다).
#
# 창 A: 시계가 들어온 뒤. 경로별 자리와 알림을 봅니다.
# 창 B: 시계가 **아직 없던** 구간. '못 잰 것을 0으로 읽지 않는가'와 두 문턱이 갈리는가를
#       함께 봅니다(15분 이상 11건 / 30분 이상 1건 - 여기서 갈립니다).
WINDOW_A = ("2026-09-12 02:30:00", "2026-09-15 00:00:00")
WINDOW_B = ("2026-09-08 00:00:00", "2026-09-09 00:00:00")

EXPECTED_A = {
    "판": 7690,
    "실행": 3498,
    "시계를 읽은 판": 7690,
    "15분 이상 간격": 1,
    "30분 이상 간격": 1,
    "최대 간격(초)": 26427,
    "recovered": 5,
    "created-ok": 5,
    "feed-ok": 4,
    "cadence-slow": 6,
    "cadence-ok": 8,
    "search-down": 0,
    "created-missing": 0,
    "empty-feed": 0,
    "keyword-down": 0,
    "state-cap": 0,
    "매물0 자리": 0,
    "등록시각 자리": 0,
    "상한 초과": 0,
    # 경고 가지의 자리. cadence 만 실제로 섰습니다 - 나머지 셋이 전부 0이라
    # '자리 0'과 '울림 0'이 뭉개지는 자리가 여기입니다.
    "경고 자리 empty-feed": 0,
    "경고 자리 created-missing": 0,
    "경고 자리 cadence-slow": 11,
    "경고 자리 keyword-down": 0,
    # seen 과 지문은 슬랩은 같지만 **세는 법이 다릅니다.** 한쪽 공식을 다른 쪽에 쓰면
    # 지문이 정확히 두 배(28,272)가 되고, 반대로 dict 만 세면 seen 이 내려앉습니다.
    # 둘이 **같이** 맞아야 통과입니다 - 둘 다 이 도구를 만들며 실제로 낸 고장입니다.
    "seen 최대": 15695,
    "지문 최대": 14136,
    "전송기록 최대": 8885,
}

EXPECTED_B = {
    "판": 450,
    "시계를 읽은 판": 0,      # keyword_checked_at 이 아직 없던 구간입니다
    "15분 이상 간격": 11,     # 여기서 두 문턱이 갈립니다
    "30분 이상 간격": 1,
    "최대 간격(초)": 2260,
}

# 이 이력에서 recovered 와 created-ok 는 **id 집합이 완전히 같습니다**(늘 같은 실행에서
# 함께 나갔습니다). 그래서 이 둘의 이름표를 뒤바꾸는 고장은 위 눈금으로 못 잡습니다.
# 그 자리는 합성 판을 쓰는 tests/test_live_paths_audit.py 가 맡습니다.


def measure(rev, window, repo):
    since, until = (parse_when(t) for t in window)
    report = summarize(iter_snapshots(rev, since, until, repo))
    grouped = families(report["health"])
    gaps = [g for _, g in report["커밋 간격"]]
    got = {
        "판": report["판"],
        "실행": len(report["실행"]),
        "시계를 읽은 판": report["시계를 읽은 판"],
        "15분 이상 간격": sum(1 for g in gaps if g >= WATCHDOG_STUCK_AFTER_SECONDS),
        "30분 이상 간격": sum(1 for g in gaps if g >= WATCHDOG_STALE_AFTER_SECONDS),
        "최대 간격(초)": int(max(gaps)) if gaps else 0,
        "매물0 자리": len(report["매물0 자리"]),
        "등록시각 자리": len(report["등록시각 자리"]),
        "경고 자리 empty-feed": report["경고 자리"]["empty-feed"],
        "경고 자리 created-missing": report["경고 자리"]["created-missing"],
        "경고 자리 cadence-slow": report["경고 자리"]["cadence-slow"],
        "경고 자리 keyword-down": report["경고 자리"]["keyword-down"],
        "상한 초과": sum(report["상한 초과"].values()),
        "seen 최대": report["seen 최대"],
        "지문 최대": report["지문 최대"],
        "전송기록 최대": report["전송기록 최대"],
    }
    for name in ("recovered", "created-ok", "feed-ok", "cadence-slow", "cadence-ok",
                 "search-down", "created-missing", "empty-feed", "keyword-down", "state-cap"):
        got[name] = len(grouped.get(name, {}))
    return got


def calibrate(rev="origin/main", repo=".", out=sys.stdout):
    say = lambda *a: print(*a, file=out)
    bad = 0
    for label, window, expected in (("A", WINDOW_A, EXPECTED_A), ("B", WINDOW_B, EXPECTED_B)):
        say(f"[눈금 {label}] {window[0]} ~ {window[1]} UTC")
        got = measure(rev, window, repo)
        if got["판"] == 0:
            say("  ⛔ 그 창에 판이 없습니다 — 얕은 클론이면 git fetch --unshallow origin main")
            return 1
        for name, want in expected.items():
            value = got.get(name)
            ok = value == want
            bad += 0 if ok else 1
            shown = f"{value:,}" if isinstance(value, int) else str(value)
            say(f"  {'OK ' if ok else '⛔ '} {name:16s} {shown:>10s} / 기대 {want:>10,}")
    if bad:
        say(f"⛔ 눈금이 {bad}칸 어긋났습니다 — 표를 읽지 마세요")
        return 1
    say("눈금 통과 — 창 둘, 아는 답 %d개" % (len(EXPECTED_A) + len(EXPECTED_B)))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("rev", nargs="?", default="origin/main")
    parser.add_argument("--since")
    parser.add_argument("--until")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--calibrate", action="store_true",
                        help="아는 답에 먼저 대 봅니다. 어긋나면 1로 끝납니다")
    args = parser.parse_args(argv)

    if args.calibrate:
        return calibrate(args.rev, args.repo)

    report = summarize(iter_snapshots(args.rev, parse_when(args.since),
                                      parse_when(args.until), args.repo))
    render(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
