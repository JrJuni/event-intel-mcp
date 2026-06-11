"""G4 — news_capture advisory report (ZNC criterion ⑤)."""
from __future__ import annotations

from typer.testing import CliRunner

from event_intel.cli import app
from event_intel.eval.news_capture import news_capture_report

runner = CliRunner()


def _payload():
    def ex(name, bodied, listed):
        return {
            "name": name, "news_count": listed,
            "news_relatedness": [
                {"url": f"https://n/{name}/{i}", "relatedness": 0.5, "body_chars": 900}
                for i in range(bodied)
            ],
        }

    return {"exhibitors": [
        ex("BigCo Met", 11, 12),      # >=10M, 11 bodied → met
        ex("BigCo Miss", 7, 12),      # >=10M, 7 bodied → not met
        ex("SmallCo Met", 3, 5),      # <10M, 3 bodied → met
        ex("SmallCo Miss", 1, 4),     # <10M → not met
        ex("Mystery Co", 9, 9),       # no revenue judgment → unknown
    ]}


TIERS = {"BigCo Met": True, "BigCo Miss": True,
         "SmallCo Met": False, "SmallCo Miss": False}


def test_report_thresholds_by_revenue_tier():
    r = news_capture_report(_payload(), TIERS)
    assert r["grade"] == "advisory"
    by_name = {c["name"]: c for c in r["companies"]}
    assert by_name["BigCo Met"]["met"] is True and by_name["BigCo Met"]["threshold"] == 10
    assert by_name["BigCo Miss"]["met"] is False
    assert by_name["SmallCo Met"]["met"] is True and by_name["SmallCo Met"]["threshold"] == 3
    assert by_name["Mystery Co"]["met"] is None and by_name["Mystery Co"]["revenue_tier"] is None
    s = r["summary"]
    assert s["big"] == {"total": 2, "met": 1, "met_rate": 0.5}
    assert s["small"] == {"total": 2, "met": 1, "met_rate": 0.5}
    assert s["unknown_tier"] == 1
    assert s["overall_met_rate"] == 0.5  # unknowns excluded from denominators


def test_bodied_count_drives_met_not_listed():
    """Criterion ⑤ counts BODY-CRAWLED articles (news_relatedness entries),
    not the snippet-level news_count."""
    payload = {"exhibitors": [{
        "name": "Snippety", "news_count": 12, "news_relatedness": [],
    }]}
    r = news_capture_report(payload, {"Snippety": True})
    assert r["companies"][0]["bodied_news"] == 0
    assert r["companies"][0]["met"] is False


def test_empty_payload_graceful():
    r = news_capture_report({"exhibitors": []}, {})
    assert r["summary"]["total"] == 0
    assert r["summary"]["overall_met_rate"] is None


def test_measure_cli_requires_revenue_tiers_with_tier_list(tmp_path):
    res = runner.invoke(app, [
        "benchmark", "measure", "--run-dir", str(tmp_path),
        "--roster", "x.json", "--sealed-labels", "y.json",
        "--tier-list", "z.yaml",
    ])
    assert res.exit_code == 2
    assert "revenue-tiers" in res.output
