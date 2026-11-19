from fastapi.testclient import TestClient

DIGEST = "python@sha256:" + "a" * 64


def create_released_project(client: TestClient, *, labels: dict | None = None) -> tuple[str, str]:
    response = client.post(
        "/api/v1/projects",
        json={
            "name": "demo",
            "repository_url": "https://example.invalid/demo.git",
            "framework": "pytest",
            "owner": "qa",
            "concurrency_limit": 1,
            "draft_config": {
                "image_digest": DIGEST,
                "recipe": [{"phase": "test", "command": ["pytest", "-q"], "timeout_seconds": 60}],
                "required_labels": labels or {},
                "artifact_paths": ["junit.xml"],
            },
        },
    )
    assert response.status_code == 201, response.text
    project_id = response.json()["id"]
    release = client.post(f"/api/v1/projects/{project_id}/releases")
    assert release.status_code == 201, release.text
    return project_id, release.json()["id"]


def submit(client: TestClient, project_id: str, key: str = "pipeline-123") -> dict:
    response = client.post(
        "/api/v1/executions",
        json={
            "project_id": project_id,
            "source_revision": "abcdef0123456789",
            "idempotency_key": key,
            "parameters": {"environment": "test"},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def register(client: TestClient, labels: dict | None = None) -> str:
    response = client.post(
        "/api/v1/agents/register",
        json={
            "name": "agent-1",
            "labels": labels or {},
            "cpu_millis": 4000,
            "memory_mb": 8192,
            "max_concurrency": 1,
        },
    )
    assert response.status_code == 200
    return response.json()["id"]


def test_full_execution_contract(client: TestClient):
    project_id, release_id = create_released_project(client, labels={"os": "linux"})
    execution = submit(client, project_id)
    assert execution["release_id"] == release_id
    assert execution["status"] == "QUEUED"
    assert execution["context"]["image_digest"] == DIGEST

    duplicate = client.post(
        "/api/v1/executions",
        json={
            "project_id": project_id,
            "source_revision": "abcdef0123456789",
            "idempotency_key": "pipeline-123",
        },
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["id"] == execution["id"]

    agent_id = register(client, {"os": "linux"})
    claimed = client.post(f"/api/v1/agents/{agent_id}/claim")
    assert claimed.status_code == 200, claimed.text
    work = claimed.json()
    lease = {"lease_token": work["lease_token"], "generation": work["generation"]}

    running = client.post(
        f"/api/v1/executions/{execution['id']}/state", json={**lease, "status": "RUNNING"}
    )
    assert running.status_code == 200
    assert running.json()["status"] == "RUNNING"

    log = client.post(
        f"/api/v1/executions/{execution['id']}/logs",
        json={**lease, "sequence": 0, "stream": "stdout", "content": "2 passed\n"},
    )
    assert log.status_code == 201
    duplicate_log = client.post(
        f"/api/v1/executions/{execution['id']}/logs",
        json={**lease, "sequence": 0, "stream": "stdout", "content": "2 passed\n"},
    )
    assert duplicate_log.json()["duplicate"] is True

    finalizing = client.post(
        f"/api/v1/executions/{execution['id']}/state",
        json={**lease, "status": "FINALIZING"},
    )
    assert finalizing.status_code == 200
    complete = client.post(
        f"/api/v1/executions/{execution['id']}/finalize",
        json={
            **lease,
            "process_exit_code": 0,
            "result_kind": "PASSED",
            "artifacts": [{"path": "junit.xml", "size": 42, "sha256": "b" * 64, "uri": None}],
            "cleanup_complete": True,
            "delivery_complete": True,
        },
    )
    assert complete.status_code == 200, complete.text
    assert complete.json()["status"] == "COMPLETED"
    assert complete.json()["artifacts_sealed"] is True

    assert (
        client.get(f"/api/v1/executions/{execution['id']}/logs").json()[0]["content"]
        == "2 passed\n"
    )
    assert (
        client.get(f"/api/v1/executions/{execution['id']}/artifacts").json()[0]["path"]
        == "junit.xml"
    )


def test_label_mismatch_does_not_claim(client: TestClient):
    project_id, _ = create_released_project(client, labels={"browser": "chrome"})
    submit(client, project_id)
    agent_id = register(client, {"browser": "firefox"})
    assert client.post(f"/api/v1/agents/{agent_id}/claim").status_code == 204


def test_queued_execution_can_be_cancelled(client: TestClient):
    project_id, _ = create_released_project(client)
    execution = submit(client, project_id)
    response = client.post(f"/api/v1/executions/{execution['id']}/cancel")
    assert response.status_code == 200
    current = client.get(f"/api/v1/executions/{execution['id']}").json()
    assert current["status"] == "CANCELLED"
    assert current["result_kind"] == "CANCELLED"


def test_rejects_mutable_image_tag(client: TestClient):
    response = client.post(
        "/api/v1/projects",
        json={
            "name": "unsafe",
            "repository_url": "https://example.invalid/demo.git",
            "framework": "pytest",
            "owner": "qa",
            "draft_config": {
                "image_digest": "python:latest",
                "recipe": [{"phase": "test", "command": ["pytest"]}],
            },
        },
    )
    assert response.status_code == 422


def test_rejects_non_linear_recipe(client: TestClient):
    response = client.post(
        "/api/v1/projects",
        json={
            "name": "non-linear",
            "repository_url": "https://example.invalid/demo.git",
            "framework": "pytest",
            "owner": "qa",
            "draft_config": {
                "image_digest": DIGEST,
                "recipe": [
                    {"phase": "cleanup", "command": ["rm", "-f", "temp"]},
                    {"phase": "test", "command": ["pytest"]},
                ],
            },
        },
    )
    assert response.status_code == 422
