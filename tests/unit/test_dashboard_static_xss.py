from __future__ import annotations

import re
from importlib import resources


def _index_html() -> str:
    return resources.files("subsched.dashboard").joinpath("static/index.html").read_text(
        encoding="utf-8"
    )


def test_spa_never_assigns_dynamic_data_to_inner_html() -> None:
    """Regression guard for the stored-XSS finding in issue #443's review: the SPA
    must not interpolate server-provided data (task titles, handoffs, etc.) into
    `innerHTML`. The only permitted `innerHTML` assignment is clearing a container
    with a literal empty string; everything else must use `textContent` / DOM APIs."""
    html = _index_html()
    assignments = re.findall(r'innerHTML\s*=\s*([^;]+);', html)
    assert assignments, "expected to find at least the container-clearing assignment"
    for rhs in assignments:
        assert rhs.strip() == '""', f"unexpected innerHTML assignment: {rhs!r}"
