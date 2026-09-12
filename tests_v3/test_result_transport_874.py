"""#874: a lane's deliverable must survive the node that produced it.

Before this change `POST /tasks/{id}/complete` took no body and the `tasks`
table had no `result` column. A lane wrote its deliverable to
``<that node's working_dir>/hermes-results/<task_id>.result.md`` -- local disk
on whichever machine happened to run it -- so a verdict produced on one node
was unreadable from every other, and "produced nothing" was indistinguishable
from "produced something unreachable".

That ambiguity cost a full re-review and a 13-agent diagnosis on 2026-09-12.
"""

import pytest
from fastapi.testclient import TestClient

from hermes_cluster.app import create_app


@pytest.fixture
def app():
    return create_app(cluster_id="test-cluster", node_id="test-node", node_role="main")


@pytest.fixture
def client(app):
    return TestClient(app)


def _claimed_task(client, title="deliverable test"):
    """Create a task and put it in a state where /complete is legal."""
    tid = client.post("/api/v1/tasks", json={"title": title}).json()["id"]
    node_id = client.post(
        "/api/v1/nodes/join", json={"node_name": "w1", "capabilities": ["coding"]}
    ).json()["node_id"]
    client.post(f"/api/v1/tasks/{tid}/claim", json={"node_id": node_id})
    return tid


def test_complete_carries_the_deliverable(client):
    """The whole point: a result posted to /complete is readable off the row."""
    tid = _claimed_task(client)
    verdict = "Reviewer verdict: PASS\nSHA: deadbeefcafe"

    done = client.post(f"/api/v1/tasks/{tid}/complete", json={"result": verdict})
    assert done.status_code == 200, done.text
    assert done.json()["result_stored"] is True

    back = client.get(f"/api/v1/tasks/{tid}")
    assert back.status_code == 200, back.text
    assert back.json()["result"] == verdict, (
        "the deliverable did not survive on the task row -- this is #874, where a "
        "result is reachable only from the node that produced it"
    )


def test_complete_without_a_body_is_unchanged(client):
    """Back-compat: existing callers post no body and must keep working."""
    tid = _claimed_task(client, "no body")
    done = client.post(f"/api/v1/tasks/{tid}/complete")
    assert done.status_code == 200, done.text
    assert done.json()["result_stored"] is False
    assert client.get(f"/api/v1/tasks/{tid}").json()["result"] is None


def test_blank_result_is_not_recorded(client):
    """Whitespace is not a deliverable.

    Guards the #870 lesson at a new layer: the old success gate was
    ``contents.strip()``, where any non-whitespace byte counted. Here the
    inverse must hold -- a blank body must not be mistaken for a result.
    """
    tid = _claimed_task(client, "blank")
    done = client.post(f"/api/v1/tasks/{tid}/complete", json={"result": "   \n\t  "})
    assert done.status_code == 200, done.text
    assert done.json()["result_stored"] is False
    assert client.get(f"/api/v1/tasks/{tid}").json()["result"] is None


def test_result_is_recorded_before_the_status_flip(client):
    """A reader that sees `completed` must never see it without the result.

    Ordering matters: if the status flipped first, a poller could observe a
    terminal task whose deliverable had not landed yet -- reproducing the very
    ambiguity this issue exists to remove.
    """
    tid = _claimed_task(client, "ordering")
    verdict = "ordered-deliverable"
    client.post(f"/api/v1/tasks/{tid}/complete", json={"result": verdict})

    task = client.get(f"/api/v1/tasks/{tid}").json()
    assert task["status"] == "completed"
    assert task["result"] == verdict


def test_result_survives_a_task_listing(client):
    """The row is the transport, so the result travels with any read path."""
    tid = _claimed_task(client, "listing")
    client.post(f"/api/v1/tasks/{tid}/complete", json={"result": "from-the-list"})

    listed = [t for t in client.get("/api/v1/tasks").json() if t["id"] == tid]
    assert listed, "task vanished from the listing"
    assert listed[0]["result"] == "from-the-list"
