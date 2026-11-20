import httpx
from fastapi.testclient import TestClient

from tests.test_api_flow import DIGEST, register, submit


def test_versioned_public_service_lifecycle(client: TestClient, monkeypatch):
    calls: list[tuple[str, str]] = []

    def fake_request(method: str, url: str, **kwargs):
        calls.append((method, url))
        body = {"instance_id": "external-123"} if url.endswith("/create") else {}
        return httpx.Response(200, json=body, request=httpx.Request(method, url))

    monkeypatch.setattr("testforge.integrations.httpx.request", fake_request)
    service = client.post(
        "/api/v1/services",
        json={
            "name": "test-data",
            "service_type": "resource_permit",
            "owner": "qa",
            "draft_config": {
                "create_url": "https://services.example/create",
                "health_url": "https://services.example/health/{instance_id}",
                "cleanup_url": "https://services.example/cleanup/{instance_id}",
                "timeout_seconds": 5,
                "retries": 0,
            },
        },
    )
    assert service.status_code == 201, service.text
    service_release = client.post(f"/api/v1/services/{service.json()['id']}/releases").json()

    project = client.post(
        "/api/v1/projects",
        json={
            "name": "service-project",
            "repository_url": "https://example.invalid/demo.git",
            "framework": "pytest",
            "owner": "qa",
            "draft_config": {
                "image_digest": DIGEST,
                "recipe": [{"phase": "test", "command": ["pytest"]}],
                "services": [
                    {
                        "service_release_id": service_release["id"],
                        "binding_key": "test-data-permit",
                        "phase": "before",
                        "parameters": {"environment": "staging"},
                    }
                ],
            },
        },
    )
    client.post(f"/api/v1/projects/{project.json()['id']}/releases")
    execution = submit(client, project.json()["id"], "service-pipeline")
    agent_id = register(client)
    work = client.post(f"/api/v1/agents/{agent_id}/claim").json()
    lease = {"lease_token": work["lease_token"], "generation": work["generation"]}

    started = client.post(
        f"/api/v1/executions/{execution['id']}/services/start",
        json={**lease, "phase": "before"},
    )
    assert started.status_code == 200, started.text
    assert started.json()["started"] == 1
    cleaned = client.post(f"/api/v1/executions/{execution['id']}/services/cleanup", json=lease)
    assert cleaned.status_code == 200
    assert cleaned.json() == {"cleaned": 1, "failed": 0}
    assert calls == [
        ("POST", "https://services.example/create"),
        ("GET", "https://services.example/health/external-123"),
        ("POST", "https://services.example/cleanup/external-123"),
    ]
