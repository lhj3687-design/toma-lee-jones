"""
git push가 충돌났을 때, 로컬에서 방금 계산한 seen_items(/tmp/mine.json)와
원격 저장소의 최신 seen_items(/tmp/theirs.json)를 합쳐서 /tmp/merged.json에 씁니다.
- seen(매물ID->가격): 두 쪽 다 유지 (합집합)
- pending(대기 중인 알림): 두 쪽 다 유지하되, 같은 caption은 중복 제거
"""
import json

with open("/tmp/mine.json") as f:
    mine = json.load(f)

try:
    with open("/tmp/theirs.json") as f:
        theirs = json.load(f)
except Exception:
    theirs = {}

merged_seen = {**theirs.get("seen", {}), **mine.get("seen", {})}
merged_pending = theirs.get("pending", []) + mine.get("pending", [])

captions = set()
dedup_pending = []
for entry in merged_pending:
    caption = entry.get("caption")
    if caption not in captions:
        captions.add(caption)
        dedup_pending.append(entry)

with open("/tmp/merged.json", "w") as f:
    json.dump({"seen": merged_seen, "pending": dedup_pending}, f, ensure_ascii=False)
