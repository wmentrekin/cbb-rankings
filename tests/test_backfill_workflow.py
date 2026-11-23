from pathlib import Path

import pytest

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "backfill-games.yml"


def test_workflow_text_contract():
    text = WORKFLOW.read_text()
    assert "workflow_dispatch" in text and "group: weekly-rankings" in text and "cancel-in-progress: false" in text
    assert "|| true" not in text
    for secret in ("CBBD_API_KEY", "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_D1_DATABASE_ID", "CLOUDFLARE_API_TOKEN"):
        assert f"secrets.{secret}" in text
    assert "scripts/backfill_games.py" in text


def test_workflow_yaml_parses_and_declares_inputs():
    yaml = pytest.importorskip("yaml")  # not a project dependency; parsed where available
    doc = yaml.safe_load(WORKFLOW.read_text())
    inputs = doc[True]["workflow_dispatch"]["inputs"]  # PyYAML reads the key `on` as True
    assert set(inputs) == {"season", "week", "apply", "max_rows_written"}
    assert inputs["season"]["required"] is True and inputs["apply"]["default"] is False
    assert doc["concurrency"] == {"group": "weekly-rankings", "cancel-in-progress": False}
