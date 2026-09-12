"""#872 instance 2: a brief whose POSTING command names a different target
than the lane.

PR#30 (merged as 6771ec7) added the brief/target guard: if `lane_key` names
`!274`, the brief must mention 274. That kills instance 1 of #872 (the 274
reviewer lane handed an author brief naming no PR). It does NOT touch
instance 2, which is measured here: the lane for PR#275 carried a correct
`pr#275` mention AND an instruction to `gh pr comment 273` -- the wrong PR.
The brief passed the merged guard; the lane overrode its own instruction and
posted to 275 anyway. The issue's words: "A lane correcting its brief is
luck, not a control."

This extends the same 422 to action-bound numbers: a `gh|glab pr|mr|issue
<verb> <N>` command inside the brief must name the lane's own target. Bare
`#NNN` MENTIONS are deliberately NOT bound -- briefs legitimately cite other
issues ("Fixes shared/claude-plugins#871") and a citation does not tell the
lane where to act.
"""

import pytest
from fastapi.testclient import TestClient

from hermes_cluster.app import create_app


@pytest.fixture
def client():
    return TestClient(
        create_app(cluster_id="test-cluster", node_id="test-node", node_role="main")
    )


# Verbatim shape of task_f4c0e3d70e522f93: correct primary mention, wrong
# posting target.
_INSTANCE2_BRIEF = (
    "REVIEW-ONLY lane - Bdaya-Dev/infra-github PR#275 at head ae67e42. "
    "NEVER approve or merge. Post the verdict with "
    "gh pr comment 273 --repo Bdaya-Dev/infra --body-file v.md, then read "
    "it back and quote the first line."
)


def test_instance2_wrong_posting_target_is_rejected(client):
    """The residual gap: PR#30's guard accepts this brief; #872's instance 2
    was exactly this shape."""
    r = client.post(
        "/api/v1/tasks",
        json={"title": _INSTANCE2_BRIEF, "lane_key": "infra-github!275-rev-c"},
    )
    assert r.status_code == 422, (
        "a brief that posts to 273 on a 275 lane was accepted -- "
        "instance 2 of #872 is still unguarded"
    )
    assert "273" in r.json()["detail"]
    assert "#872" in r.json()["detail"]


def test_correct_posting_target_is_accepted(client):
    """The same brief with the right number must pass -- the guard may not
    block correct work."""
    r = client.post(
        "/api/v1/tasks",
        json={
            "title": _INSTANCE2_BRIEF.replace("comment 273", "comment 275"),
            "lane_key": "infra-github!275-rev-c",
        },
    )
    assert r.status_code == 200, r.text


def test_bare_cross_references_are_not_bound(client):
    """A MENTION of another issue is not a posting action.

    Reviewer briefs routinely say `Fixes shared/claude-plugins#871` or cite
    other defect numbers. Only `gh|glab <pr|mr> <verb> <N>` binds; getting
    this wrong would reject most real reviewer lanes.
    """
    r = client.post(
        "/api/v1/tasks",
        json={
            "title": (
                "REVIEW-ONLY lane - hermes-agent-cluster PR#26 at head 19933ea. "
                "Fixes shared/claude-plugins#871 - read it before judging."
            ),
            "lane_key": "hermes-cluster!26-rev-b",
        },
    )
    assert r.status_code == 200, r.text


def test_gh_pr_comment_still_passes_when_agreeing(client):
    """Real corpus shape (task_34c6e948...): `gh pr comment 27 --repo
    Bdaya-Dev/hermes-agent-cluster` on a `!27` lane. Agreement is common;
    the guard must be invisible to it."""
    r = client.post(
        "/api/v1/tasks",
        json={
            "title": (
                "REVIEW-ONLY lane - PR#27 at head efef2df73. Post the verdict: "
                "gh pr comment 27 --repo Bdaya-Dev/hermes-agent-cluster "
                "--body-file v.md"
            ),
            "lane_key": "hermes-cluster!27-rev-a",
        },
    )
    assert r.status_code == 200, r.text


def test_bound_ref_is_bounded_not_substring(client):
    """`gh pr comment 27` on a !274 lane is a mismatch even though '27' is a
    substring of '274' -- the same bounded rule the primary guard uses.
    Behavioral so the red on main is an assertion, not an ImportError."""
    r = client.post(
        "/api/v1/tasks",
        json={
            "title": (
                "REVIEW-ONLY lane - review PR#274. Post the verdict: "
                "gh pr comment 27 --repo Bdaya-Dev/infra"
            ),
            "lane_key": "infra-github!274-rev-c",
        },
    )
    assert r.status_code == 422, "'27' satisfied a '274' target by substring"
    ok = client.post(
        "/api/v1/tasks",
        json={
            "title": (
                "REVIEW-ONLY lane - review PR#274. Post the verdict: "
                "gh pr comment 274 --repo Bdaya-Dev/infra"
            ),
            "lane_key": "infra-github!274-rev-c",
        },
    )
    assert ok.status_code == 200, ok.text


def test_no_target_lane_is_unconstrained(client):
    """Branch-shaped lane keys name no number, so nothing can disagree --
    even a posting command pointing anywhere."""
    r = client.post(
        "/api/v1/tasks",
        json={
            "title": "AUTHOR LANE - wire it up. Post via gh pr comment 999 --repo x/y.",
            "lane_key": "claude-plugins#feat/869-seat-by-paste",
        },
    )
    assert r.status_code == 200, r.text


def test_multiple_actions_one_mismatch_is_rejected(client):
    """If a brief posts to the right PR AND acts on a wrong one, the wrong
    one still fails the task at authoring, not silently at the lane."""
    r = client.post(
        "/api/v1/tasks",
        json={
            "title": (
                "REVIEW-ONLY lane - review PR#275. gh pr comment 275 --body-file v.md; "
                "then gh pr close 274 --repo Bdaya-Dev/infra"
            ),
            "lane_key": "infra-github!275-rev-c",
        },
    )
    assert r.status_code == 422, "the mismatched gh pr close 274 slipped through"
    assert "274" in r.json()["detail"]
