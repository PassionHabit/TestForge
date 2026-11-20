from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from testforge.models import Execution, ExecutionStatus, Lease, utcnow
from tests.test_api_flow import DIGEST, create_released_project, register, submit


def test_expired_runtime_can_be_safely_adopted(client: TestClient, session: Session):
    project_id, _ = create_released_project(client)
    execution = submit(client, project_id)
    agent_id = register(client)
    work = client.post(f"/api/v1/agents/{agent_id}/claim").json()

    lease = session.scalar(select(Lease).where(Lease.id == work["lease_id"]))
    lease.expires_at = utcnow() - timedelta(seconds=1)
    session.commit()

    reconciled = client.post("/api/v1/admin/reconcile")
    assert reconciled.json()["reconciled"] == 1
    current = client.get(f"/api/v1/executions/{execution['id']}").json()
    assert current["status"] == "RECONCILING"

    recovered = client.post(
        f"/api/v1/agents/{agent_id}/reconcile",
        json={
            "runtimes": [
                {
                    "execution_id": execution["id"],
                    "generation": work["generation"],
                    "container_name": "testforge-existing",
                }
            ]
        },
    )
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()[0]["generation"] == work["generation"] + 1
    assert recovered.json()[0]["execution"]["status"] == "RUNNING"


def test_started_reconciliation_cannot_be_requeued(client: TestClient, session: Session):
    project_id, _ = create_released_project(client)
    execution = submit(client, project_id)
    row = session.get(Execution, execution["id"])
    row.status = ExecutionStatus.RECONCILING
    row.started_at = utcnow()
    session.commit()
    response = client.post(
        f"/api/v1/admin/executions/{execution['id']}/resolve",
        json={"action": "requeue_unstarted", "reason": "agent disappeared"},
    )
    assert response.status_code == 409


def test_agent_maintenance_prevents_new_claims(client: TestClient):
    project_id, _ = create_released_project(client)
    submit(client, project_id)
    agent_id = register(client)
    response = client.patch(f"/api/v1/agents/{agent_id}/maintenance", json={"maintenance": True})
    assert response.json()["maintenance"] is True
    assert client.post(f"/api/v1/agents/{agent_id}/claim").status_code == 204


def test_scheduler_reserves_aggregate_node_resources(client: TestClient):
    project = client.post(
        "/api/v1/projects",
        json={
            "name": "resource-heavy",
            "repository_url": "https://example.invalid/demo.git",
            "framework": "pytest",
            "owner": "qa",
            "concurrency_limit": 2,
            "draft_config": {
                "image_digest": DIGEST,
                "recipe": [{"phase": "test", "command": ["pytest"]}],
                "cpu_millis": 3000,
                "memory_mb": 1024,
            },
        },
    ).json()
    client.post(f"/api/v1/projects/{project['id']}/releases")
    submit(client, project["id"], "resource-job-1")
    submit(client, project["id"], "resource-job-2")
    agent = client.post(
        "/api/v1/agents/register",
        json={
            "name": "resource-agent",
            "labels": {},
            "cpu_millis": 4000,
            "memory_mb": 8192,
            "max_concurrency": 2,
        },
    ).json()
    assert client.post(f"/api/v1/agents/{agent['id']}/claim").status_code == 200
    assert client.post(f"/api/v1/agents/{agent['id']}/claim").status_code == 204
