"""Contract tests for the weekly, staging and ratings-backfill workflows (text and parsed YAML)."""
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"
D1_SECRETS = ("CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_D1_DATABASE_ID", "CLOUDFLARE_API_TOKEN")
R2_SECRETS = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET_NAME")
NEW = ("weekly-update.yml", "test-week-run.yml", "backfill-ratings.yml")


def load(name):
    text = (WORKFLOWS / name).read_text()
    doc = yaml.safe_load(text)
    doc["on"] = doc.pop(True)  # PyYAML reads the key `on` as True
    return text, doc


def steps(doc):
    return next(iter(doc["jobs"].values()))["steps"]


def step(doc, name):
    return next(s for s in steps(doc) if s.get("name") == name)


@pytest.mark.parametrize("name", NEW)
def test_common_hardening(name):
    text, doc = load(name)
    job = next(iter(doc["jobs"].values()))
    assert doc["permissions"] == {"contents": "read"}
    assert doc["concurrency"] == {"group": "weekly-rankings", "cancel-in-progress": False}
    assert job["timeout-minutes"] == 30 and job["runs-on"] == "ubuntu-latest"
    assert "|| true" not in text and "continue-on-error" not in text
    uses = [s["uses"] for s in job["steps"] if "uses" in s]
    assert uses == ["actions/checkout@v7", "actions/setup-python@v7", "astral-sh/setup-uv@v7"]
    assert step(doc, "Install dependencies")["run"] == "uv sync --frozen"
    for s in job["steps"]:
        run = s.get("run", "")
        assert "${{" not in run, f"expression interpolated inside run: in step {s.get('name')}"  # inputs and secrets go through env
        assert "uv run python" not in run and "uv sync\n" not in run
        assert "set -x" not in run
    main_step = next(s for s in job["steps"] if "main.py" in s.get("run", ""))
    assert "set -o pipefail" in main_step["run"] and "| tee output.log" in main_step["run"]
    assert "uv run --frozen python main.py" in main_step["run"]
    assert "inputs." not in "".join(s.get("run", "") for s in job["steps"])


@pytest.mark.parametrize("name", NEW)
def test_the_last_step_always_writes_the_markers_to_the_job_summary(name):
    _, doc = load(name)
    last = steps(doc)[-1]
    assert last["if"] == "always()" and "GITHUB_STEP_SUMMARY" in last["run"] and "output.log" in last["run"]
    for marker in ("PUBLISH_FAILURE", "MODEL_FAILURE", "PRIOR_FAILURE"):
        assert marker in last["run"]


def test_weekly_triggers_inputs_and_secrets():
    text, doc = load("weekly-update.yml")
    assert doc["on"]["schedule"] == [{"cron": "0 10 * * 1"}]  # Monday 10:00 UTC
    assert set(doc["on"]["workflow_dispatch"]["inputs"]) == {"season", "week"}
    assert all(not i["required"] for i in doc["on"]["workflow_dispatch"]["inputs"].values())
    env = step(doc, "Run weekly update")["env"]
    for name in ("CBBD_API_KEY",) + D1_SECRETS + R2_SECRETS:
        assert env[name] == "${{ secrets." + name + " }}"
    assert env["SEASON"] == "${{ inputs.season }}" and env["WEEK"] == "${{ inputs.week }}"
    assert "PERSONAL_SITE_DEPLOY_HOOK_URL" not in str(env)  # only the hook step sees it
    run = step(doc, "Run weekly update")["run"]
    assert "--season" in run and "--week" in run and "--staging" not in run and "--no-ingest" not in run


def test_deploy_hook_runs_only_after_a_publish_and_never_echoes_the_url():
    text, doc = load("weekly-update.yml")
    detect, hook = step(doc, "Detect a publish"), step(doc, "Trigger personal-site rebuild")
    assert "grep -q '^WEEKLY_OK ' output.log" in detect["run"] and "GITHUB_OUTPUT" in detect["run"]
    assert hook["if"] == "steps.published.outputs.published == 'true'"
    assert hook["env"] == {"DEPLOY_HOOK_URL": "${{ secrets.PERSONAL_SITE_DEPLOY_HOOK_URL }}"}
    assert text.count("PERSONAL_SITE_DEPLOY_HOOK_URL") == 2  # the secret reference and the not-set error message
    assert "echo \"$DEPLOY_HOOK_URL\"" not in hook["run"] and "echo $DEPLOY_HOOK_URL" not in hook["run"]
    assert "--fail" in hook["run"] and "--output /dev/null" in hook["run"]
    assert "ARE published" in hook["run"]  # a hook failure fails the job but says the artifacts stay published
    names = [s.get("name") for s in steps(doc)]
    assert names.index("Run weekly update") < names.index("Detect a publish") < names.index("Trigger personal-site rebuild")


def test_test_week_run_is_staging_only_unless_asked():
    _, doc = load("test-week-run.yml")
    assert "schedule" not in doc["on"]
    inputs = doc["on"]["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"season", "week", "publish_staging", "allow_missing_prior"}
    assert inputs["season"]["required"] and inputs["week"]["required"]
    assert inputs["publish_staging"]["default"] is False and inputs["allow_missing_prior"]["default"] is False
    run = step(doc, "Run staging week")["run"]
    assert "args=(--staging --season" in run
    assert run.count("--publish-staging") == 1 and 'PUBLISH_STAGING" = "true"' in run
    assert "PERSONAL_SITE_DEPLOY_HOOK_URL" not in (WORKFLOWS / "test-week-run.yml").read_text()
    assert "curl" not in run


def test_backfill_ratings_is_a_dry_run_in_staging_by_default():
    text, doc = load("backfill-ratings.yml")
    assert "schedule" not in doc["on"]
    inputs = doc["on"]["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"season", "weeks", "destination", "apply", "allow_missing_prior"}
    assert inputs["apply"]["default"] is False and inputs["destination"]["default"] == "staging"
    assert inputs["destination"]["options"] == ["staging", "staging-published", "production"]
    run = step(doc, "Backfill ratings")["run"]
    assert "--no-ingest" in run and "--dry-run" in run and 'APPLY" != "true"' in run
    assert "CBBD_API_KEY" not in text  # ratings-only: no API calls
    assert "--all-weeks" in run and "--weeks" in run
