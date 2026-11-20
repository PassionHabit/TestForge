from fastapi.testclient import TestClient


def test_image_build_requires_scan_and_digest_before_publish(client: TestClient):
    build = client.post(
        "/api/v1/image-builds",
        json={
            "repository_url": "https://example.invalid/tests.git",
            "source_revision": "abcdef0",
            "dockerfile": "FROM python:3.12-slim\nRUN useradd runner\nUSER runner\n",
            "requested_by": "qa@example.com",
        },
    )
    assert build.status_code == 201
    build_id = build.json()["id"]
    rejected = client.post(
        f"/api/v1/image-builds/{build_id}/result",
        json={
            "status": "PUBLISHED",
            "image_digest": "registry.example/test:latest",
            "scan_passed": False,
        },
    )
    assert rejected.status_code == 422
    published = client.post(
        f"/api/v1/image-builds/{build_id}/result",
        json={
            "status": "PUBLISHED",
            "image_digest": "registry.example/test@sha256:" + "c" * 64,
            "scan_passed": True,
        },
    )
    assert published.status_code == 200
    assert published.json()["status"] == "PUBLISHED"
