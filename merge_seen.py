"""GitHub push 충돌 때 seen_items.json의 상태를 안전하게 병합합니다.

- seen: 두 실행이 확인한 매물/가격을 모두 유지합니다.
- sent_alerts: 합집합으로 보존해 이미 전송된 알림이 대기열에 되살아나도 재전송하지 않습니다.
- pending: 고유 alert_id(구버전은 caption) 기준으로 합치되 sent_alerts에 있는 항목은 제거합니다.
"""

import json


def read_json(path: str) -> dict:
    try:
        with open(path) as file:
            data = json.load(file)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def alert_key(entry: dict) -> str:
    value = entry.get("alert_id")
    if value:
        return str(value)
    return f"legacy:{entry.get('caption', '')}"


def unique_recent(values: list, limit: int) -> list[str]:
    result = []
    known = set()
    for value in values:
        value = str(value)
        if value and value not in known:
            result.append(value)
            known.add(value)
    return result[-limit:]


def merge_pending(theirs: list, mine: list, sent_alerts: list) -> list:
    sent_keys = set(sent_alerts)
    pending_keys = set()
    result = []

    for entry in theirs + mine:
        if not isinstance(entry, dict):
            continue
        key = alert_key(entry)
        if key in sent_keys or key in pending_keys:
            continue
        normalized = dict(entry)
        normalized.setdefault("alert_id", key)
        result.append(normalized)
        pending_keys.add(key)

    return result[-500:]


mine = read_json("/tmp/mine.json")
theirs = read_json("/tmp/theirs.json")

merged_seen = {**theirs.get("seen", {}), **mine.get("seen", {})}
sent_alerts = unique_recent(theirs.get("sent_alerts", []) + mine.get("sent_alerts", []), 5000)
merged_pending = merge_pending(theirs.get("pending", []), mine.get("pending", []), sent_alerts)

with open("/tmp/merged.json", "w") as file:
    json.dump(
        {
            "seen": dict(list(merged_seen.items())[-5000:]),
            "pending": merged_pending,
            "sent_alerts": sent_alerts,
        },
        file,
        ensure_ascii=False,
    )
