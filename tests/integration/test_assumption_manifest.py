from pathlib import Path

import pytest

from subsched.assumptions import (
    AssumptionDecision,
    AssumptionManifestError,
    load_manifest,
)

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "assumptions"


def test_replays_valid_manifest_fixture() -> None:
    manifest = load_manifest(FIXTURE_ROOT / "valid-manifest.json")

    assert manifest.date == "2026-08-13"
    assert len(manifest.records) == 1
    assert manifest.records[0].decision is AssumptionDecision.UNKNOWN


@pytest.mark.parametrize(
    "fixture_name",
    ("unknown-field.json", "unverified-pass.json", "unredacted-secret.json"),
)
def test_replay_rejects_invalid_manifest_fixtures(fixture_name: str) -> None:
    with pytest.raises(AssumptionManifestError):
        load_manifest(FIXTURE_ROOT / fixture_name)


def test_replay_rejects_missing_manifest(tmp_path: Path) -> None:
    with pytest.raises(AssumptionManifestError, match="cannot read"):
        load_manifest(tmp_path / "missing.json")
