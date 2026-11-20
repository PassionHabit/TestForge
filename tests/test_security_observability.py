from fastapi.testclient import TestClient

from testforge.config import get_settings


def test_api_keys_protect_control_and_agent_routes(client: TestClient, monkeypatch):
    monkeypatch.setenv("TESTFORGE_CONTROL_API_KEY", "control-secret")
    monkeypatch.setenv("TESTFORGE_AGENT_API_KEY", "agent-secret")
    get_settings.cache_clear()
    try:
        assert client.get("/api/v1/health").status_code == 200
        assert client.get("/api/v1/projects").status_code == 401
        assert (
            client.get(
                "/api/v1/projects",
                headers={"Authorization": "Bearer control-secret"},
            ).status_code
            == 200
        )
        registration = {
            "name": "secured-agent",
            "labels": {},
            "cpu_millis": 1000,
            "memory_mb": 1024,
            "max_concurrency": 1,
        }
        assert client.post("/api/v1/agents/register", json=registration).status_code == 401
        assert (
            client.post(
                "/api/v1/agents/register",
                json=registration,
                headers={"Authorization": "Bearer agent-secret"},
            ).status_code
            == 200
        )
    finally:
        monkeypatch.delenv("TESTFORGE_CONTROL_API_KEY")
        monkeypatch.delenv("TESTFORGE_AGENT_API_KEY")
        get_settings.cache_clear()


def test_prometheus_metrics_are_exposed(client: TestClient):
    response = client.get("/api/v1/metrics")
    assert response.status_code == 200
    assert "testforge_executions" in response.text
    assert "testforge_agents" in response.text


def test_web_console_is_served(client: TestClient):
    response = client.get("/")
    assert response.status_code == 200
    assert "TestForge Console" in response.text
