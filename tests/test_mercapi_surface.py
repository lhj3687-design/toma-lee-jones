"""봇이 기대는 mercapi의 표면이 그대로인지 확인합니다.

왜 별도 파일이고 왜 서브프로세스인가:

`tests/test_check_mercari.py`는 mercapi를 **스텁으로 바꿔치기**한 뒤 봇을 import합니다
(라이브러리 없이도 판정 로직을 검증하기 위해서입니다). 그래서 그 파일이 먼저 돌고 나면
`sys.modules["mercapi"]`는 가짜이고, 같은 프로세스에서는 진짜 라이브러리를 볼 수 없습니다.

그 결과 **테스트 전체가 초록이어도 mercapi 호환성은 하나도 검증되지 않습니다.**
Dependabot이 mercapi를 올린 PR도 그대로 초록불이 됩니다 — 심볼이 옮겨졌더라도요.
그 구멍을 메우려고, 깨끗한 인터프리터를 따로 띄워 진짜 라이브러리를 확인합니다.

무엇을 확인하는가:

  1. 정렬·상태 상수 (build_search_options가 쓰는 것)
  2. Mercapi.search()가 받는 인자 이름
  3. 검색 결과 항목의 필드 이름 (item_fields가 읽는 것)

1번이 특히 중요합니다. `build_search_options()`는 import에 실패하면 **조용히 기본
검색으로 물러납니다.** 이름이 바뀌어도 봇은 죽지 않고 정렬 없이 돌기 때문에(stderr 로그만
남습니다), 깨져도 눈에 잘 띄지 않습니다.
"""
import subprocess
import sys
import unittest

# 깨끗한 인터프리터에서 돌 검사 본문입니다. 봇이 실제로 참조하는 것만 봅니다.
PROBE = r"""
import inspect, sys

from mercapi import Mercapi
from mercapi.requests import SearchRequestData as request_data

missing = []

# 1) build_search_options()가 쓰는 상수
for path in ("Status.STATUS_ON_SALE", "SortBy.SORT_CREATED_TIME",
             "SortBy.SORT_SCORE", "SortOrder.ORDER_DESC"):
    target = request_data
    for part in path.split("."):
        target = getattr(target, part, None)
        if target is None:
            missing.append(f"SearchRequestData.{path}")
            break

# 2) search_items()가 넘기는 인자
parameters = inspect.signature(Mercapi.search).parameters
for name in ("categories", "sort_by", "sort_order", "status"):
    if name not in parameters:
        missing.append(f"Mercapi.search(..., {name}=)")

# 3) item_fields()가 읽는 필드
from mercapi.models.search import SearchResultItem
annotations = getattr(SearchResultItem, "__annotations__", {})
for field in ("id_", "name", "price", "created", "thumbnails", "item_type", "seller_id"):
    if field not in annotations and not hasattr(SearchResultItem, field):
        missing.append(f"SearchResultItem.{field}")

if missing:
    print("\n".join(missing))
    sys.exit(1)
"""

# 라이브러리를 아예 불러올 수 없는 환경(의존성 미설치)에서는 건너뜁니다.
_AVAILABLE = subprocess.run(
    [sys.executable, "-c", "import mercapi"], capture_output=True
).returncode == 0


@unittest.skipUnless(_AVAILABLE, "mercapi를 불러올 수 없는 환경")
class MercapiSurfaceTests(unittest.TestCase):
    def test_the_bot_still_finds_everything_it_uses_in_mercapi(self):
        result = subprocess.run(
            [sys.executable, "-c", PROBE], capture_output=True, text=True
        )

        self.assertEqual(
            result.returncode,
            0,
            "mercapi에서 봇이 쓰는 것이 사라졌습니다:\n"
            + result.stdout
            + result.stderr,
        )


if __name__ == "__main__":
    unittest.main()
