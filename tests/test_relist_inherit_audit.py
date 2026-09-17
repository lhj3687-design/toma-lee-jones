"""`scripts/relist_inherit_audit.py`의 **눈금에 고장을 넣어 보는** 테스트입니다.

눈금을 넣어 둔 것만으로는 부족하다는 것을 이 저장소가 세 라운드 연속으로 겪었습니다
(2026-09-15 한 번, 09-17 여덟 번, 같은 날 세 번 더). 그래서 이 파일은 이 도구가
'못 잡는 도구'가 아닌지를 CI에서 계속 확인합니다 — **칸마다, 그리고 한 칸 안의
숫자마다** 고장을 넣습니다.

`--calibrate`의 '아는 답' 절반(운영 이력 7,338판 walk)은 여기서 안 돌립니다. 몇 분이
걸려 매 실행 유닛 테스트로는 못 씁니다. 여기서 도는 것은 `self_test()`이고, 그쪽이
고장을 잡는지를 봅니다.
"""
import contextlib
import re
import io
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import relist_inherit_audit as audit  # noqa: E402


class SelfTestPassesOnGoodCodeTests(unittest.TestCase):
    """고장을 안 넣었을 때는 통과해야 합니다 — 안 그러면 아래가 전부 의미가 없습니다."""

    def test_self_test_passes(self):
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            result = audit.self_test()
        self.assertTrue(result, printed.getvalue())
        self.assertNotIn("⛔", printed.getvalue())


class CalibrationCatchesFaultsTests(unittest.TestCase):
    """눈금에 **고장을 넣어 보고** 자체 점검이 실패로 끝나는지 봅니다."""

    def assert_caught(self, what: str):
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            result = audit.self_test()
        self.assertFalse(result, f"눈금이 '{what}' 고장을 그대로 통과시켰습니다")
        self.assertIn("⛔", printed.getvalue())

    def patch(self, name, value):
        original = getattr(audit, name)
        setattr(audit, name, value)
        self.addCleanup(setattr, audit, name, original)

    # --- 자리를 찾는 칸 (`sites`) -------------------------------------------------

    def test_ignoring_the_already_seen_guard_is_caught(self):
        """B가 이미 `seen`에 있던 자리까지 세면 '물려받음'이 아닌 것이 섞입니다."""
        original = audit.sites

        def loose(previous, current, ts=0.0, sha="", subject=""):
            stripped = dict(previous)
            stripped["seen"] = {k: v for k, v in (previous.get("seen") or {}).items()
                                if k != "m-new"}
            return original(stripped, current, ts, sha, subject)

        self.patch("sites", loose)
        self.assert_caught("B가 이미 seen 에 있던 자리")

    def test_ignoring_the_owner_change_guard_is_caught(self):
        """주인이 그대로인 자리까지 세면 재출품이 아닌 것이 섞입니다."""
        original = audit.sites

        def loose(previous, current, ts=0.0, sha="", subject=""):
            found = original(previous, current, ts, sha, subject)
            if found:
                return found
            seen = current.get("seen") or {}
            record = audit.price_record(seen, "m-new")
            return [{"ts": ts, "sha": sha, "fp": "seller:x:y", "old_id": "m-old",
                     "new_id": "m-new", "copy": 1, "origin": 2, "origin_present": True,
                     "new_alert": record["last_alert_price"],
                     "new_seen": record["last_seen_price"],
                     "drops": audit.item_drops(current, "m-new"),
                     "run_gone": audit.run_gone(subject), "run_sites": 1}]

        self.patch("sites", loose)
        self.assert_caught("주인이 안 바뀐 자리")

    # --- 가설이 서는지 보는 칸 (`predicted` / `consistent`) ------------------------

    def test_ignoring_the_price_drop_rule_is_caught(self):
        """문턱만큼 싸면 기준가가 관측가로 **내려갑니다.** 그 규칙을 빼면 예측이 틀립니다."""
        self.patch("predicted",
                   lambda baseline, price: ((baseline, price), None)
                   if isinstance(price, int) else (None, None))
        self.assert_caught("인하 규칙")

    def test_an_off_by_one_threshold_is_caught(self):
        """문턱은 `<=  기준가 - 1,000`입니다. `<`로 바꾸면 경계 판이 어긋납니다."""
        original = audit.PRICE_DROP_ALERT_THRESHOLD
        self.patch("PRICE_DROP_ALERT_THRESHOLD", original + 1)
        self.assert_caught("문턱 경계")

    def test_ignoring_the_drop_alert_is_caught(self):
        """알림 id 가 기준가를 들고 있습니다. 안 보면 두 가설이 안 갈립니다."""
        original = audit.consistent

        def blind(baseline, site):
            return original(baseline, dict(site, drops=set()))

        self.patch("consistent", blind)
        self.assert_caught("인하 알림")

    def test_allowing_an_alert_that_should_not_have_fired_is_caught(self):
        """'안 물려받음'은 인하 알림이 **안 나가야** 섭니다. 그걸 빼면 가설이 안 지워집니다."""
        def lenient(baseline, site):
            record, drop = audit.predicted(baseline, site["new_seen"])
            if record is None:
                return True
            if (site["new_alert"], site["new_seen"]) != record:
                return False
            return True if drop is None else drop in site["drops"]

        self.patch("consistent", lenient)
        self.assert_caught("나가면 안 될 알림")

    def test_reading_the_drop_target_instead_of_the_baseline_is_caught(self):
        """`drop:{매물}:{기준가}:{관측가}`에서 **앞 숫자**가 물려받은 기준가입니다."""
        original = audit.item_drops
        self.patch("item_drops",
                   lambda state, item_id: {(target, base)
                                           for base, target in original(state, item_id)})
        self.assert_caught("인하 알림의 기준가 자리")

    # --- 판정하는 칸 (`verdict`) --------------------------------------------------

    def test_calling_undecidable_sites_clean_is_caught(self):
        """'가를 수 없음'을 ✅로 세면 도구가 거짓말을 합니다 — 분모까지 부풉니다."""
        original = audit.verdict
        self.patch("verdict", lambda site: (audit.NOT_COPY
                                            if original(site) == audit.UNDECIDED
                                            else original(site)))
        self.assert_caught("가를 수 없음을 ✅로")

    def test_calling_unexplained_sites_clean_is_caught(self):
        """'설명이 안 됨'도 ✅가 아닙니다."""
        original = audit.verdict
        self.patch("verdict", lambda site: (audit.NOT_COPY
                                            if original(site) == audit.UNEXPLAINED
                                            else original(site)))
        self.assert_caught("설명이 안 됨을 ✅로")

    def test_counting_fresh_copies_in_the_denominator_is_caught(self):
        """사본이 안 낡은 자리를 분모에 넣으면 '어긋남 0건'이 부풀어 보입니다."""
        original = audit.verdict
        self.patch("verdict", lambda site: (audit.NOT_COPY
                                            if original(site) == audit.FRESH_COPY
                                            else original(site)))
        self.assert_caught("사본이 원본과 같은 자리를 분모에")

    def test_ignoring_the_missing_owner_is_caught(self):
        """주인이 `seen`에 없으면 고침이 닿지 않습니다. 섞으면 분모가 틀립니다."""
        original = audit.verdict
        self.patch("verdict", lambda site: (audit.FROM_COPY
                                            if original(site) == audit.NO_ORIGIN
                                            else original(site)))
        self.assert_caught("주인이 seen 에 없는 자리")

    # --- 방향을 가르는 칸 (`direction`) -------------------------------------------

    def test_reversing_the_direction_is_caught(self):
        """비쌈/쌈은 처방이 다릅니다. 뒤집히면 README의 '257건 중 1건'이 딴 값이 됩니다."""
        original = audit.direction
        flip = {"비쌈(지문>매물)": "쌈(지문<매물)", "쌈(지문<매물)": "비쌈(지문>매물)"}
        self.patch("direction", lambda site: flip.get(original(site), original(site)))
        self.assert_caught("방향")

    def test_collapsing_the_direction_is_caught(self):
        """방향을 아예 안 가르면 '3건'과 '1건'이 한 덩어리가 됩니다."""
        self.patch("direction", lambda site: "비쌈(지문>매물)")
        self.assert_caught("방향을 한 덩어리로")

    # --- 숫자마다의 자릿수 --------------------------------------------------------

    def test_rounding_prices_to_thousands_is_caught(self):
        """천 단위로 반올림해 비교하는 고장. 눈금 값이 전부 여섯 자리면 안 잡힙니다 —
        그래서 0 · 12 · 999 · 1,000 · 1,001 을 섞어 두었습니다."""
        original = audit.verdict

        def rounded(site):
            coarse = dict(site)
            for key in ("copy", "origin", "baseline"):
                value = site.get(key)
                if isinstance(value, int):
                    coarse[key] = round(value / 1000) * 1000
            return original(coarse)

        self.patch("verdict", rounded)
        self.assert_caught("천 단위 반올림")

    def test_truncating_prices_to_five_digits_is_caught(self):
        """여섯 자리 이상을 자르는 고장. 215,000 과 200,000 이 같은 값이 됩니다."""
        original = audit.verdict

        def truncated(site):
            cut = dict(site)
            for key in ("copy", "origin", "baseline"):
                value = site.get(key)
                if isinstance(value, int):
                    cut[key] = value % 100000
            return original(cut)

        self.patch("verdict", truncated)
        self.assert_caught("여섯 자리 자르기")

    # --- 꼬리표를 읽는 칸 (`run_gone`) ---------------------------------------------

    def test_a_single_digit_gone_count_is_caught(self):
        """`없음12`가 `없음1`로 세지는 고장. **자릿수는 숫자마다** 섞어야 잡힙니다 —
        이 저장소가 세 라운드 연속으로 데인 그 자리입니다."""
        self.patch("RUN_GONE_PATTERN",
                   re.compile(r"재출품 \d+(?:/\d+)?: 있음\d+/없음(?P<gone>\d)"))
        self.assert_caught("없음 자릿수")

    def test_pinning_when_several_sites_share_the_run_is_caught(self):
        """그 판에 자리가 여럿이면 어느 자리가 물려받았는지 못 고릅니다."""
        self.patch("pinned", lambda site: (site.get("run_gone") or 0) >= 1)
        self.assert_caught("자리가 여럿인 판을 못 박음")

    def test_reading_a_missing_tag_as_zero_is_caught(self):
        """꼬리표가 없는 판의 '없음'은 **0이 아니라 '측정이 없음'**입니다."""
        original = audit.run_gone
        self.patch("run_gone", lambda subject: original(subject) or 0)
        self.assert_caught("꼬리표 없는 판을 0으로")

    def test_reading_the_asked_count_instead_of_gone_is_caught(self):
        """`재출품 N/M: 있음A/없음G`에서 읽을 값은 **G**입니다."""
        self.patch("RUN_GONE_PATTERN",
                   re.compile(r"재출품 (?P<gone>\d+)(?:/\d+)?: 있음\d+/없음\d+"))
        self.assert_caught("꼬리표에서 읽는 자리")

    def test_letting_the_tag_overrule_a_decided_site_is_caught(self):
        """꼬리표는 **못 가른 자리**만 가릅니다. 이미 가려진 자리를 덮으면 안 됩니다."""
        original = audit.verdict

        def overruled(site):
            got = original(site)
            return audit.NOT_COPY if site.get("run_gone") == 0 else got

        self.patch("verdict", overruled)
        self.assert_caught("꼬리표가 가려진 자리를 덮음")

    # --- 층을 가르는 칸 (`stale_sites`) -------------------------------------------

    def test_merging_the_layers_is_caught(self):
        """2층(사본이 낡은 자리)을 1층으로 뭉개면 분모가 통째로 틀립니다."""
        self.patch("stale_sites", lambda found: list(found))
        self.assert_caught("2층을 1층으로")

    def test_an_empty_second_layer_is_caught(self):
        """반대로 2층을 비우면 '분모 0 = 아직 못 잼'이 언제나 나옵니다."""
        self.patch("stale_sites", lambda found: [])
        self.assert_caught("2층을 비움")


class LayerCountsAreDistinctTests(unittest.TestCase):
    """눈금 판이 모자라면 눈금이 못 지킵니다(판이 넷이면 p90과 최대가 같은 값).

    여기서는 **세 층의 값이 서로 달라야** 층을 뭉개는 고장이 잡힙니다.
    """

    def test_three_layers_have_three_different_values(self):
        everything = []
        for _name, (previous, current, subject), _v, _d in audit.SELF_TEST_CASES:
            for site in audit.sites(previous, current, 1000.0, "", subject):
                everything.append(audit.classify(site))
        stale = audit.stale_sites(everything)
        decided = [s for s in stale if audit.verdict(s) in audit.DECIDED]
        layers = (len(everything), len(stale), len(decided))
        self.assertEqual(len(set(layers)), 3, f"층이 같은 값이면 뭉개도 안 잡힙니다: {layers}")


if __name__ == "__main__":
    unittest.main()
