"""GitHub push가 충돌났을 때 seen_items.json의 상태를 안전하게 병합합니다.

- seen: 두 실행이 확인한 매물/가격을 모두 유지합니다(최근에 본 쪽을 뒤로 보내 오래된 것부터 잘라냅니다).
- sent_alerts: 합집합으로 보존해 이미 전송된 알림이 대기열에 되살아나도 재전송하지 않습니다.
- pending: 고유 alert_id(구버전은 caption) 기준으로 합치되 sent_alerts에 있는 항목은 제거합니다.
- relist_fingerprints: 재출품 감지용 지문 기록도 두 쪽 다 유지합니다(합집합, 최신 쪽 우선).
- known_keywords: 이미 한 번이라도 조회한 키워드 목록도 합집합으로 유지합니다.
- keyword_checked_at: 키워드별 마지막 조회 시각은 더 늦은 쪽을 남깁니다.
"""
import json
import sys

MAX_SEEN_ITEMS = 15000
MAX_RELIST_FINGERPRINTS = 6000
MAX_SENT_ALERTS = 20000
MAX_PENDING_ALERTS = 500
SUPPORTED_FINGERPRINT_PREFIXES = ("seller:", "title:")
# 숍스 상품은 sellerId가 0으로 내려옵니다. 그 시절 지문은 지금 조회되지 않습니다.
UNKNOWN_SELLER_IDS = {"", "0", "none", "null"}


def read_json(path: str, required: bool = False) -> dict:
    """상태 파일을 읽습니다. required면 읽지 못할 때 조용히 넘어가지 않습니다.

    /tmp/mine.json은 방금 이 실행이 모은 결과입니다. 읽지 못했을 때 빈 dict로
    대신하면, 원격 상태만 남은 파일을 '병합 결과'라며 그대로 push해서 이번 실행의
    알림과 가격 기록이 소리 없이 사라집니다. 그럴 바에는 병합을 실패시키는 편이
    낫습니다 — push_state.sh가 실패로 받아 재시도하거나 멈추고, 상태는 보존됩니다.
    (/tmp/theirs.json은 원격에 파일이 없을 때 정상적으로 비어 있을 수 있어 관대합니다.)
    """
    try:
        with open(path) as file:
            data = json.load(file)
    except Exception as exc:
        if required:
            print(f"[병합 중단] {path}를 읽지 못했습니다: {exc}", file=sys.stderr)
            raise SystemExit(1)
        return {}
    if isinstance(data, dict):
        return data
    if required:
        print(f"[병합 중단] {path}의 형식이 올바르지 않습니다(dict가 아님)", file=sys.stderr)
        raise SystemExit(1)
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
        # 본문이 없는 항목은 전송 단계에서 터지고, 터지면 대기열에 그대로 남아
        # 다음 실행도 같은 자리에서 터집니다. check_mercari.py와 같은 기준으로 걸러 냅니다.
        caption = entry.get("caption")
        if not isinstance(caption, str) or not caption.strip():
            continue
        key = alert_key(entry)
        if key in sent_keys or key in pending_keys:
            continue
        normalized = dict(entry)
        normalized.setdefault("alert_id", key)
        result.append(normalized)
        pending_keys.add(key)
    return result[-MAX_PENDING_ALERTS:]


def merge_ordered(theirs: dict, mine: dict, limit: int) -> dict:
    """두 쪽을 합치되, 양쪽에 다 있는 키는 '최근에 본 쪽'(mine)의 순서·값을 따릅니다.

    상태 파일은 뒤에서부터 limit개만 남기므로, 최근 확인한 항목이 뒤로 가야
    오래 올라와 있는 매물이 먼저 잘려 나가 '신규'로 오인되는 일이 없습니다.
    """
    merged = {key: value for key, value in theirs.items() if key not in mine}
    merged.update(mine)
    return dict(list(merged.items())[-limit:])


def merge_checked_at(theirs: dict, mine: dict) -> dict:
    merged = dict(theirs)
    for keyword, value in mine.items():
        current = merged.get(keyword)
        if not isinstance(current, (int, float)) or (
            isinstance(value, (int, float)) and value > current
        ):
            merged[keyword] = value
    return merged


def is_usable_fingerprint(key: str) -> bool:
    """check_mercari.py와 같은 기준. 현재 코드가 조회하지 않는 지문을 걸러냅니다.

    'seller:0:'처럼 판매자 ID를 모르던 시절의 지문은 지금 'title:' 형식으로 대체되어
    영영 조회되지 않으므로 함께 버립니다.
    """
    key = str(key)
    if not key.startswith(SUPPORTED_FINGERPRINT_PREFIXES):
        return False
    parts = key.split(":")
    if parts[0] == "seller" and len(parts) > 1 and parts[1].strip().lower() in UNKNOWN_SELLER_IDS:
        return False
    return True


def prune_fingerprints(fingerprints: dict) -> dict:
    """check_mercari.py의 prune_fingerprints와 같은 기준(값이 dict인 것만 남김)."""
    return {
        key: value
        for key, value in fingerprints.items()
        if is_usable_fingerprint(key) and isinstance(value, dict)
    }


def main() -> None:
    mine = read_json("/tmp/mine.json", required=True)
    theirs = read_json("/tmp/theirs.json")

    merged_seen = merge_ordered(theirs.get("seen", {}), mine.get("seen", {}), MAX_SEEN_ITEMS)
    sent_alerts = unique_recent(theirs.get("sent_alerts", []) + mine.get("sent_alerts", []), MAX_SENT_ALERTS)
    merged_pending = merge_pending(theirs.get("pending", []), mine.get("pending", []), sent_alerts)
    merged_fingerprints = merge_ordered(
        prune_fingerprints(theirs.get("relist_fingerprints", {})),
        prune_fingerprints(mine.get("relist_fingerprints", {})),
        MAX_RELIST_FINGERPRINTS,
    )
    merged_known_keywords = sorted(set(theirs.get("known_keywords", [])) | set(mine.get("known_keywords", [])))
    merged_checked_at = merge_checked_at(
        theirs.get("keyword_checked_at", {}) or {}, mine.get("keyword_checked_at", {}) or {}
    )

    with open("/tmp/merged.json", "w") as file:
        json.dump(
            {
                "seen": merged_seen,
                "pending": merged_pending,
                "sent_alerts": sent_alerts,
                "relist_fingerprints": merged_fingerprints,
                "known_keywords": merged_known_keywords,
                "keyword_checked_at": merged_checked_at,
            },
            file,
            ensure_ascii=False,
        )


if __name__ == "__main__":
    main()
