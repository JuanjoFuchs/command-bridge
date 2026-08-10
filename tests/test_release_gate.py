"""Nothing may reach PyPI without the suite passing first.

CI was red for five consecutive pushes across four releases and every one of them published
anyway. Not because anybody overrode anything — because CI triggers on `push: branches` and the
release workflow triggers on `push: tags`, so they are two unrelated runs and neither can see the
other. The tag was sufficient on its own.

PyPI has no unpublish. That makes this the one gate in the project where "we will notice" is not
an acceptable control, so it is asserted here rather than trusted to a convention.
"""
import os

import pytest

yaml = pytest.importorskip("yaml", reason="PyYAML is a dev dependency")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RELEASE = os.path.join(ROOT, ".github", "workflows", "release.yml")


@pytest.fixture(scope="module")
def workflow():
    if not os.path.exists(RELEASE):
        pytest.skip("no release workflow in this checkout")
    with open(RELEASE, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _publishing_jobs(workflow):
    """Any job that talks to an index. Found by what it runs, not by name — a renamed job must
    not slip out from under this."""
    out = {}
    for name, job in workflow.get("jobs", {}).items():
        blob = str(job)
        if "pypa/gh-action-pypi-publish" in blob or "twine upload" in blob or "npm publish" in blob:
            out[name] = job
    return out


def test_the_release_workflow_runs_the_tests(workflow):
    jobs = workflow.get("jobs", {})
    assert "test" in jobs, "the release workflow must run the suite itself"
    steps = str(jobs["test"])
    assert "pytest" in steps, "a gate that does not run pytest is not a gate"
    assert "ruff" in steps, "lint too — CI's red runs included lint failures"


def test_every_publishing_job_waits_for_the_tests(workflow):
    """THE REGRESSION. A publish step with no `needs: test` above it is the exact configuration
    that shipped four releases on a red suite."""
    publishers = _publishing_jobs(workflow)
    assert publishers, "no publishing job found — has this workflow been restructured?"

    jobs = workflow.get("jobs", {})
    for name, job in publishers.items():
        needs = job.get("needs") or []
        if isinstance(needs, str):
            needs = [needs]
        # Either it waits on the gate directly or on something that does. One hop is all this
        # project's graph has ever been.
        chain = set(needs)
        for dep in list(needs):
            dep_needs = jobs.get(dep, {}).get("needs") or []
            chain.update([dep_needs] if isinstance(dep_needs, str) else dep_needs)
        assert "test" in chain, f"job `{name}` can publish without the suite passing"
