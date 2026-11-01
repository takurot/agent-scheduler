from __future__ import annotations

import json
from pathlib import Path

import pytest

from subsched.audit_producer import parse_input, write_record


def test_parse_only_accepts_bounded_data_inputs() -> None:
    target, packages = parse_input(
        json.dumps({"commit": "a" * 40}),
        '[{"name":"Example_pkg","version":"1.2"}]',
    )
    assert target == {"commit": "a" * 40}
    assert packages == [("Example_pkg", "1.2")]


@pytest.mark.parametrize(
    "inventory",
    [
        "[]",
        '[{"name":"x","version":"1","extra":"bad"}]',
        '[{"name":"x","name":"y","version":"1"}]',
        '[{"name":"a;echo","version":"1"}]',
        "x" * 65537,
    ],
)
def test_parse_rejects_malformed_or_unsafe_inventory(inventory: str) -> None:
    with pytest.raises(ValueError):
        parse_input('{"commit":"a"}', inventory)


def test_write_record_refuses_to_overwrite_or_follow_symlinks(tmp_path: Path) -> None:
    output = tmp_path / "audit.json"
    output.write_text("preserve")
    with pytest.raises(FileExistsError):
        write_record(output, {"schema": 1})
    assert output.read_text() == "preserve"
    link = tmp_path / "link"
    link.symlink_to(output)
    with pytest.raises((ValueError, FileExistsError)):
        write_record(link, {"schema": 1})
