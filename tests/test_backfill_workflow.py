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


WORKFLOWS = sorted(WORKFLOW.parent.glob("*.yml"))


def test_there_are_three_workflows():
    assert [w.name for w in WORKFLOWS] == ["apply-migrations.yml", "backfill-games.yml", "tests.yml"]


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_workflow_is_least_privilege_and_frozen(path):
    text = path.read_text()
    assert "\npermissions:\n  contents: read\n" in text
    assert "uv sync\n" not in text and "uv run python" not in text and "uv run pytest" not in text  # always --frozen
    assert "npx wrangler " not in text  # pinned to a major: npx wrangler@4
    for action in ("actions/checkout", "actions/setup-python", "actions/setup-node", "astral-sh/setup-uv"):
        assert f"uses: {action}@v7" in text or action not in text  # majors verified with git ls-remote


def test_migrations_workflow_pins_wrangler_and_documents_the_missing_force():
    text = (WORKFLOW.parent / "apply-migrations.yml").read_text()
    assert "npx wrangler@4 d1 migrations apply cbb-rankings --remote" in text
    assert "--force" in text and text.count("--force") == 1  # only in the explanatory comment
    assert "No --force" in text


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_workflow_permissions_parse_where_yaml_is_available(path):
    yaml = pytest.importorskip("yaml")
    assert yaml.safe_load(path.read_text())["permissions"] == {"contents": "read"}
