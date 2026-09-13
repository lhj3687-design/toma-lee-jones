"""
메루카리(mercari.jp) 키워드 알림 봇

GitHub Actions는 아래 순서로 이 파일을 두 번 실행합니다.
1. collect: 새 매물/가격 인하를 찾아 상태와 대기열을 먼저 저장합니다.
2. send: 이미 원격 저장소에 저장된 대기열만 텔레그램으로 전송합니다.

이 순서 덕분에 텔레그램 전송이 오래 걸리거나 실행이 중간에 겹쳐도,
같은 매물이나 같은 가격 인하가 다시 새 알림으로 등록되는 일을 막습니다.

'신규' 판정은 상태 파일(seen)뿐 아니라 매물의 실제 등록 시각(created)을 함께 봅니다.
상태 파일은 용량 제한 때문에 오래된 항목을 버릴 수밖에 없는데, 등록 시각을 같이 보면
버려진 오래된 매물이 다시 '신규'로 둔갑해 알림이 가는 일을 원천적으로 막을 수 있습니다.
"""

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import unicodedata
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import httpx
from mercapi import Mercapi

# 일반 메루카리 개인 매물 ID는 항상 "m" + 숫자 형식입니다 (예: m90925725213).
# 이 형식이 아니면 메루카리 숍스(기업 판매자) 상품이므로 /shops/product/ 링크를 써야 합니다.
# item_type 필드만으로는 라이브러리가 숍스 여부를 늘 정확히 채워주지 않아 ID 형식을 보조 판단 기준으로 씁니다.
MERCARI_ITEM_ID_PATTERN = re.compile(r"^m\d+$")

# 제목 정규화 시 제거할 기호(전각/반각 공백, 대괄호·괄호·특수문자 등 꾸밈 차이 무시용)
_TITLE_NOISE_PATTERN = re.compile(r"[\s　!-/:-@\[-`{-~、。・【】「」『』［］（）]+")

# 메루카리 검색 API는 숍스(기업 판매자) 상품의 sellerId를 0으로 돌려줍니다.
# 이 값을 진짜 판매자 ID로 쓰면 서로 다른 숍스 상품들이 "같은 판매자"로 묶여서
# 제목만 같으면 재출품으로 오인되고, 진짜 새 매물 알림이 조용히 삼켜집니다.
UNKNOWN_SELLER_IDS = {"", "0", "none", "null"}

SEEN_FILE = Path("seen_items.json")
MAX_ITEMS_PER_KEYWORD = 120  # 메루카리 검색 한 페이지 크기(키워드당 정렬 방식마다 확인할 매물 개수)
MAX_SEARCH_PAGES = 3  # 새 매물이 한 페이지를 가득 채웠을 때만 추가로 볼 최대 페이지 수
MAX_SEEN_ITEMS = 15000
# 재출품 지문. seen과 거의 같은 속도로 쌓이는데(실측 둘 다 시간당 130~140건) 상한만
# seen의 40%였습니다. 올라와 있는 매물의 지문은 매 조회마다 갱신돼 살아남으므로,
# 밀려나는 건 사라진 매물의 지문 — 재출품 판정이 필요한 바로 그 항목입니다.
# 그래서 6000에서는 재출품 기억이 약 하루, seen은 약 나흘로 벌어집니다.
# 올리는 비용은 측정해 보니 거의 없습니다: 커밋당 델타는 파일 크기가 아니라 매 실행
# 재정렬되는 항목 수가 결정하고, 늘어난 꼬리는 다시 조회되지 않아 델타에 기여하지
# 않습니다(같은 조건 실측: 11.1 -> 9.9KB/커밋, 압축 기준 일회성 +0.35MB).
MAX_RELIST_FINGERPRINTS = 15000
PRICE_DROP_ALERT_THRESHOLD = 1000  # 마지막 알림 가격보다 이 금액(엔) 이상 떨어졌을 때만 알립니다.
MAX_PENDING_ALERTS = 500
# 이미 보낸 알림 목록. 같은 알림이 두 번 나가는 걸 막는 마지막 방어선입니다.
# 실측 전송량(시간당 약 52건) 기준으로 약 2.6일치였는데, 상한에 닿으면 오래된 기록부터
# 잘려 나가면서 그만큼 방어선이 얇아집니다. 한 건이 24바이트 남짓이라 올리는 비용이
# 거의 없어 약 6.5일치로 늘렸습니다.
MAX_SENT_ALERTS = 20000

# keyword_checked_at 안에 같이 보관하는 예약 키들입니다(키워드 이름과 겹치지 않도록 표시를 붙였습니다).
FULL_SCAN_STATE_KEY = "__full_scan__"        # 마지막 '전체 조회' 시각
LAST_SEARCH_OK_KEY = "__last_search_ok__"    # 마지막으로 검색에 성공한 시각
LAST_CREATED_OK_KEY = "__last_created_ok__"  # 등록 시각을 정상적으로 받아온 마지막 시각
LAST_ITEMS_OK_KEY = "__last_items_ok__"      # 검색이 매물을 한 건이라도 돌려준 마지막 시각
LAST_CADENCE_OK_KEY = "__last_cadence_ok__"  # 실행 주기가 정상이었던 마지막 시각
RESERVED_STATE_KEYS = (
    FULL_SCAN_STATE_KEY,
    LAST_SEARCH_OK_KEY,
    LAST_CREATED_OK_KEY,
    LAST_ITEMS_OK_KEY,
    LAST_CADENCE_OK_KEY,
)

# 봇 고장 감지.
# 검색이 이 시간 넘게 한 건도 성공하지 못하면 "봇이 멈춘 것 같다"고 알립니다.
# 조용한 게 '새 매물이 없어서'인지 '봇이 죽어서'인지 구분할 수 없는 문제를 막기 위한 장치입니다.
# 한 번 실패했다고 바로 알리지는 않습니다(일시적인 네트워크 오류로 헛알림이 가지 않도록).
HEALTH_ALERT_AFTER_SECONDS = 10 * 60
# 고장이 계속되는 동안 1분마다 알림이 쏟아지지 않도록, 이 간격당 최대 한 번만 알립니다.
# 간격은 벽시계가 아니라 '고장이 시작된 시점'부터 셉니다(outage_bucket 참고).
HEALTH_ALERT_COOLDOWN_SECONDS = 6 * 60 * 60

# 키워드 하나만 막히는 경우를 잡는 기준입니다.
# 전량 실패는 위 HEALTH_ALERT_AFTER_SECONDS가 잡지만, '다른 키워드는 멀쩡한데 이 키워드만
# 계속 실패'하면 그 키워드 알림만 조용히 멈춥니다. 일시적인 실패와 구분하기 위해
# 한참 동안 한 번도 성공하지 못했을 때만 알립니다.
KEYWORD_STUCK_AFTER_SECONDS = 60 * 60

# 매물 등록 시각(created)을 받아오는 비율의 하한입니다.
# 이 값은 "오래된 매물을 신규로 오인하지 않는" 방어선이 쓰는 핵심 입력입니다.
# 메루카리 응답에서 사라지면 그 방어선이 조용히 무력화되므로, 비율이 무너지면 알립니다.
# 실측은 100%(2861/2861)라 50%면 정상 변동이 아니라 명백한 이상입니다.
CREATED_COVERAGE_MIN_RATIO = 0.5

# 실행 주기가 조용히 느려지는 것을 잡는 기준입니다.
#
# 이 봇의 1분 주기는 **저장소 밖의 cron 서비스**가 workflow_dispatch를 호출해서 만듭니다
# (실측: 실행의 약 91%가 dispatch, 나머지 9%가 아래 워크플로의 5분 스케줄).
# 그 cron이 멈추면 봇은 죽지 않고 **5분 주기로 조용히 떨어집니다.** 실행은 전부 성공하고
# 검색도 정상이라 기존 고장 알림은 하나도 울리지 않는데, 새 매물 알림만 최대 5분 늦어집니다.
# 5분 스케줄이 백업이자 동시에 이 저하를 가려 주는 셈이라, 따로 보지 않으면 알 수 없습니다.
#
# 주기를 바꾸면 이 값도 같이 바꿔 주세요(README "실행 주기 설정" 참고).
EXPECTED_RUN_INTERVAL_SECONDS = 60
# 기대 주기의 몇 배까지를 정상으로 볼지. 실행이 겹쳐 취소되는 일이 실측 약 6% 있어서
# 한두 번 건너뛰는 건 정상입니다. 3배(3분)를 넘으면 주기 자체가 달라진 것으로 봅니다.
RUN_INTERVAL_SLACK = 3

# 전송 단계는 알림 하나마다 git push까지 하기 때문에 한 건당 수 초가 걸립니다.
# 한 실행이 5분 크론을 넘겨 다음 실행이 줄줄이 밀리지 않도록 한 번에 보낼 양을 제한하고,
# 남은 알림은 다음 실행에서 이어서 보냅니다(대기열은 그대로 보존됩니다).
SEND_INTERVAL_SECONDS = 1.5
MAX_SEND_ATTEMPTS_PER_RUN = 40
MAX_ALERT_ATTEMPTS = 3
MAX_CONSECUTIVE_SEND_FAILURES = 5
# 건수 상한만으로는 실행 시간이 묶이지 않습니다. 텔레그램이 레이트리밋(429)을 걸면
# 한 건마다 최대 1분을 기다리므로, 상한 40건이 곧 40분이 될 수 있습니다.
# 워크플로는 concurrency 그룹으로 한 번에 하나만 돌기 때문에 그동안 모든 조회가 멈춥니다.
# 시간 상한을 따로 둬서, 오래 걸리는 실행은 남은 대기열을 다음 실행에 넘기고 끝냅니다.
MAX_SEND_SECONDS_PER_RUN = 4 * 60

# 등록 시각 기준으로 '신규'를 판정할 때 쓰는 여유값들(초 단위).
# GRACE: 크론 지연·실행 큐 대기 때문에 직전 조회 시각이 조금 밀릴 수 있어 그만큼 넉넉히 봅니다.
# FIRST_LOOKBACK: 해당 키워드의 직전 조회 기록이 아직 없을 때(업그레이드 직후 첫 실행) 쓰는 기본 창.
# MAX_LOOKBACK: 봇이 오래 멈춰 있다 살아났을 때 며칠치가 한꺼번에 쏟아지지 않도록 상한을 둡니다.
NEW_ITEM_GRACE_SECONDS = 15 * 60
FIRST_RUN_LOOKBACK_SECONDS = 60 * 60
MAX_LOOKBACK_SECONDS = 24 * 60 * 60


class SendBlocked(RuntimeError):
    """이번 실행의 텔레그램 전송이 통째로 막혔습니다(토큰 오류, 텔레그램 장애 등).

    개별 알림의 문제와 구분해서 다뤄야 합니다. 한 건만 실패하는 건 그 알림을 포기하면
    되지만, 전부 실패하는 건 사람이 손을 써야 하는 고장입니다. 그런데 전송 경로가
    막힌 상태에서는 고장을 알릴 수단(텔레그램)도 같이 막혀 있어서, 대기열만 조용히
    소모되고 워크플로는 초록색으로 남습니다. 이 예외로 전송 단계를 실패시켜서
    Actions 탭과 기존 실행-실패 알림(scripts/notify_failure.sh, 별도의 curl 경로)에
    드러나게 합니다.
    """


SEARCHES = [
    {"query": "Carol Christian Poell", "categories": []},
    {"query": "Martin Margiela", "categories": [30]},
    {"query": "マルタンマルジェラ", "categories": [30]},
    {"query": "Margiela", "categories": [2, 1]},
    {"query": "マルジェラ", "categories": [2, 1]},
    {"query": "Hermes Margiela", "categories": []},
    {"query": "Allegri Margiela", "categories": []},
    {"query": "Hermes", "categories": [30, 31, 32, 11, 12, 13]},
    {"query": "エルメス", "categories": [30, 31, 32, 11, 12, 13]},
    {"query": "Chrome Hearts", "categories": [30, 31, 32, 11]},
    {"query": "クロムハーツ", "categories": [30, 31, 32, 11]},
    {"query": "Richard Avedon", "categories": [3088]},
    {"query": "The Row", "categories": [2]},
    {"query": "ザ・ロウ", "categories": [2]},
    {"query": "Gunter Wermekes", "categories": []},
    {"query": "Label Under Construction", "categories": []},
    {"query": "Yves Saint Laurent Rive Gauche", "categories": [2]},
    {"query": "YSL Rive Gauche", "categories": [2]},
]

# 이 기능(키워드별 '첫 조회' 추적)을 배포하기 전부터 이미 돌리고 있던 키워드 목록입니다.
# known_keywords가 상태 파일에 아직 없을 때(첫 업그레이드 실행)만 이 목록을 '이미 알려짐'으로
# 간주해서, 오래전부터 쓰던 키워드까지 신규로 오인해 알림을 생략해버리는 일을 막습니다.
# 이후 새 키워드를 추가할 때는 이 목록을 더 손댈 필요가 없습니다(실행 한 번이면 자동으로 등록됨).
LEGACY_KEYWORDS_SEEDED_AT_UPGRADE = [
    "Carol Christian Poell",
    "Martin Margiela",
    "マルタンマルジェラ",
    "Margiela",
    "マルジェラ",
    "Hermes Margiela",
    "Hermes",
    "エルメス",
    "Chrome Hearts",
    "クロムハーツ",
    "Richard Avedon",
    "The Row",
    "ザ・ロウ",
]

# 현재 코드가 만들어 내는 재출품 지문 접두사입니다.
# 예전(또는 외부 도구가 잠깐 끼어들어 만든) 형식의 지문은 지금 코드가 조회하지 않으면서
# 용량 상한만 잡아먹기 때문에, 상태를 읽을 때 한 번 정리합니다.
SUPPORTED_FINGERPRINT_PREFIXES = ("seller:", "title:")


def build_search_options() -> dict:
    """정렬 방식별 검색 옵션을 준비합니다.

    - created: 등록 시각 내림차순. 새 매물을 놓치지 않기 위한 본 패스입니다.
    - score: 메루카리 기본값(추천순). 오래된 매물의 가격 인하를 계속 추적하기 위해 함께 봅니다.
    두 패스 모두 '판매중'만 조회해서, 이미 팔린 매물이 자리를 차지하지 않게 합니다.

    mercapi가 없거나(테스트) 옵션 이름이 바뀌어도 봇이 죽지 않도록 실패 시 기본 검색으로 물러납니다.
    """
    try:
        from mercapi.requests import SearchRequestData as request_data

        on_sale = [request_data.Status.STATUS_ON_SALE]
        return {
            "created": {
                "sort_by": request_data.SortBy.SORT_CREATED_TIME,
                "sort_order": request_data.SortOrder.ORDER_DESC,
                "status": on_sale,
            },
            "score": {
                "sort_by": request_data.SortBy.SORT_SCORE,
                "sort_order": request_data.SortOrder.ORDER_DESC,
                "status": on_sale,
            },
        }
    except Exception as exc:  # pragma: no cover - 라이브러리 구조가 바뀐 예외 상황
        print(f"[검색 정렬 옵션 준비 실패 -> 기본 검색으로 진행] {exc}", file=sys.stderr)
        return {"created": {}, "score": {}}


SEARCH_SORT_OPTIONS = build_search_options()

# 새 매물을 책임지는 정렬 패스의 이름입니다. 이 조회가 실패하면 그 구간의 새 매물을
# 놓친 것이므로, 해당 키워드의 조회 시각을 갱신하면 안 됩니다.
NEW_ITEM_SORT_PASS = "created"

# 1분처럼 짧은 주기로 돌릴 때를 위한 구분입니다.
# - 빠른 조회: 등록순만 봅니다. 새 매물을 놓치지 않는 데는 이것만으로 충분하고,
#   실행 시간과 메루카리 API 호출량이 절반으로 줄어 짧은 주기에서도 밀리지 않습니다.
# - 전체 조회: 추천순까지 함께 봐서 오래 올라와 있는 매물의 가격 인하도 확인합니다.
#
# 실행 주기에 맞춰 이 값 하나만 조절하면 됩니다.
#   0     : 매 실행마다 전체 조회 (2분 이상 주기로 돌릴 때 권장 — 가격 인하를 가장 빨리 잡음)
#   5*60  : 1분 주기로 돌릴 때 권장 (새 매물은 매분, 가격 인하는 5분마다)
FULL_SCAN_INTERVAL_SECONDS = 5 * 60


def load_state() -> tuple[dict, list, list, dict, set, dict]:
    empty = ({}, [], [], {}, set(LEGACY_KEYWORDS_SEEDED_AT_UPGRADE), {})

    if not SEEN_FILE.exists():
        return empty
    try:
        data = json.loads(SEEN_FILE.read_text())
    except Exception as exc:
        # 파일이 있는데 읽지 못하는 상황에서 빈 상태로 출발하면 두 가지를 한꺼번에 잃습니다.
        #   - sent_alerts: 이미 보낸 알림 기록이 사라져 예전 알림이 다시 나갈 수 있습니다.
        #   - 원본: 이어지는 save_state가 깨진 파일을 '정상' 파일로 덮어써, 되돌릴 대상마저 사라집니다.
        # 조용히 지나가는 대신 실행을 멈춥니다. 워크플로가 실패하면 텔레그램으로 알림이 가고,
        # 상태 파일은 git에 남아 있으므로 직전 정상본으로 되돌리면 그대로 복구됩니다.
        raise RuntimeError(
            f"상태 파일({SEEN_FILE})을 읽지 못했습니다: {exc}\n"
            "빈 상태로 새로 시작하면 이미 보낸 알림이 다시 나갈 수 있어 실행을 멈춥니다.\n"
            "git 이력에서 직전 정상본을 되돌린 뒤 다시 실행해 주세요 "
            "(예: git checkout HEAD~1 -- seen_items.json)."
        ) from exc

    if isinstance(data, list):
        return ({item_id: None for item_id in data},) + empty[1:]

    raw_seen = data.get("seen", [])
    seen = {item_id: None for item_id in raw_seen} if isinstance(raw_seen, list) else raw_seen
    pending = data.get("pending", [])
    sent_alerts = data.get("sent_alerts", [])
    relist_fingerprints = data.get("relist_fingerprints", {})
    raw_known_keywords = data.get("known_keywords")
    known_keywords = (
        set(raw_known_keywords)
        if isinstance(raw_known_keywords, list)
        else set(LEGACY_KEYWORDS_SEEDED_AT_UPGRADE)
    )
    raw_checked_at = data.get("keyword_checked_at")
    keyword_checked_at = dict(raw_checked_at) if isinstance(raw_checked_at, dict) else {}
    # 마지막 '전체 조회' 시각은 keyword_checked_at 안에 예약 키로 같이 보관합니다.
    # (상태 파일 형식과 병합 로직을 그대로 두면서 값 하나만 늘리기 위한 선택입니다.)

    return (
        seen if isinstance(seen, dict) else {},
        pending if isinstance(pending, list) else [],
        sent_alerts if isinstance(sent_alerts, list) else [],
        prune_fingerprints(relist_fingerprints if isinstance(relist_fingerprints, dict) else {}),
        known_keywords,
        keyword_checked_at,
    )


def is_usable_fingerprint(key: str) -> bool:
    """지금 코드가 실제로 다시 조회하게 될 지문인지 판단합니다.

    두 가지를 걸러냅니다.
      - 현재 코드가 만들지 않는 형식(예전 버전이나 외부 도구가 남긴 것).
      - 판매자 ID를 0으로 적어 둔 지문. 숍스 상품의 sellerId가 0으로 내려오던 때
        만들어진 것들인데, 지금은 그런 매물을 'title:' 지문으로 다루므로 영영 조회되지
        않습니다. 남겨 두면 용량 상한만 차지합니다.
    """
    key = str(key)
    if not key.startswith(SUPPORTED_FINGERPRINT_PREFIXES):
        return False
    parts = key.split(":")
    if parts[0] == "seller" and len(parts) > 1 and parts[1].strip().lower() in UNKNOWN_SELLER_IDS:
        return False
    return True


def prune_fingerprints(relist_fingerprints: dict) -> dict:
    """다시 조회될 일이 없는 재출품 지문을 걸러냅니다.

    값이 dict가 아닌 지문도 함께 버립니다. 지금 코드는 지문 값에서 item_id와 가격을
    꺼내 쓰는데(matched.get(...)), 예전 형식이 섞여 들어오면 그 순간 조회가 통째로
    죽고 다음 실행에서도 같은 자리에서 다시 죽습니다.
    """
    return {
        key: value
        for key, value in relist_fingerprints.items()
        if is_usable_fingerprint(key) and isinstance(value, dict)
    }


def alert_key(entry: dict) -> str:
    value = entry.get("alert_id")
    if value:
        return str(value)
    return f"legacy:{entry.get('caption', '')}"


def unique_recent(values: list[str], limit: int) -> list[str]:
    result = []
    seen_values = set()
    for value in values:
        value = str(value)
        if value and value not in seen_values:
            result.append(value)
            seen_values.add(value)
    return result[-limit:]


def has_sendable_caption(entry: dict) -> bool:
    """텔레그램으로 실제 보낼 수 있는 본문이 있는지 확인합니다.

    본문이 없는 항목은 전송을 시도하는 순간 터지는데, 터지면 그 항목이 대기열에
    그대로 남아 다음 실행도 같은 자리에서 터집니다. 즉 한 번 섞여 들어오면
    전송 단계가 영영 멈춥니다. 대기열을 만들 때 걸러 내는 편이 안전합니다.
    """
    caption = entry.get("caption")
    return isinstance(caption, str) and bool(caption.strip())


def deduplicate_pending(pending: list, sent_alerts: list) -> list:
    sent_keys = set(sent_alerts)
    pending_keys = set()
    result = []
    broken = 0
    for entry in pending:
        if not isinstance(entry, dict):
            continue
        if not has_sendable_caption(entry):
            broken += 1
            continue
        key = alert_key(entry)
        if key in sent_keys or key in pending_keys:
            continue
        normalized = dict(entry)
        normalized.setdefault("alert_id", key)
        result.append(normalized)
        pending_keys.add(key)
    if broken:
        print(f"[경고] 본문이 없는 대기 알림 {broken}건을 버렸습니다", file=sys.stderr)
    if len(result) > MAX_PENDING_ALERTS:
        # 상한을 넘으면 가장 오래된 알림부터 버려집니다. 조용히 사라지면 원인을 찾기
        # 어려우므로 반드시 로그를 남깁니다(평소에는 절대 찍히지 않아야 정상입니다).
        print(
            f"[경고] 대기 알림이 상한({MAX_PENDING_ALERTS}건)을 넘어 "
            f"오래된 {len(result) - MAX_PENDING_ALERTS}건을 버립니다",
            file=sys.stderr,
        )
    return result[-MAX_PENDING_ALERTS:]


def remember(store: dict, key, value) -> None:
    """가장 최근에 본 항목이 항상 dict의 맨 뒤에 오도록 다시 넣습니다.

    파이썬 dict는 이미 있는 키에 값만 바꾸면 원래 자리(처음 넣은 순서)를 그대로 지킵니다.
    상태 파일은 용량 상한 때문에 '뒤에서부터 N개'만 남기므로, 이 처리를 빼먹으면
    매번 검색에 걸리는 '오래 올라와 있는 인기 매물'이 가장 먼저 잘려 나가고,
    다음 조회에서 처음 보는 매물로 오인돼 알림이 갑니다. (실제로 발생했던 버그)
    """
    store.pop(key, None)
    store[key] = value


def state_update_order(fields: dict) -> tuple:
    """상태를 갱신할 순서. 매 실행 같아야 합니다.

    remember()는 항목을 dict 맨 뒤로 보냅니다. 그래서 '어떤 순서로 갱신하는지'가 곧
    상태 파일의 바이트 배치를 결정합니다. 지금까지는 검색 결과 순서대로 갱신했는데,
    메루카리 결과 순서는 매 실행 흔들립니다(새 매물이 끼어들고 추천순이 바뀜).
    그러면 **같은 매물을 같은 집합으로 다시 봤을 때도** 1.5MB 한 줄 JSON이 통째로
    다시 쓰이고, 그게 커밋당 델타의 대부분을 차지했습니다.

        queue Mercari alerts   공통접두 0.00MB 공통접미 0.00MB 변경 1503KB (파일 100%)

    다만 이 재배치가 저장소 용량에 얼마나 기여했는지는 확인되지 않았습니다. 같은 크기 창
    (각 429커밋, 약 3시간)으로 전후를 재면 시간당 증가량이 0.79 -> 0.83 MB/시간으로
    차이가 없습니다. 파일이 통째로 다시 쓰이는 것처럼 보여도 git 델타 압축이 상당 부분을
    흡수합니다. 그러니 이 함수의 값은 용량이 아니라 '결정성'으로 봐 주세요 — 같은 매물을
    같은 집합으로 다시 보면 저장 결과가 바이트 단위로 같고, 테스트가 그걸 확인합니다.
    (배포 직후 작은 창으로 재고 -72%라고 적었던 적이 있는데 측정 방법의 산물이었습니다.
    자세한 경위는 README "저장소 용량 관리" 참고.)

    남는 churn은 빠른 조회(등록순만)와 전체 조회(등록순+추천순)가 5분마다 교대하면서
    '이번에 관찰한 집합'이 오르내리기 때문입니다.

    매물에 붙어 있는 값(등록 시각, ID)으로 정렬하면 순서가 실행과 무관해집니다.
    오름차순이라서 갓 올라온 매물이 dict 맨 뒤, 즉 용량 상한에서 가장 먼 자리에
    놓입니다 — 예전에는 검색 결과 위치에 따라 아무 데나 놓였으니 이쪽이 더 안전합니다.

    등록 시각을 모르는 매물은 앞쪽으로 보냅니다. 신규 판정에서도 가장 약한 근거를
    가진 항목이라, 상한에 먼저 닿는 자리에 두는 편이 맞습니다.
    """
    created = listing_created_at(fields)
    item_id = extract_item_id(fields) or ""
    if created is None:
        return (0, 0.0, item_id)
    return (1, created, item_id)


def save_state(
    seen: dict,
    pending: list,
    sent_alerts: list,
    relist_fingerprints: dict,
    known_keywords: set,
    keyword_checked_at: dict | None = None,
) -> None:
    sent_alerts = unique_recent(sent_alerts, MAX_SENT_ALERTS)
    pending = deduplicate_pending(pending, sent_alerts)
    relist_fingerprints = prune_fingerprints(relist_fingerprints)
    data = {
        "seen": dict(list(seen.items())[-MAX_SEEN_ITEMS:]),
        "pending": pending,
        "sent_alerts": sent_alerts,
        "relist_fingerprints": dict(list(relist_fingerprints.items())[-MAX_RELIST_FINGERPRINTS:]),
        "known_keywords": sorted(known_keywords),
        "keyword_checked_at": keyword_checked_at or {},
    }
    temporary_file = SEEN_FILE.with_suffix(".tmp")
    temporary_file.write_text(json.dumps(data, ensure_ascii=False))
    temporary_file.replace(SEEN_FILE)


def absorb_pushed_state(
    seen: dict,
    pending: list,
    sent_alerts: list,
    relist_fingerprints: dict,
    known_keywords: set,
    keyword_checked_at: dict | None = None,
) -> None:
    """push_state가 원격과 병합했다면 그 결과를 메모리로 되가져옵니다.

    push_state.sh는 push가 거부되면 원격 상태를 받아 merge_seen.py로 병합하고
    seen_items.json을 그 결과로 고쳐 씁니다. 그런데 전송 루프는 알림 하나마다
    **자기 메모리 값으로** 파일을 다시 쓰므로, 병합으로 들어온 상대편 기록이 바로
    다음 저장에서 사라집니다. 그리고 그 다음 push는 충돌 없이 통과하기 때문에
    병합 경로가 다시 불려 복구될 기회도 없습니다.

    특히 sent_alerts가 사라지면 상대편이 이미 보낸 알림이 대기열에 되살아나
    다음 실행에서 다시 나갑니다. 병합 로직이 막으려던 바로 그 일입니다.

    병합이 없었다면 파일은 방금 save_state가 쓴 그대로이므로 이 함수는 아무것도
    바꾸지 않습니다. 즉 정상 경로의 동작은 달라지지 않습니다.

    파일의 값이 곧 '병합된 결과'이므로 그대로 옮겨 씁니다(호출한 쪽이 같은 객체를
    들고 있으므로 새 객체를 만들지 않고 제자리에서 바꿉니다). 대기열만은 예외로,
    이미 순회 중이라 상대편에게서 새로 들어온 알림을 **뒤에 덧붙이기만** 합니다.
    """
    if not SEEN_FILE.exists():
        return  # 바로 앞 save_state가 쓴 파일이 없다면 되읽을 병합 결과도 없습니다.
    try:
        data = json.loads(SEEN_FILE.read_text())
    except Exception as exc:
        # 여기서 멈출 일은 아닙니다. 메모리 상태를 그대로 쓰면 기존 동작과 같고,
        # 저장이 정말 깨졌다면 다음 push_state가 실행을 실패로 끝냅니다.
        print(f"[경고] 병합된 상태를 되읽지 못했습니다: {exc}", file=sys.stderr)
        return
    if not isinstance(data, dict):
        return

    for target, key in (
        (seen, "seen"),
        (relist_fingerprints, "relist_fingerprints"),
        (keyword_checked_at, "keyword_checked_at"),
    ):
        merged = data.get(key)
        if target is not None and isinstance(merged, dict):
            target.clear()
            target.update(merged)

    merged_sent = data.get("sent_alerts")
    if isinstance(merged_sent, list):
        sent_alerts[:] = [str(value) for value in merged_sent]

    merged_keywords = data.get("known_keywords")
    if isinstance(merged_keywords, list):
        known_keywords.clear()
        known_keywords.update(merged_keywords)

    merged_pending = data.get("pending")
    if isinstance(merged_pending, list):
        queued = {alert_key(entry) for entry in pending if isinstance(entry, dict)}
        delivered = set(sent_alerts)
        for entry in merged_pending:
            if not isinstance(entry, dict):
                continue
            key = alert_key(entry)
            if key in queued or key in delivered:
                continue
            pending.append(entry)
            queued.add(key)


def push_state(commit_message: str) -> bool:
    """scripts/push_state.sh를 호출해 현재 상태 파일을 원격 저장소에 즉시 반영합니다.

    텔레그램 알림을 하나 보낼 때마다 이 함수를 호출해서,
    '전송 완료 기록'이 원격에 반영되기 전의 위험 구간을 최소화합니다.
    실패하면 False를 반환하며, 호출한 쪽에서 더 이상의 전송을 멈춰야 합니다.
    """
    try:
        result = subprocess.run(
            ["bash", "scripts/push_state.sh", commit_message],
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        print(f"[상태 저장 실행 실패] {exc}", file=sys.stderr)
        return False

    if result.returncode != 0:
        print(f"[상태 저장 실패]\n{result.stdout}\n{result.stderr}", file=sys.stderr)
        return False
    if result.stdout.strip():
        print(result.stdout.strip())
    return True


def price_record(seen: dict, item_id) -> dict:
    """seen[item_id]를 {'last_alert_price', 'last_seen_price'} 형태로 정규화합니다.

    예전 버전은 seen[item_id]에 정수(또는 None) 하나만 저장했기 때문에,
    그런 값을 만나면 두 필드에 동일하게 채워서 자연스럽게 새 형식으로 넘어가게 합니다.
    """
    value = seen.get(item_id)
    if isinstance(value, dict):
        return {
            "last_alert_price": value.get("last_alert_price"),
            "last_seen_price": value.get("last_seen_price"),
        }
    return {"last_alert_price": value, "last_seen_price": value}


def normalize_title(name) -> str:
    """재출품 판별용으로 제목을 정규화합니다 (공백/기호/전각·반각 차이를 무시)."""
    text = unicodedata.normalize("NFKC", str(name or ""))
    text = _TITLE_NOISE_PATTERN.sub("", text)
    return text.lower()


def item_fields(item) -> dict:
    """검색 결과 항목을 평범한 dict로 바꿉니다.

    asdict()는 중첩 구조까지 전부 깊은 복사를 하는 무거운 함수라, 한 매물마다 필드를
    꺼낼 때마다 부르면 실행 시간이 크게 늘어납니다. 매물당 한 번만 변환해서 돌려씁니다.
    """
    if isinstance(item, dict):
        return item
    try:
        return asdict(item)
    except Exception:
        return dict(getattr(item, "__dict__", {}) or {})


def extract_seller_id(item) -> str | None:
    """검색 결과에서 판매자 ID를 뽑아냅니다. 라이브러리 버전에 따라 필드 위치가
    다를 수 있어 평평한 필드와 중첩된 seller 객체를 모두 시도합니다.

    숍스 상품처럼 판매자 ID가 0으로 내려오는 경우는 '모름'으로 취급합니다
    (0을 그대로 쓰면 서로 다른 숍스 상품이 같은 판매자로 묶여 버립니다)."""

    def clean(value):
        if value is None:
            return None
        text = str(value).strip()
        return None if text.lower() in UNKNOWN_SELLER_IDS else text

    data = item_fields(item)
    direct = clean(data.get("seller_id")) or clean(data.get("sellerId"))
    if direct:
        return direct
    seller = data.get("seller")
    if isinstance(seller, dict):
        nested = clean(seller.get("id")) or clean(seller.get("id_")) or clean(seller.get("seller_id"))
        if nested:
            return nested
    return None


def relist_fingerprint(seller_id, name, price) -> str | None:
    """같은 매물의 재출품(삭제 후 재등록)을 잡아내기 위한 지문을 만듭니다.

    판매자 ID를 알 수 있으면 '판매자+정규화된 제목'만으로 판단하고(가격이 달라도 매칭),
    판매자 ID를 못 가져오면 '정규화된 제목+정확히 같은 가격'으로 대체합니다.
    """
    normalized_title = normalize_title(name)
    if not normalized_title:
        return None
    if seller_id:
        return f"seller:{seller_id}:{normalized_title}"
    if isinstance(price, int):
        return f"title:{normalized_title}:{price}"
    return None


def extract_field(item, candidates, default=None):
    """여러 후보 필드명 중 값이 있는 첫 번째를 돌려줍니다(매물 객체/dict 둘 다 허용)."""
    data = item_fields(item)
    for key in candidates:
        value = data.get(key)
        if value:
            return value
    return default


def extract_item_id(item) -> str | None:
    """매물 ID를 항상 문자열로 정규화해서 돌려줍니다.

    상태 파일은 JSON이라 저장 시 키가 무조건 문자열이 됩니다. 여기서 정수 ID가
    섞여 들어오면 저장 전후로 키가 달라져서, 다음 실행에 같은 매물을 처음 보는
    매물로 오인하게 됩니다."""
    value = extract_field(item, ["id_", "id", "item_id", "itemId"])
    return str(value) if value else None


def listing_created_at(item) -> float | None:
    """매물의 실제 등록 시각을 epoch 초로 돌려줍니다 (없으면 None)."""
    value = item.get("created") if isinstance(item, dict) else getattr(item, "created", None)
    if isinstance(value, datetime):
        try:
            return value.timestamp()
        except Exception:
            return None
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    return None


def current_time() -> float:
    """현재 시각을 epoch 초로 돌려줍니다(테스트에서 시간을 갈아끼울 수 있도록 분리)."""
    return datetime.now().timestamp()


def new_item_cutoff(keyword_checked_at: dict, keyword: str, now: float) -> float:
    """이 키워드에서 '신규'로 볼 등록 시각의 하한선을 계산합니다.

    - 직전에 이 키워드를 조회한 시각이 기준선입니다(그 이후 등록된 것만 새 매물).
    - 조회 기록이 없으면(업그레이드 직후 첫 실행) 최근 1시간만 신규로 봅니다.
    - 봇이 오래 멈춰 있었다면 최대 24시간까지만 거슬러 올라갑니다.
    """
    if keyword in RESERVED_STATE_KEYS:
        return now - FIRST_RUN_LOOKBACK_SECONDS
    last_checked = keyword_checked_at.get(keyword)
    if not isinstance(last_checked, (int, float)):
        return now - FIRST_RUN_LOOKBACK_SECONDS
    return max(float(last_checked), now - MAX_LOOKBACK_SECONDS)


def is_fresh_listing(created_at: float | None, cutoff: float | None) -> bool:
    """등록 시각 기준으로 '이번에 새로 올라온 매물'인지 판단합니다.

    등록 시각을 알 수 없거나 비교 기준이 없으면, 예전처럼 상태 파일 기준으로만 판단하도록
    True를 돌려줍니다(알림을 놓치는 쪽보다 한 번 더 보내는 쪽이 안전).
    """
    if created_at is None or cutoff is None:
        return True
    return created_at >= cutoff - NEW_ITEM_GRACE_SECONDS


async def _telegram_post(token: str, chat_id: str, method: str, payload: dict):
    """텔레그램 API를 한 번 호출하고 (성공여부, 재시도대기초, 상태코드)를 돌려줍니다."""
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            response = await client.post(
                f"https://api.telegram.org/bot{token}/{method}",
                data={"chat_id": chat_id, **payload},
            )
        except Exception as exc:
            print(f"[텔레그램 전송 에러] {exc}", file=sys.stderr)
            return False, None, None

    if response.status_code == 200:
        return True, None, 200

    retry_after = None
    try:
        retry_after = response.json().get("parameters", {}).get("retry_after")
    except Exception:
        pass
    print(f"[텔레그램 전송 실패 {response.status_code}] {response.text}", file=sys.stderr)
    return False, retry_after, response.status_code


async def send_telegram(caption: str, photo_url):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[텔레그램 설정 누락] TELEGRAM_BOT_TOKEN 또는 TELEGRAM_CHAT_ID", file=sys.stderr)
        return False, None

    if photo_url:
        ok, retry_after, status = await _telegram_post(
            token, chat_id, "sendPhoto", {"caption": caption, "photo": photo_url}
        )
        if ok:
            return True, None
        # 메루카리 썸네일은 webp라서 텔레그램이 사진으로 받아주지 않는 경우가 있습니다.
        # 사진 때문에 알림 자체를 놓치지 않도록, 잘못된 요청(4xx)이면 텍스트로 한 번 더 시도합니다.
        if status is not None and 400 <= status < 500 and status != 429:
            print("[사진 전송 실패 -> 텍스트로 재시도]", file=sys.stderr)
            ok, retry_after, _ = await _telegram_post(token, chat_id, "sendMessage", {"text": caption})
            return ok, retry_after
        return False, retry_after

    ok, retry_after, _ = await _telegram_post(token, chat_id, "sendMessage", {"text": caption})
    return ok, retry_after


async def flush_pending(
    seen: dict,
    pending: list,
    sent_alerts: list,
    relist_fingerprints: dict,
    known_keywords: set,
    keyword_checked_at: dict | None = None,
) -> tuple[list, list]:
    """대기열을 전송하고, 성공할 때마다 곧바로 원격 저장소에 기록합니다.

    한 건이라도 전송된 뒤 원격 저장 기록(push_state)이 실패하면 즉시 멈춥니다.
    이미 텔레그램으로는 전송됐으므로 sent_alerts에는 남겨 두고(그래야 이후
    save_state 호출에서 결국 반영됨), 그 이상 대기열을 처리하지 않아
    "배치 전체 중복 재전송" 위험을 '방금 보낸 1건'으로 최소화합니다.

    전송에 실패한 알림은 대기열 뒤로 미뤄 두고 다음 알림을 먼저 보냅니다.
    한 건이 막혀서 뒤에 쌓인 정상 알림까지 지연되지 않게 하려는 것이며,
    같은 알림을 한 실행에서 두 번 시도하지는 않습니다(재시도는 다음 실행에서).
    전부 실패하거나 한 실행 전송 상한에 도달하면 남은 알림은 그대로 보존한 채 끝냅니다.

    '이 알림만의 문제'와 '전송 경로가 막힌 것'은 한 실행에서 한 건이라도 전송됐는지로
    가릅니다. 다른 알림은 잘 나갔는데 이 건만 실패했다면 그 알림 쪽 문제이므로 재시도
    횟수를 세고 MAX_ALERT_ATTEMPTS에서 포기합니다. 반대로 한 건도 못 보냈다면 어느
    알림 탓이라고 할 근거가 없으므로, 이번 실행에서 올린 재시도 횟수를 되돌리고
    SendBlocked를 올립니다.

    되돌리지 않으면 장애가 이어지는 동안 멀쩡한 알림들이 차례로 MAX_ALERT_ATTEMPTS에
    걸려 폐기됩니다. 봇이 1분마다 도니까 텔레그램이나 토큰이 몇 시간 막히면 대기열이
    소리 없이 비어 버리는데, 전송 경로가 막힌 상태에서는 그 사실을 알릴 수단도 같이
    막혀 있어서 워크플로만 초록색으로 남습니다.
    """
    remaining = list(pending)
    sent_keys = set(sent_alerts)
    failed_keys: set[str] = set()
    sent = 0
    consecutive_failures = 0
    attempted = 0
    started_at = current_time()
    out_of_time = False
    # 이번 실행에서 재시도 횟수를 올린 알림들입니다. 한 건도 전송되지 않은 채 실행이
    # 끝나면 되돌립니다(개별 알림의 잘못이라고 볼 근거가 없으므로).
    failed_entries: list[dict] = []
    # 재시도 한도를 다 써서 버린 알림들. 로그는 '막힌 실행'인지 판정된 뒤에 남깁니다
    # (막힌 실행에서는 한도를 되돌리므로 버리지 않습니다).
    abandoned: list[dict] = []
    while remaining and attempted < MAX_SEND_ATTEMPTS_PER_RUN:
        if current_time() - started_at >= MAX_SEND_SECONDS_PER_RUN:
            out_of_time = True
            break
        entry = remaining[0]
        key = alert_key(entry)
        if key in sent_keys:
            remaining.pop(0)
            continue
        if key in failed_keys:
            # 이번 실행에서 이미 시도했다가 실패해 뒤로 미뤄 둔 알림입니다.
            # 여기까지 돌아왔다는 건 대기열을 한 바퀴 다 돌았다는 뜻이므로 이번 실행은 마칩니다.
            # (재시도는 다음 실행에서 — 일시적인 장애가 회복될 시간을 줍니다.)
            break
        attempted += 1
        ok, retry_after = await send_telegram(entry.get("caption") or "", entry.get("photo"))
        if ok:
            sent_alerts.append(key)
            sent_keys.add(key)
            remaining.pop(0)
            sent += 1
            consecutive_failures = 0
            save_state(seen, remaining, sent_alerts, relist_fingerprints, known_keywords, keyword_checked_at)
            if not push_state("record Mercari alert delivery"):
                print(
                    "[중단] 전송 기록 저장에 실패해 이번 실행은 여기서 멈춥니다 "
                    "(다음 실행 때 안전하게 이어갑니다)",
                    file=sys.stderr,
                )
                break
            # push가 거부돼 원격과 병합됐다면 그 결과를 메모리로 되가져옵니다.
            # 이걸 빼먹으면 다음 알림의 save_state가 병합 결과를 덮어써서, 상대편이
            # 이미 보낸 알림이 다시 나갑니다(absorb_pushed_state 주석 참고).
            absorb_pushed_state(
                seen, remaining, sent_alerts, relist_fingerprints, known_keywords, keyword_checked_at
            )
            sent_keys = set(sent_alerts)
            await asyncio.sleep(SEND_INTERVAL_SECONDS)
            continue
        if retry_after and retry_after <= 60:
            if current_time() - started_at + retry_after >= MAX_SEND_SECONDS_PER_RUN:
                # 기다렸다가 보내면 시간 상한을 넘깁니다. 대기열은 그대로 두고 다음 실행에 넘깁니다.
                out_of_time = True
                break
            print(f"[레이트리밋] {retry_after}초 대기 후 재시도", file=sys.stderr)
            await asyncio.sleep(retry_after + 1)
            continue

        consecutive_failures += 1
        failed_keys.add(key)
        entry["attempts"] = entry.get("attempts", 0) + 1
        failed_entries.append(entry)
        remaining.pop(0)
        if entry["attempts"] >= MAX_ALERT_ATTEMPTS:
            abandoned.append(entry)
        else:
            # 실패한 알림을 맨 앞에 그대로 두면 그 한 건 때문에 뒤에 쌓인 알림이 전부
            # 막혀서(매 실행 1회 재시도 -> 15분 정체) 정상 알림까지 늦어집니다.
            # 뒤로 미뤄 두고 다음 알림부터 먼저 보냅니다.
            remaining.append(entry)
        if consecutive_failures >= MAX_CONSECUTIVE_SEND_FAILURES:
            # 토큰 오류나 텔레그램 장애처럼 전체가 실패하는 상황입니다.
            # 대기열 전체를 헛돌지 않도록 이번 실행은 여기서 멈춥니다.
            print(
                f"[중단] 연속 {consecutive_failures}건 전송 실패 -> 이번 실행은 여기서 멈춥니다",
                file=sys.stderr,
            )
            break
    if remaining and out_of_time:
        print(
            f"[이번 실행 전송 시간 상한({MAX_SEND_SECONDS_PER_RUN // 60}분) 도달 -> 나머지는 다음 실행에서]",
            file=sys.stderr,
        )
    elif remaining and attempted >= MAX_SEND_ATTEMPTS_PER_RUN:
        print(
            f"[이번 실행 전송 상한({MAX_SEND_ATTEMPTS_PER_RUN}건) 도달 -> 나머지는 다음 실행에서]",
            file=sys.stderr,
        )
    if sent:
        print(f"텔레그램 알림 {sent}건 전송 완료")
    if failed_entries and not sent:
        # 한 건도 못 보냈으니 어느 알림 탓이라고 볼 근거가 없습니다. 재시도 횟수를
        # 되돌려서, 장애가 이어지는 동안 멀쩡한 알림이 폐기되지 않게 합니다.
        for entry in failed_entries:
            attempts = entry.get("attempts", 1) - 1
            if attempts > 0:
                entry["attempts"] = attempts
            else:
                entry.pop("attempts", None)
        # 대기열은 호출한 쪽이 넘겨준 그대로 남습니다(remaining을 돌려주지 않으므로
        # 이번 실행에서 뒤로 미뤄 둔 순서 변경도 함께 사라집니다).
        raise SendBlocked(
            f"{len(failed_entries)}건을 시도했지만 한 건도 전송되지 않았습니다. "
            "토큰/채팅 ID 설정이나 텔레그램 상태를 확인해 주세요."
        )
    for entry in abandoned:
        caption = str(entry.get("caption", ""))[:50]
        print(f"[알림 포기: {MAX_ALERT_ATTEMPTS}회 실패] {caption}", file=sys.stderr)
    return remaining, unique_recent(sent_alerts, MAX_SENT_ALERTS)


def wants_another_page(pass_name: str, results, items: list, created_cutoff: float | None) -> bool:
    """등록순 조회에서 한 페이지가 통째로 '신규 구간'에 들어갈 때만 다음 페이지를 봅니다.

    한 페이지(120개)가 전부 기준선 이후에 등록된 매물이라면, 그 뒤에 아직 못 본 새 매물이
    더 있을 수 있다는 뜻입니다. 인기 키워드에서 짧은 시간에 매물이 쏟아질 때 놓치지 않으려는
    장치이며, 평소(5분에 120개 미만)에는 한 페이지만 보고 끝납니다.
    """
    if pass_name != "created" or created_cutoff is None:
        return False
    if len(items) < MAX_ITEMS_PER_KEYWORD:
        return False
    if not getattr(getattr(results, "meta", None), "next_page_token", ""):
        return False
    created_times = [t for t in (listing_created_at(item) for item in items) if t is not None]
    if not created_times:
        return False
    return is_fresh_listing(min(created_times), created_cutoff)


async def search_items(
    m: Mercapi,
    keyword: str,
    categories: list,
    created_cutoff: float | None = None,
    sort_passes: list[str] | None = None,
) -> tuple[list[dict], bool, bool]:
    """한 키워드를 '등록순'과 '추천순' 두 가지로 조회해 매물 목록을 합칩니다.

    메루카리 기본 검색은 추천순(관련도)이라, 갓 올라온 매물이 상위 120개 안에 못 드는 일이
    자주 생깁니다. 그래서 새 매물은 등록순으로 확실히 잡고, 오래 올라와 있는 매물의
    가격 인하는 추천순 결과로 계속 추적합니다.

    결과는 매물 객체가 아니라 필드 dict로 돌려줍니다(무거운 변환을 매물당 한 번만 하려고).

    돌려주는 값은 (매물들, 조회 성공, 신규 매물 구간을 훑었는지) 세 가지입니다.
    마지막 값을 따로 두는 이유: 새 매물을 책임지는 건 '등록순' 조회입니다. 등록순이
    실패했는데 추천순만 성공했다고 조회 시각을 갱신해 버리면, 그 구간에 올라온 매물이
    다음 실행에서 '오래된 매물'로 분류돼 영영 알림이 오지 않습니다.

    같은 이유로, 등록순 조회가 '끝까지' 갔을 때만 훑었다고 인정합니다. 1페이지는
    받았는데 2페이지에서 터진 경우는 훑지 못한 것입니다 — 2페이지를 요청했다는 건
    1페이지가 전부 기준선 이후 등록분이어서 "뒤에 새 매물이 더 있다"고 판단했다는
    뜻이므로, 거기서 실패하면 놓친 새 매물이 있을 가능성이 높습니다.
    (페이지 상한 MAX_SEARCH_PAGES까지 다 쓴 경우는 예외로 인정합니다. 인정하지 않으면
    매물이 쏟아지는 키워드의 기준선이 영영 전진하지 못해 매 실행 같은 구간을 다시 훑고,
    키워드 단위 고장 알림까지 헛되게 울립니다.)
    """
    merged: dict = {}
    succeeded = False
    new_item_coverage = False
    passes = sort_passes if sort_passes is not None else list(SEARCH_SORT_OPTIONS)
    for index, pass_name in enumerate(passes):
        options = SEARCH_SORT_OPTIONS.get(pass_name, {})
        if index:
            await asyncio.sleep(1)
        results = None
        # 이 정렬 패스를 중간에 터지지 않고 끝까지 봤는지. 신규 매물 구간을 훑었다고
        # 인정하는 조건입니다(위 docstring 참고).
        pass_completed = False
        for page_number in range(MAX_SEARCH_PAGES):
            try:
                if page_number == 0:
                    results = await m.search(keyword, categories=categories, **options)
                else:
                    results = await results.next_page()
            except Exception as exc:
                print(
                    f"[검색 실패: {keyword} / {pass_name} {page_number + 1}페이지] {exc}",
                    file=sys.stderr,
                )
                break
            succeeded = True
            page = [item_fields(item) for item in list(getattr(results, "items", []) or [])]
            page = page[:MAX_ITEMS_PER_KEYWORD]
            for fields in page:
                item_id = extract_item_id(fields)
                if item_id and item_id not in merged:
                    merged[item_id] = fields
            if not wants_another_page(pass_name, results, page, created_cutoff):
                pass_completed = True
                break
            print(f"[{keyword}] 신규 매물이 한 페이지를 가득 채워 다음 페이지도 확인합니다")
            await asyncio.sleep(1)
        else:
            # 페이지 상한까지 다 쓴 경우입니다. 더 볼 수 있는 페이지가 남았을 수는 있지만,
            # 이건 설계상 받아들인 상한이므로 '훑었다'로 인정합니다.
            pass_completed = True
        if pass_completed and pass_name == NEW_ITEM_SORT_PASS:
            new_item_coverage = True
    if NEW_ITEM_SORT_PASS not in passes:
        # 등록순 조회를 쓸 수 없는 예외 상황(정렬 옵션 준비 실패)에서는
        # 예전처럼 '한 번이라도 성공했는지'로 판단합니다.
        new_item_coverage = succeeded
    return list(merged.values()), succeeded, new_item_coverage


async def check_keyword(
    m: Mercapi,
    keyword: str,
    categories: list,
    seen: dict,
    relist_fingerprints: dict,
    new_items: list,
    created_cutoff: float | None = None,
) -> bool:
    items, succeeded, _coverage = await search_items(m, keyword, categories, created_cutoff)
    if not succeeded:
        return False
    process_items(keyword, items, seen, relist_fingerprints, new_items, created_cutoff)
    return True


def listed_item_ids(items: list[dict]) -> set[str]:
    """지금 실제로 올라와 있는 매물 ID 집합. 재출품 판정에서
    '예전 매물이 정말 사라졌는지' 확인하는 데 씁니다."""
    return {item_id for item_id in (extract_item_id(fields) for fields in items) if item_id}


def process_items(
    keyword: str,
    items: list[dict],
    seen: dict,
    relist_fingerprints: dict,
    new_items: list,
    created_cutoff: float | None = None,
    listed_ids: set[str] | None = None,
    can_resolve_relists: bool = True,
) -> None:
    """검색 결과를 보고 신규/가격인하 알림을 만들고 상태를 갱신합니다.

    listed_ids는 '이번 실행에서 살아 있는 것이 확인된 매물 ID' 집합입니다.
    한 키워드의 결과만으로 판단하면, 같은 판매자가 제목이 똑같은 상품을 여러 개 올렸는데
    카테고리 필터 때문에 한쪽만 검색에 잡히는 경우 별개의 매물을 재출품으로 오인합니다.
    그래서 이번 실행의 모든 키워드 결과를 합쳐서 넘겨줍니다.
    """
    if listed_ids is None:
        listed_ids = listed_item_ids(items)

    new_count = 0
    drop_count = 0
    relist_count = 0
    stale_count = 0
    deferred_count = 0
    # 상태 갱신은 실행과 무관한 순서로 합니다(state_update_order 주석 참고).
    # 알림 순서는 아래에서 최신 매물이 먼저 나가도록 되돌립니다.
    alerts_start = len(new_items)
    for fields in sorted(items, key=state_update_order):
        item_id = extract_item_id(fields)
        if not item_id:
            continue
        name = fields.get("name") or extract_field(fields, ["name", "title"], "(제목 없음)")
        price = fields.get("price")
        if isinstance(price, Decimal):
            price = int(price)
        # 가격 비공개(is_no_price) 매물은 price에 9999999가 들어옵니다. 그대로 두면
        # 말도 안 되는 가격 인하 알림의 기준가가 되므로 '가격 모름'으로 취급합니다.
        if fields.get("is_no_price"):
            price = None
        photo = extract_field(fields, ["thumbnails", "photos", "thumbnail", "image_url"])
        if isinstance(photo, (list, tuple)):
            photo = photo[0] if photo else None
        seller_id = extract_seller_id(fields)
        fingerprint = relist_fingerprint(seller_id, name, price)

        # item_type에 "SHOP"이 찍히거나, ID가 일반 매물 형식(m+숫자)이 아니면 숍스 상품으로 간주합니다.
        item_type = str(extract_field(fields, ["item_type"], "")).upper()
        is_shop_item = "SHOP" in item_type or not MERCARI_ITEM_ID_PATTERN.match(str(item_id))
        item_url = (
            f"https://jp.mercari.com/shops/product/{item_id}"
            if is_shop_item
            else f"https://jp.mercari.com/item/{item_id}"
        )
        price_txt = f"¥{price:,}" if isinstance(price, int) else "가격 확인 필요"

        if item_id in seen:
            record = price_record(seen, item_id)
        else:
            # 처음 보는 ID지만 지문이 이미 있다면 두 가지 경우입니다.
            # 1) 같은 ID의 지문이 남아 있음 -> 예전에 확인했는데 seen 용량 상한 때문에 밀려난 매물.
            # 2) 다른 ID의 지문 -> 같은 판매자(또는 같은 제목+가격)가 삭제 후 재등록한 매물.
            # 어느 쪽이든 새 매물이 아니므로, 예전 가격 이력을 이어받고 '신규' 알림을 보내지 않습니다.
            matched = relist_fingerprints.get(fingerprint) if fingerprint else None
            matched_id = matched.get("item_id") if matched else None
            restored = {
                "last_alert_price": matched.get("last_alert_price"),
                "last_seen_price": matched.get("last_seen_price"),
            } if matched else None

            if matched and matched_id == item_id:
                # 같은 ID의 지문이 남아 있음 = 예전에 확인했는데 seen에서만 밀려난 매물.
                record = restored
            elif matched and matched_id is not None:
                if not can_resolve_relists:
                    # 등록순만 훑은 '빠른 조회'에서는 이 매물이 재출품인지, 제목이 우연히
                    # 같은 별개의 매물인지 가릴 근거(전체 매물 목록)가 없습니다.
                    # 잘못 판단하면 알림이 새거나 삼켜지므로, 이번 실행에서는 상태를
                    # 건드리지 않고 다음 전체 조회(최대 5분 뒤)에 맡깁니다.
                    deferred_count += 1
                    continue
                # 지문의 주인이 지금도 버젓이 올라와 있다면 재출품이 아니라 별개의 매물입니다.
                # 특히 판매자 ID를 알 수 없는 숍스 상품에서 이 혼동이 잦은데,
                # 그대로 두면 진짜 새 매물 알림이 조용히 삼켜집니다.
                if matched_id in listed_ids:
                    record = None
                else:
                    record = restored
                    relist_count += 1
            else:
                record = None

        if record is None:
            remember(seen, item_id, {"last_alert_price": price, "last_seen_price": price})
            if is_fresh_listing(listing_created_at(fields), created_cutoff):
                caption = f"[{keyword}] {name}\n💴 {price_txt}\n🔗 {item_url}"
                new_items.append({"alert_id": f"new:{item_id}", "caption": caption, "photo": photo})
                new_count += 1
            else:
                # 등록 시각이 기준선보다 한참 예전인 매물입니다. 상태 파일에서 밀려났거나
                # 검색 정렬이 흔들려 이제야 눈에 띈 것뿐이므로, 기준선만 저장하고 넘어갑니다.
                stale_count += 1
        else:
            # last_alert_price: 실제로 알림을 보낸 적 있는 '역대 최저가' 기준입니다.
            #   가격이 올랐다가 다시 내려와도 이 값보다 위에 머무는 한 알림을 보내지 않고,
            #   기준가를 절대 위로 올리지 않습니다 (그래야 잦은 가격 변동에도 기준이 안 꼬입니다).
            # last_seen_price: 참고용으로 저장하는 가장 최근 관찰가로, 알림 판단에는 쓰지 않습니다.
            last_alert_price = record["last_alert_price"]
            last_seen_price = price if isinstance(price, int) else record["last_seen_price"]

            if isinstance(last_alert_price, int) and isinstance(price, int):
                if price <= last_alert_price - PRICE_DROP_ALERT_THRESHOLD:
                    caption = (
                        f"💰[가격 인하] [{keyword}] {name}\n"
                        f"¥{last_alert_price:,} → ¥{price:,}\n🔗 {item_url}"
                    )
                    new_items.append(
                        {
                            "alert_id": f"drop:{item_id}:{last_alert_price}:{price}",
                            "caption": caption,
                            "photo": photo,
                        }
                    )
                    drop_count += 1
                    last_alert_price = price  # 역대 최저가 갱신 (알림을 보냈을 때만 내려감)
                # else: 역대 최저가보다 충분히 싸지지 않음 -> 기준가 유지 (가격이 올라도 그대로)
            else:
                last_alert_price = price

            remember(
                seen,
                item_id,
                {"last_alert_price": last_alert_price, "last_seen_price": last_seen_price},
            )

        if fingerprint:
            remember(
                relist_fingerprints,
                fingerprint,
                {
                    "item_id": item_id,
                    "last_alert_price": seen[item_id]["last_alert_price"],
                    "last_seen_price": seen[item_id]["last_seen_price"],
                },
            )

    # 갱신은 오래된 매물부터 했지만, 알림은 예전처럼 갓 올라온 매물이 먼저 나갑니다.
    new_items[alerts_start:] = reversed(new_items[alerts_start:])

    print(
        f"[{keyword}] 검색 {len(items)}개 확인 "
        f"(신규 {new_count}개, 재출품 {relist_count}개, 가격인하 {drop_count}개, "
        f"오래된 매물 {stale_count}개 조용히 기록"
        + (f", 판정 보류 {deferred_count}개" if deferred_count else "")
        + ")"
    )


def outage_bucket(down_for: float) -> int:
    """고장이 이어지는 동안 지금이 '몇 번째 쿨다운 구간'인지 돌려줍니다.

    예전에는 벽시계 절대 시각(now // COOLDOWN)으로 구간을 나눴습니다. 그러면 두 가지가 어긋납니다.

      1) 구간 경계(6시간 쿨다운이면 UTC 00/06/12/18시)를 넘는 순간, 고장이 이어지는
         중인데도 구간 번호가 바뀌어 알림이 한 번 더 나갑니다. 고장이 경계 직전에
         시작되면 1분 간격으로 두 건이 오는데, 이건 "6시간당 한 번"이 아닙니다.
      2) 알림 id가 벽시계에 묶여서, 같은 성질("고장이 이어지는 동안은 같은 id")을
         검사하는 테스트가 경계 앞뒤에서만 깨집니다. 워크플로는 테스트가 실패하면
         조회·전송 단계를 건너뛰므로, 실제로 2026-09-12 UTC 05:49~05:59에
         11회 연속 실패로 봇이 멈췄습니다(하루 네 번 반복될 수 있던 문제).

    고장이 시작된 시점부터 세면 둘 다 사라집니다. 첫 알림 이후 정확히 쿨다운
    간격마다 한 번씩만 나가고, id가 벽시계와 무관해집니다.
    """
    return int(max(down_for, 0.0) // HEALTH_ALERT_COOLDOWN_SECONDS)


def outage_was_announced(sent_alerts, alert_prefix: str) -> bool:
    """이 고장에 대해 경고가 실제로 나간 적이 있는지 봅니다.

    왜 필요한가:

    복구 알림은 "마지막 정상으로부터 HEALTH_ALERT_AFTER_SECONDS가 지났다"만으로 판단합니다.
    그런데 그 조건은 **고장이 났다 나았을 때**뿐 아니라 **봇이 아예 돌지 않았을 때**도 참입니다.
    실행이 없으면 경고를 보낼 주체도 없으므로, 그 경우에는 경고 없이 복구만 단독으로 나가
    "알린 적 없는 고장이 나았다"는 말이 됩니다.

    실측(2026-09-13): 보낸 복구 11건(recovered 4 / created-ok 4 / feed-ok 3) 전부가
    짝이 없었습니다. 같은 기간 search-down·created-missing·empty-feed는 **0건**입니다.
    러너 배정이 막혀 봇이 10분 넘게 못 도는 일이 하루 두어 번 있는데, 그때마다 한 번에
    세 건씩 나갔습니다.

    keyword_health_alerts는 이 문제를 이미 다르게 막고 있습니다 - 봇 전체가 멈췄던
    실행에서는 아예 아무것도 내보내지 않습니다(그 함수 독스트링 참고). 여기서는 같은
    생각을 나머지 가족에 적용합니다.

    경고와 복구의 alert_id는 같은 기준 시각(last_ok)을 공유하므로, 그 접두사로 보낸
    적이 있는지만 보면 됩니다. 경고는 고장이 이어지는 동안 쿨다운 구간(bucket)마다
    다른 id로 나가므로 접두사 일치로 찾습니다.

    보는 것은 **실제로 나간 것**뿐이고 아직 대기열에 있는 경고는 세지 않습니다. 그래서
    "경고를 만들었지만 전송이 막혀 못 보낸 사이에 복구된" 드문 경우에는 복구가 생략됩니다.
    일부러 그렇게 뒀습니다 - 그 상황에서 둘 다 내보내면 ⚠️와 ✅가 한 번에 도착합니다.
    """
    return any(isinstance(a, str) and a.startswith(alert_prefix) for a in sent_alerts)


def health_alerts(
    searched: list, keyword_checked_at: dict, now: float, sent_alerts=()
) -> list:
    """봇이 멈춘 것 같으면 알림을 만들고, 다시 살아나면 복구 알림을 만듭니다.

    이 봇은 평소에 조용한 게 정상이라, 고장이 나도 "새 매물이 없나 보다"와 구분되지 않습니다.
    그래서 검색이 한동안 한 건도 성공하지 못하면 그 사실 자체를 알립니다.

    알림 폭탄을 막는 장치가 두 겹입니다.
      - 한 번 실패했다고 바로 알리지 않고, 마지막 성공으로부터 일정 시간이 지나야 알립니다.
      - alert_id에 '고장이 시작된 뒤 몇 번째 쿨다운 구간인지'를 넣어, 고장이 이어져도
        그 구간당 한 번만 나갑니다
        (이미 보낸 알림을 거르는 기존 sent_alerts 장치가 그대로 적용됩니다).
    """
    if not searched:
        return []

    any_success = any(checked for _k, _i, checked, _c, _cut in searched)
    last_ok = keyword_checked_at.get(LAST_SEARCH_OK_KEY)
    last_ok = float(last_ok) if isinstance(last_ok, (int, float)) else None

    if any_success:
        keyword_checked_at[LAST_SEARCH_OK_KEY] = now
        # 한동안 죽어 있다가 살아난 경우에만, 그리고 그 고장을 실제로 알렸을 때에만
        # 복구를 알립니다(outage_was_announced 참고).
        if (
            last_ok is not None
            and now - last_ok >= HEALTH_ALERT_AFTER_SECONDS
            and outage_was_announced(sent_alerts, f"health:search-down:{int(last_ok)}:")
        ):
            minutes = int((now - last_ok) // 60)
            return [
                {
                    "alert_id": f"health:recovered:{int(last_ok)}",
                    "caption": f"✅ 메루카리 알림봇 정상 복구 (약 {minutes}분 만에 검색 재개)",
                    "photo": None,
                }
            ]
        return []

    # 이번 실행은 모든 키워드 검색이 실패했습니다.
    if last_ok is None:
        # 성공 기록이 없으면 기준이 없으므로 이번 실행을 기준으로 삼고 넘어갑니다.
        keyword_checked_at[LAST_SEARCH_OK_KEY] = now
        return []
    down_for = now - last_ok
    if down_for < HEALTH_ALERT_AFTER_SECONDS:
        return []

    minutes = int(down_for // 60)
    bucket = outage_bucket(down_for)
    return [
        {
            "alert_id": f"health:search-down:{int(last_ok)}:{bucket}",
            "caption": (
                f"⚠️ 메루카리 알림봇 이상\n"
                f"약 {minutes}분째 모든 키워드 검색이 실패하고 있습니다.\n"
                f"GitHub Actions 로그를 확인해 주세요."
            ),
            "photo": None,
        }
    ]


def created_coverage(searched: list) -> tuple[int, int]:
    """이번 조회에서 (등록 시각을 받아온 매물 수, 전체 매물 수)를 셉니다."""
    total = 0
    with_created = 0
    for _keyword, items, checked, _coverage, _cutoff in searched:
        if not checked:
            continue
        for fields in items:
            total += 1
            if listing_created_at(fields) is not None:
                with_created += 1
    return with_created, total


def created_coverage_alerts(
    searched: list, keyword_checked_at: dict, now: float, sent_alerts=()
) -> list:
    """매물 등록 시각을 받아오지 못하게 되면 알립니다.

    이 봇의 '오래된 매물을 신규로 오인하지 않는' 방어선은 등록 시각에 기대고 있습니다.
    메루카리 응답에서 이 필드가 사라지면 방어선이 조용히 예전 방식(상태 파일만 보고 판단)으로
    되돌아가고, 로그를 열어 보기 전에는 알 수 없습니다. 그 순간을 알리는 장치입니다.

    검색이 아예 실패해 표본이 없는 실행은 판단하지 않습니다(그 상황은 health_alerts가 다룹니다).
    한 번 어긋났다고 바로 알리지도 않습니다 — 다른 고장 알림과 같은 기준으로,
    일정 시간 이상 이어질 때만, 정해진 간격당 한 번만 내보냅니다.
    """
    with_created, total = created_coverage(searched)
    if total == 0:
        return []

    ratio = with_created / total
    last_ok = keyword_checked_at.get(LAST_CREATED_OK_KEY)
    last_ok = float(last_ok) if isinstance(last_ok, (int, float)) else None

    if ratio >= CREATED_COVERAGE_MIN_RATIO:
        keyword_checked_at[LAST_CREATED_OK_KEY] = now
        if (
            last_ok is not None
            and now - last_ok >= HEALTH_ALERT_AFTER_SECONDS
            and outage_was_announced(sent_alerts, f"health:created-missing:{int(last_ok)}:")
        ):
            minutes = int((now - last_ok) // 60)
            return [
                {
                    "alert_id": f"health:created-ok:{int(last_ok)}",
                    "caption": (
                        f"✅ 메루카리 알림봇 방어선 복구\n"
                        f"매물 등록 시각을 다시 정상적으로 받아옵니다 (약 {minutes}분 만에)."
                    ),
                    "photo": None,
                }
            ]
        return []

    if last_ok is None:
        # 기준이 없으면 이번 실행을 기준으로 삼고 넘어갑니다.
        keyword_checked_at[LAST_CREATED_OK_KEY] = now
        return []
    if now - last_ok < HEALTH_ALERT_AFTER_SECONDS:
        return []

    minutes = int((now - last_ok) // 60)
    bucket = outage_bucket(now - last_ok)
    return [
        {
            "alert_id": f"health:created-missing:{int(last_ok)}:{bucket}",
            "caption": (
                f"⚠️ 메루카리 알림봇 방어선 이상\n"
                f"매물 등록 시각을 {with_created}/{total}건({ratio:.0%})만 받아오고 있습니다 "
                f"(약 {minutes}분째).\n"
                f"'오래된 매물을 신규로 오인하지 않는' 장치가 약해진 상태라, "
                f"오래된 매물 알림이 늘 수 있습니다."
            ),
            "photo": None,
        }
    ]


def empty_feed_alerts(
    searched: list, keyword_checked_at: dict, now: float, sent_alerts=()
) -> list:
    """검색은 성공하는데 매물이 한 건도 오지 않는 상태를 알립니다.

    이게 왜 따로 필요하냐면, 기존 고장 감지가 전부 **검색 실패**를 기준으로 하기 때문입니다.
    search_items는 예외만 나지 않으면 succeeded=True이므로, 메루카리가 200으로 빈 결과를
    돌려주면 봇은 이렇게 판단합니다.

      - health_alerts: any_success가 참 -> 정상, __last_search_ok__도 전진
      - keyword_health_alerts: keyword_checked_at이 전진 -> 막힌 키워드 없음
      - created_coverage_alerts: 표본이 없으니(total == 0) 판단 보류

    즉 **모든 건강 신호가 초록인 채로 아무것도 찾지 못합니다.** 카테고리 ID가 바뀌거나
    검색 조건이 무효가 되면 실제로 이 모양이 됩니다. 평소 전체 조회가 2,800건씩 나오는
    봇에서 18개 키워드가 동시에 0건인 건 정상 변동이 아닙니다.

    전량 검색 실패는 여기서 다루지 않습니다(그 상황은 health_alerts가 알립니다).
    한 번 0건이라고 바로 알리지도 않습니다 — 다른 고장 알림과 같은 기준으로,
    일정 시간 이상 이어질 때만, 정해진 간격당 한 번만 내보냅니다.
    """
    if not any(checked for _k, _i, checked, _c, _cut in searched):
        return []

    _with_created, total = created_coverage(searched)
    last_ok = keyword_checked_at.get(LAST_ITEMS_OK_KEY)
    last_ok = float(last_ok) if isinstance(last_ok, (int, float)) else None

    if total > 0:
        keyword_checked_at[LAST_ITEMS_OK_KEY] = now
        if (
            last_ok is not None
            and now - last_ok >= HEALTH_ALERT_AFTER_SECONDS
            and outage_was_announced(sent_alerts, f"health:empty-feed:{int(last_ok)}:")
        ):
            minutes = int((now - last_ok) // 60)
            return [
                {
                    "alert_id": f"health:feed-ok:{int(last_ok)}",
                    "caption": (
                        f"✅ 메루카리 알림봇 검색 결과 복구\n"
                        f"다시 매물을 받아옵니다 (약 {minutes}분 만에)."
                    ),
                    "photo": None,
                }
            ]
        return []

    if last_ok is None:
        # 기준이 없으면 이번 실행을 기준으로 삼고 넘어갑니다.
        keyword_checked_at[LAST_ITEMS_OK_KEY] = now
        return []
    if now - last_ok < HEALTH_ALERT_AFTER_SECONDS:
        return []

    minutes = int((now - last_ok) // 60)
    bucket = outage_bucket(now - last_ok)
    return [
        {
            "alert_id": f"health:empty-feed:{int(last_ok)}:{bucket}",
            "caption": (
                f"⚠️ 메루카리 알림봇 검색 결과 없음\n"
                f"검색은 성공하는데 매물이 약 {minutes}분째 한 건도 오지 않습니다.\n"
                f"카테고리 ID나 검색 조건이 무효가 됐을 수 있습니다 — "
                f"그동안 새 매물 알림은 나가지 않습니다."
            ),
            "photo": None,
        }
    ]


def cadence_alerts(previous_checked_at: dict, keyword_checked_at: dict, now: float) -> list:
    """실행 주기가 기대보다 느려진 상태를 알립니다.

    1분 주기는 저장소 밖의 cron 서비스가 만듭니다(EXPECTED_RUN_INTERVAL_SECONDS 주석 참고).
    그게 멈춰도 봇은 워크플로의 5분 스케줄로 계속 돌기 때문에 **실행은 전부 성공하고
    검색도 정상**입니다. 기존 고장 알림은 하나도 울리지 않고, 새 매물 알림만 최대 5분
    늦어집니다. 백업 스케줄이 저하를 가려 주는 셈입니다.

    직전 실행과의 간격은 __last_search_ok__로 알 수 있습니다(이번 실행이 그 값을 덮어쓰기
    전의 스냅샷을 씁니다). 한두 번 벌어지는 건 정상이므로 — 실행이 겹쳐 취소되는 일이
    실측 약 6% 있습니다 — 벌어진 상태가 이어질 때만 알립니다.
    """
    previous = previous_checked_at.get(LAST_SEARCH_OK_KEY)
    if not isinstance(previous, (int, float)):
        return []  # 기준선이 없는 첫 실행입니다.

    gap = now - float(previous)
    last_ok = keyword_checked_at.get(LAST_CADENCE_OK_KEY)
    last_ok = float(last_ok) if isinstance(last_ok, (int, float)) else None

    if gap <= EXPECTED_RUN_INTERVAL_SECONDS * RUN_INTERVAL_SLACK:
        keyword_checked_at[LAST_CADENCE_OK_KEY] = now
        if last_ok is not None and now - last_ok >= HEALTH_ALERT_AFTER_SECONDS:
            minutes = int((now - last_ok) // 60)
            return [
                {
                    "alert_id": f"health:cadence-ok:{int(last_ok)}",
                    "caption": (
                        f"✅ 메루카리 알림봇 실행 주기 복구\n"
                        f"다시 약 {int(EXPECTED_RUN_INTERVAL_SECONDS // 60)}분 주기로 돕니다 "
                        f"(약 {minutes}분 만에)."
                    ),
                    "photo": None,
                }
            ]
        return []

    if last_ok is None:
        keyword_checked_at[LAST_CADENCE_OK_KEY] = now
        return []
    if now - last_ok < HEALTH_ALERT_AFTER_SECONDS:
        return []

    bucket = outage_bucket(now - last_ok)
    return [
        {
            "alert_id": f"health:cadence-slow:{int(last_ok)}:{bucket}",
            "caption": (
                f"⚠️ 메루카리 알림봇 실행 주기 저하\n"
                f"실행 간격이 약 {int(gap // 60)}분으로 벌어졌습니다 "
                f"(기대 {int(EXPECTED_RUN_INTERVAL_SECONDS // 60)}분).\n"
                f"1분 주기를 만드는 외부 cron이 멈췄을 수 있습니다 — "
                f"봇은 계속 돌지만 새 매물 알림이 그만큼 늦어집니다."
            ),
            "photo": None,
        }
    ]


def keyword_health_alerts(searched: list, previous_checked_at: dict, now: float) -> list:
    """키워드 하나가 오래 막혀 있으면 알립니다.

    전량 실패는 health_alerts가 따로 다룹니다. 여기서 잡으려는 건
    "다른 키워드는 멀쩡한데 이 키워드만 계속 실패"하는 상황입니다.
    그대로 두면 그 키워드 알림만 조용히 멈추고, 로그를 열어 보기 전에는 알 수 없습니다.

    판정 기준은 keyword_checked_at입니다. 이 값은 새 매물을 책임지는 '등록순' 조회가
    성공했을 때만 전진하므로, 오래 멈춰 있다는 건 그 키워드의 새 매물을 못 보고 있다는 뜻입니다.

    봇 전체가 죽은 실행에서는 아무것도 내보내지 않습니다. 그런 실행에서 키워드마다
    알림을 만들면 한 번에 17건이 쏟아지기 때문입니다(그 상황은 health_alerts가 한 건으로 알립니다).

    봇 전체가 한동안 멈춰 있다가 살아난 실행에서도 마찬가지입니다. 그때는 모든 키워드가
    동시에 '오래 막혀 있었다'가 되기 때문에, 막아 두지 않으면 복구되는 순간
    "✅ [키워드] 검색 재개"가 키워드 수만큼(지금은 18건) 한꺼번에 쏟아집니다.
    같은 소식을 health_alerts가 이미 "✅ 정상 복구" 한 건으로 알리므로 전부 군더더기입니다.
    여기서 다루려는 건 어디까지나 '다른 키워드는 멀쩡한데 이 키워드만' 막힌 경우입니다.
    """
    alive = any(coverage for _k, _i, _c, coverage, _cut in searched)
    if not alive:
        return []

    # previous_checked_at은 이번 실행 '전'의 값이라, 여기 담긴 마지막 검색 성공 시각이
    # 곧 봇 전체가 얼마나 멈춰 있었는지입니다.
    last_search_ok = previous_checked_at.get(LAST_SEARCH_OK_KEY)
    if (
        isinstance(last_search_ok, (int, float))
        and now - float(last_search_ok) >= KEYWORD_STUCK_AFTER_SECONDS
    ):
        return []

    alerts = []
    for keyword, _items, _checked, coverage, _cutoff in searched:
        last_ok = previous_checked_at.get(keyword)
        if not isinstance(last_ok, (int, float)):
            continue  # 기준선이 없는 키워드(첫 조회)는 판단하지 않습니다.
        stuck_for = now - float(last_ok)
        if stuck_for < KEYWORD_STUCK_AFTER_SECONDS:
            continue

        minutes = int(stuck_for // 60)
        if coverage:
            alerts.append(
                {
                    "alert_id": f"health:keyword-up:{keyword}:{int(last_ok)}",
                    "caption": f"✅ [{keyword}] 검색 재개 (약 {minutes}분 만에 정상)",
                    "photo": None,
                }
            )
        else:
            bucket = outage_bucket(stuck_for)
            alerts.append(
                {
                    "alert_id": f"health:keyword-down:{keyword}:{int(last_ok)}:{bucket}",
                    "caption": (
                        f"⚠️ [{keyword}] 검색이 약 {minutes}분째 실패하고 있습니다.\n"
                        f"다른 키워드는 정상이라 이 키워드 알림만 멈춘 상태입니다."
                    ),
                    "photo": None,
                }
            )
    return alerts


def report_feed_health(searched: list) -> None:
    """메루카리 응답이 기대대로 오는지 실행마다 한 줄로 요약합니다.

    '오래된 매물을 신규로 오인하지 않는' 방어선은 매물의 등록 시각(created)에 기대고 있는데,
    이 필드는 응답에 따라 비어 있을 수 있습니다. 비어 있으면 조용히 예전 방식(상태 파일만
    보고 판단)으로 되돌아가기 때문에, 눈치채지 못한 채 지나가지 않도록 로그를 남깁니다.
    """
    with_created, total = created_coverage(searched)
    unknown_seller = sum(
        1
        for _k, items, checked, _c, _cut in searched
        if checked
        for fields in items
        if not extract_seller_id(fields)
    )

    failed = [keyword for keyword, _items, checked, _c, _cut in searched if not checked]
    if failed:
        print(f"[점검] 조회 실패한 키워드 {len(failed)}개: {', '.join(failed)}", file=sys.stderr)
    if not total:
        print("[점검] 이번 실행에서 확인한 매물이 없습니다", file=sys.stderr)
        return

    print(
        f"[점검] 매물 {total}개 확인 / 등록시각 있음 {with_created}개 / 판매자ID 모름 {unknown_seller}개"
    )
    if not with_created:
        print(
            "[경고] 등록 시각(created)이 하나도 채워지지 않았습니다. "
            "'오래된 매물을 신규로 오인하지 않는' 방어선이 상태 파일 기준으로만 동작합니다.",
            file=sys.stderr,
        )


def forget_removed_keywords(known_keywords: set, keyword_checked_at: dict) -> None:
    """SEARCHES에서 빠진 키워드의 기록을 지웁니다.

    두 가지를 막습니다.
      - 상태 파일이 조용히 커지는 것. 지우지 않으면 한 번 쓴 키워드의 조회 시각이
        영영 남습니다(다른 항목들과 달리 여기에는 용량 상한이 없습니다).
      - 키워드를 지웠다가 나중에 되살릴 때의 알림 폭탄. 예전 조회 시각이 남아 있으면
        그 키워드는 '첫 조회'로 취급되지 않아서, 기준선만 저장하고 넘어가는 장치가
        동작하지 않습니다. 그러면 되살린 순간 최대 MAX_LOOKBACK_SECONDS(24시간)치
        매물이 한꺼번에 신규 알림으로 쏟아집니다.
    """
    configured = {search["query"] for search in SEARCHES}
    removed = sorted(
        (known_keywords | set(keyword_checked_at))
        - configured
        - set(RESERVED_STATE_KEYS)
    )
    if not removed:
        return
    for keyword in removed:
        known_keywords.discard(keyword)
        keyword_checked_at.pop(keyword, None)
    print(f"[정리] SEARCHES에 없는 키워드 {len(removed)}개의 기록을 지웠습니다: {', '.join(removed)}")


async def collect_updates() -> None:
    mercari = Mercapi()
    seen, pending, sent_alerts, relist_fingerprints, known_keywords, keyword_checked_at = load_state()
    forget_removed_keywords(known_keywords, keyword_checked_at)
    is_first_run = len(seen) == 0
    new_items: list = []
    now = current_time()
    # 키워드별 조회 시각은 아래 루프에서 갱신되므로, 판정에 쓸 '갱신 전' 값을 미리 떠 둡니다.
    previous_checked_at = dict(keyword_checked_at)

    # 짧은 주기(예: 1분)로 돌릴 때, 매번 추천순까지 조회하면 실행이 주기를 넘겨
    # 트리거가 버려지고 메루카리 API 호출량만 두 배가 됩니다.
    # 새 매물 탐지는 등록순만으로 충분하므로, 추천순(가격 인하 추적)은 일정 간격으로만 봅니다.
    last_full_scan = keyword_checked_at.get(FULL_SCAN_STATE_KEY)
    full_scan = (
        not isinstance(last_full_scan, (int, float))
        or now - float(last_full_scan) >= FULL_SCAN_INTERVAL_SECONDS
    )
    sort_passes = None if full_scan else [NEW_ITEM_SORT_PASS]
    print("전체 조회(등록순+추천순)" if full_scan else "빠른 조회(등록순만)")

    # 1단계: 모든 키워드를 먼저 조회합니다.
    # 판정을 뒤로 미루는 이유는, 재출품 여부를 판단할 때 '이번 실행에서 살아 있는 것이
    # 확인된 매물' 전체를 봐야 별개의 매물을 재출품으로 오인하지 않기 때문입니다.
    searched: list[tuple[str, list, bool, bool, float]] = []
    for index, search in enumerate(SEARCHES):
        if index:
            await asyncio.sleep(1)
        keyword = search["query"]
        cutoff = new_item_cutoff(keyword_checked_at, keyword, now)
        items, checked, coverage = await search_items(
            mercari, keyword, search["categories"], cutoff, sort_passes
        )
        searched.append((keyword, items, checked, coverage, cutoff))

    # 검색에 잡혔다는 것 자체가 '지금 살아 있다'는 증거이므로, 부분적으로만 성공한
    # 키워드의 결과도 재출품 판정용 목록에는 넣습니다.
    listed_ids: set[str] = set()
    for _keyword, items, checked, _coverage, _cutoff in searched:
        if checked:
            listed_ids |= listed_item_ids(items)

    report_feed_health(searched)

    # 2단계: 모아 둔 결과로 알림을 판정합니다.
    for keyword, items, checked, coverage, cutoff in searched:
        keyword_is_new = keyword not in known_keywords
        # 조회 시각 기록이 아직 없는 키워드는 '신규' 판정의 기준선이 없는 상태입니다.
        # 새로 추가한 키워드일 수도 있고, 이 기능을 배포한 직후의 첫 실행일 수도 있습니다.
        # 어느 쪽이든 이번 조회분은 기준선만 저장하고 넘어가야 알림 폭탄을 피할 수 있습니다.
        baseline_only = keyword_is_new or keyword not in keyword_checked_at
        keyword_items: list = []
        if checked:
            process_items(
                keyword,
                items,
                seen,
                relist_fingerprints,
                keyword_items,
                created_cutoff=cutoff,
                listed_ids=listed_ids,
                can_resolve_relists=full_scan,
            )

        if baseline_only:
            # 기존에 이미 올라와 있던 매물이 전부 '신규'로 잡혀 알림 폭탄이 되는 걸 막기 위해,
            # 기준선(seen)만 저장하고 이번 조회분의 신규 알림은 보내지 않습니다.
            # 다음 조회부터는 정상적으로 알림이 옵니다. (가격 인하 알림은 이미 추적 중인
            # 매물에만 해당하므로 그대로 내보냅니다.)
            suppressed = sum(1 for e in keyword_items if e["alert_id"].startswith("new:"))
            if suppressed:
                reason = "새로 추가된 키워드" if keyword_is_new else "기준선 최초 기록"
                print(f"[{keyword}] {reason} 첫 조회: 매물 {suppressed}개 기준선만 저장, 알림 생략")
            keyword_items = [e for e in keyword_items if not e["alert_id"].startswith("new:")]
            if keyword_is_new and checked:
                known_keywords.add(keyword)

        new_items.extend(keyword_items)
        # 등록순 조회에 실패한 키워드는 시각을 갱신하지 않습니다. 갱신해 버리면 그 구간에
        # 올라온 매물을 다음 실행에서 '오래된 매물'로 보고 영영 건너뜁니다.
        if coverage:
            keyword_checked_at[keyword] = now
        elif checked:
            print(f"[{keyword}] 등록순 조회 실패 -> 조회 시각을 갱신하지 않고 다음 실행에서 다시 확인합니다")

    if full_scan and any(coverage for _k, _i, _c, coverage, _cut in searched):
        keyword_checked_at[FULL_SCAN_STATE_KEY] = now

    # 봇 고장/복구 알림은 매물 알림과 달리 첫 실행에서도 내보냅니다.
    # cadence_alerts는 health_alerts보다 **먼저** 불러야 합니다. health_alerts가
    # __last_search_ok__를 이번 실행 시각으로 덮어쓰기 때문에, 그 뒤에 부르면 직전
    # 실행과의 간격이 0이 되어 주기 저하를 영영 못 봅니다.
    #
    # cadence_alerts만 sent_alerts를 받지 않습니다. 이 가족은 '봇이 안 돌았다'를 실행
    # 간격으로 직접 감지하므로 멈춰 있던 경우에도 경고가 정상적으로 나가기 때문입니다
    # (실측 2026-09-13: 복구 4건 중 3건이 짝이 맞았고, 짝 없는 1건도 정당한 복구였습니다).
    # 나머지 세 가족은 봇이 멈추면 경고를 낼 주체가 없어 복구만 단독으로 나갑니다.
    warnings = cadence_alerts(previous_checked_at, keyword_checked_at, now)
    warnings += health_alerts(searched, keyword_checked_at, now, sent_alerts)
    warnings += keyword_health_alerts(searched, previous_checked_at, now)
    warnings += created_coverage_alerts(searched, keyword_checked_at, now, sent_alerts)
    warnings += empty_feed_alerts(searched, keyword_checked_at, now, sent_alerts)
    for entry in warnings:
        print(entry["caption"].splitlines()[0], file=sys.stderr)

    if is_first_run:
        print(f"첫 실행: 기존 매물 {len(seen)}개를 기준으로 저장했습니다 (알림 생략)")
        pending = deduplicate_pending(pending + warnings, sent_alerts)
    else:
        pending = deduplicate_pending(pending + new_items + warnings, sent_alerts)
        print(f"새 알림 {len(new_items)}건 발견 (저장될 대기열 {len(pending)}건)")

    save_state(seen, pending, sent_alerts, relist_fingerprints, known_keywords, keyword_checked_at)


async def send_pending() -> None:
    seen, pending, sent_alerts, relist_fingerprints, known_keywords, keyword_checked_at = load_state()
    pending = deduplicate_pending(pending, sent_alerts)

    if not pending:
        print("전송할 대기 알림이 없습니다")
        save_state(seen, pending, sent_alerts, relist_fingerprints, known_keywords, keyword_checked_at)
        return

    try:
        pending, sent_alerts = await flush_pending(
            seen, pending, sent_alerts, relist_fingerprints, known_keywords, keyword_checked_at
        )
    finally:
        # flush_pending이 SendBlocked로 끝난 경우에도 이 저장은 정확합니다.
        # sent_alerts는 제자리에서 갱신되므로 이미 보낸 건이 반영되고, pending은 호출 전
        # 값이라 이번 실행에서 건드린 순서 변경이 남지 않습니다. 그리고 save_state가
        # sent_alerts에 있는 항목을 대기열에서 걸러 내므로 재전송도 생기지 않습니다.
        save_state(seen, pending, sent_alerts, relist_fingerprints, known_keywords, keyword_checked_at)

    if pending:
        print(f"[대기열에 {len(pending)}건 남음 -> 다음 실행에 재시도]", file=sys.stderr)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Mercari 알림 수집/전송")
    parser.add_argument(
        "--mode",
        choices=("collect", "send"),
        default="collect",
        help="collect는 검색 결과를 저장하고, send는 저장된 대기열을 텔레그램으로 전송합니다.",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    if args.mode == "collect":
        await collect_updates()
        return
    try:
        await send_pending()
    except SendBlocked as exc:
        # 대기열과 전송 기록은 send_pending이 이미 저장했습니다. 여기서는 실행을
        # 실패로 끝내서, 조용히 지나가지 않게만 합니다(SendBlocked 주석 참고).
        print(f"[전송 단계 실패] {exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    asyncio.run(main())
