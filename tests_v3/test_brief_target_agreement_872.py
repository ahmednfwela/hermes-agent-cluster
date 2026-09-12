"""#872: a task's brief must match the target its lane names.

`tasks.title` IS the brief -- the schema has no description column. On
2026-09-12 the authoring path wrote one task's brief verbatim into another
task's title: the reviewer lane for PR#274 (`infra-github!274-rev-b`) was handed
an IMPLEMENTATION brief that named no PR at all. The lane did exactly as asked
and posted nothing. The lead read `completed` with no verdict, concluded the
result had been lost, and paid for a re-review plus a 13-agent diagnosis.

There was never a lost verdict. There was a brief that did not match its lane.
"""

import pytest
from fastapi.testclient import TestClient

from hermes_cluster.app import create_app
from hermes_cluster.routers.tasks import _lane_target, _brief_names_target


@pytest.fixture
def client():
    return TestClient(
        create_app(cluster_id="test-cluster", node_id="test-node", node_role="main")
    )


# --- the incident, reproduced ------------------------------------------------

# Verbatim shape of the brief that actually shipped to task_d317c36d2243d429:
# an author brief, naming no PR, with no posting instruction.
_WRONG_BRIEF = (
    "=== WRITE YOUR VERDICT FIRST. === AUTHOR LANE - wire MCP into the GKE "
    "brain. ONE Draft PR. NEVER approve or merge."
)


def test_the_274_incident_is_rejected(client):
    """The exact failure: a reviewer lane for PR#274 handed an author brief."""
    r = client.post(
        "/api/v1/tasks",
        json={"title": _WRONG_BRIEF, "lane_key": "infra-github!274-rev-b"},
    )
    assert r.status_code == 422, "the wrong-brief task was accepted"
    assert "274" in r.json()["detail"]
    assert "#872" in r.json()["detail"], "the rejection should name the defect"


def test_a_matching_brief_is_accepted(client):
    """The guard must not block correct work."""
    r = client.post(
        "/api/v1/tasks",
        json={
            "title": "REVIEW-ONLY lane - review PR#274 at head 85a9b382f. Post with gh.",
            "lane_key": "infra-github!274-rev-b",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["lane_key"] == "infra-github!274-rev-b"


# --- the cases most likely to produce a FALSE rejection ----------------------

def test_a_lane_with_no_numeric_target_is_unconstrained(client):
    """`claude-plugins#feat/869-seat-by-paste` names a BRANCH, not a number.

    The segment after `#` is not digits, so there is no target to disagree with
    and any brief is legal. Getting this wrong would reject every author lane.
    """
    assert _lane_target("claude-plugins#feat/869-seat-by-paste") is None
    r = client.post(
        "/api/v1/tasks",
        json={"title": "AUTHOR LANE - phone-only seat add", "lane_key": "claude-plugins#feat/869-seat-by-paste"},
    )
    assert r.status_code == 200, r.text


def test_no_lane_key_is_unconstrained(client):
    """A per-task session carries no lane identity, so nothing to check."""
    assert _lane_target("") is None
    r = client.post("/api/v1/tasks", json={"title": "anything at all"})
    assert r.status_code == 200, r.text


def test_target_matching_is_bounded_not_substring(client):
    """"274" must not be satisfied by "1274" or "2740".

    A substring match would let a brief about a different PR silently pass --
    reintroducing the defect while appearing to guard against it.
    """
    assert _brief_names_target("review PR#274 now", "274") is True
    assert _brief_names_target("review PR#1274 now", "274") is False
    assert _brief_names_target("review PR#2740 now", "274") is False

    r = client.post(
        "/api/v1/tasks",
        json={"title": "REVIEW-ONLY - review PR#1274", "lane_key": "infra-github!274-rev"},
    )
    assert r.status_code == 422, "a near-miss number satisfied the guard"


def test_issue_shaped_lane_keys_are_checked_too(client):
    """`claude-plugins!912-rev2` -> 912. MRs and issues both name targets."""
    assert _lane_target("claude-plugins!912-rev2") == "912"
    bad = client.post(
        "/api/v1/tasks",
        json={"title": "review the drop", "lane_key": "claude-plugins!912-rev2"},
    )
    assert bad.status_code == 422
    good = client.post(
        "/api/v1/tasks",
        json={"title": "review !912 at head 420e4bc3", "lane_key": "claude-plugins!912-rev2"},
    )
    assert good.status_code == 200, good.text


# --- instance 2 of the issue: the ACTION names a different target -----------

# Verbatim shape of task_f4c0e3d70e522f93's brief: it mentions its own target
# (so the guard above passes) while instructing `gh pr comment 273` -- the
# WRONG PR. The 275 lane silently overrode its own instruction and posted to
# 275. A lane correcting its brief is luck, not a control.
_INSTANCE_2_BRIEF = (
    "REVIEW-ONLY lane for pr#275. Post your verdict with "
    "`gh pr comment 273 --repo Bdaya-Dev/infra-github --body-file v.md`. "
    "NEVER approve or merge."
)


def test_the_275_comment_273_incident_is_rejected(client):
    """Instance 2: brief mentions the lane target but instructs a wrong-PR action."""
    r = client.post(
        "/api/v1/tasks",
        json={"title": _INSTANCE_2_BRIEF, "lane_key": "infra-github!275-rev-c"},
    )
    assert r.status_code == 422, (
        "instance 2 escaped: a brief whose ACTION (`gh pr comment 273`) names a "
        "different PR than the lane (275) was accepted -- the lane would post the "
        "verdict to the wrong PR, or override its own brief and get lucky"
    )
    assert "275" in r.json()["detail"]
    assert "273" in r.json()["detail"], "the rejection should name the offending number"


def test_action_mentioning_the_own_target_is_accepted(client):
    """The corrected instance-2 brief (comment on 275) must pass."""
    r = client.post(
        "/api/v1/tasks",
        json={
            "title": _INSTANCE_2_BRIEF.replace("273", "275"),
            "lane_key": "infra-github!275-rev-c",
        },
    )
    assert r.status_code == 200, r.text


def test_action_target_extraction_is_verb_bounded():
    """Only verb-attached numbers count as the job's action target.

    `NEVER approve or merge` (no number) must not trigger, and a bare
    cross-reference like `Refs #872` is context, not an instruction --
    false-rejecting those would block ordinary reviewer briefs.
    """
    from hermes_cluster.routers.tasks import _brief_action_targets

    assert _brief_action_targets(_INSTANCE_2_BRIEF) == {"273"}
    assert _brief_action_targets(_WRONG_BRIEF) == set()          # verbs, no numbers
    assert _brief_action_targets("REVIEW-ONLY - review PR#274 at head 85a9b382f") == {"274"}
    assert "872" not in _brief_action_targets("fix per Refs #872")
    assert _brief_action_targets("post the verdict to 275") == {"275"}
    assert _brief_action_targets("reviewed 128, merged 129") == {"128", "129"}
