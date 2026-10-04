# tests/test_adapters.py
"""Every adapter replayed against its recorded fixture must
1. parse without errors and pass validation (no "broken" days), and
2. produce exactly the golden summary in tests/fixtures/<campus>/expected.txt.

If a site change is intentional and the new output is correct, refresh the golden file:
    python -m hungrybear.devtools golden <campus>
"""

import pytest

from hungrybear.devtools import FIXTURES, replay_days, summarize

CAMPUSES = sorted(p.name for p in FIXTURES.iterdir() if (p / "meta.json").exists())


@pytest.mark.parametrize("campus", CAMPUSES)
def test_fixture_parses_and_validates(campus):
    days = replay_days(campus)
    assert days, "fixture has no days"
    for menu in days:
        assert menu.status != "broken", f"{menu.date}: {menu.reason}"
    assert any(menu.status == "ok" for menu in days), "fixture should include at least one day with menus"


@pytest.mark.parametrize("campus", CAMPUSES)
def test_fixture_matches_golden(campus):
    expected = (FIXTURES / campus / "expected.txt").read_text()
    actual = "".join(summarize(m) for m in replay_days(campus))
    assert actual == expected, f"output changed; if intended run: python -m hungrybear.devtools golden {campus}"
