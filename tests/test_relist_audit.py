"""scripts/relist_audit.py의 판정 논리를 검증합니다.

이 스크립트는 "재출품 판정 68건 중 34건이 오판"처럼 **결론이 되는 숫자**를 내는
자리입니다. 세는 법이 한 군데 틀리면 숫자가 통째로 달라지고, 그 숫자로 다음 라운드의
작업이 정해집니다. 실제로 이번에 두 번 틀렸습니다.

  - 값(`last_alert_price != last_seen_price`)만 보면 68건이 아니라 **49건**이 나옵니다.
    첫 관측에서 곧바로 인하 알림이 나간 19건은 기준가가 관측가로 내려가면서 두 값이
    같아지기 때문입니다.
  - 되돌아온 것을 **같은 지문에서만** 찾으면 놓칩니다. `title:` 지문은 키에 가격이
    박혀 있어, 매물 값이 바뀌면 키 자체가 달라집니다.
"""
import importlib
import sys
import types
import unittest
from pathlib import Path

mercapi_stub = types.ModuleType("mercapi")
mercapi_stub.Mercapi = object
sys.modules.setdefault("mercapi", mercapi_stub)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
relist_audit = importlib.import_module("relist_audit")


def event(**overrides) -> dict:
    base = {"ts": 1000.0, "fp": "seller:1:제목", "old_id": "예전", "new_id": "새것",
            "old_fp_alert": 50000, "new_alert": 50000, "new_seen": 50000}
    base.update(overrides)
    return base


class InheritanceEvidenceTests(unittest.TestCase):
    def test_two_different_prices_mean_the_baseline_came_from_somewhere_else(self):
        """새로 기록하는 매물은 두 값이 같습니다 — 다르면 물려받은 것입니다."""
        self.assertEqual(
            relist_audit.inheritance_evidence(event(new_alert=50000, new_seen=61000), {}),
            "값",
        )

    def test_a_drop_alert_at_first_sight_proves_an_inherited_baseline(self):
        """이 경우를 빠뜨리면 68건이 49건이 됩니다.

        기준가가 없는 매물은 인하 알림을 보낼 수 없습니다. 첫 관측과 같은 실행에서
        인하 알림이 나갔다면 기준가를 물려받았다는 뜻입니다. 그런데 알림을 보내면서
        기준가가 관측가로 내려가므로, 값만 보면 새 기록과 구분되지 않습니다.
        """
        drops = {"새것": [(96300, 47500, 1000.0)]}
        self.assertEqual(
            relist_audit.inheritance_evidence(
                event(new_alert=47500, new_seen=47500), drops),
            "인하알림",
        )

    def test_the_drop_alert_baseline_does_not_have_to_match_the_last_seen_owner(self):
        """한 실행이 같은 지문을 여러 번 덮어쓰면 직전 판의 값과 어긋납니다.

        `process_items()`는 한 실행에서 같은 지문에 걸리는 매물을 여러 개 처리할 수
        있고, 나중 것이 앞의 것을 덮어씁니다. 그래서 물려받은 기준가가 '직전 커밋의
        지문 값'과 다를 수 있습니다. 기준가가 일치해야 한다고 요구하면 이걸 놓칩니다.
        """
        drops = {"새것": [(96300, 47500, 1000.0)]}
        self.assertEqual(
            relist_audit.inheritance_evidence(
                event(old_fp_alert=48600, new_alert=47500, new_seen=47500), drops),
            "인하알림",
        )

    def test_a_later_price_drop_is_not_evidence_of_inheritance(self):
        """한참 뒤의 인하는 그냥 추적 중인 매물이 값을 내린 것입니다."""
        drops = {"새것": [(50000, 40000, 1000.0 + 3600)]}
        self.assertIsNone(
            relist_audit.inheritance_evidence(event(new_alert=50000, new_seen=50000), drops)
        )

    def test_a_separate_item_at_its_own_price_is_not_inherited(self):
        self.assertIsNone(relist_audit.inheritance_evidence(event(), {}))


class ReturnDetectionTests(unittest.TestCase):
    def test_the_return_is_looked_for_across_every_fingerprint(self):
        """`title:` 지문은 값이 바뀌면 키가 달라집니다 — 같은 지문만 보면 놓칩니다."""
        owner_became = {"예전": [500.0, 1500.0, 2500.0]}
        self.assertEqual(relist_audit.first_return("예전", 1000.0, owner_became), 1500.0)

    def test_an_item_that_never_comes_back_has_no_return(self):
        self.assertIsNone(relist_audit.first_return("예전", 1000.0, {"예전": [500.0]}))
        self.assertIsNone(relist_audit.first_return("처음보는", 1000.0, {}))


class AlertParsingTests(unittest.TestCase):
    def test_only_well_formed_drop_alerts_are_read(self):
        alerts = {
            "drop:매물A:50000:40000": 10.0,
            "new:매물B": 20.0,                 # 신규 알림은 기준가가 없습니다
            "drop:망가진것": 30.0,              # 자리가 모자랍니다
            "drop:매물C:숫자아님:40000": 40.0,   # 값이 숫자가 아닙니다
        }
        drops = relist_audit.parse_drop_alerts(alerts)
        self.assertEqual(dict(drops), {"매물A": [(50000, 40000, 10.0)]})
