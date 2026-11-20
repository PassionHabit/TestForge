import hashlib
from types import SimpleNamespace

import pytest

from testforge.agent import AgentSettings, DockerRunner, observed_docker_runtimes


class FakeControlClient:
    def upload_artifact(self, work, artifact_path, local_path):
        content = local_path.read_bytes()
        return {
            "path": artifact_path,
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "uri": f"artifact://execution-1/{artifact_path}",
        }


def test_docker_command_applies_security_and_resource_limits(tmp_path):
    settings = AgentSettings(work_root=str(tmp_path))
    runner = DockerRunner(settings, FakeControlClient())
    source = tmp_path / "source"
    source.mkdir()
    execution = {
        "id": "12345678-1234-1234-1234-123456789012",
        "lease_generation": 4,
        "cpu_millis": 1500,
        "memory_mb": 2048,
        "parameters": {"browser": "chrome"},
        "image_digest": "python@sha256:" + "a" * 64,
    }
    command, name = runner._docker_command(
        execution,
        tmp_path,
        {"phase": "test", "command": ["pytest", "-q"]},
    )
    assert name.startswith("testforge-12345678")
    assert command[command.index("--cap-drop") + 1] == "ALL"
    assert "no-new-privileges:true" in command
    assert "testforge.lease_generation=4" in command
    assert "testforge.phase=test" in command
    assert execution["image_digest"] in command
    assert ["pytest", "-q"] == command[-2:]


def test_native_execution_is_disabled_by_default(tmp_path):
    runner = DockerRunner(AgentSettings(work_root=str(tmp_path)), FakeControlClient())
    work = {
        "execution": {
            "context": {"execution_kind": "native"},
        }
    }
    with pytest.raises(RuntimeError, match="native execution is disabled"):
        runner._run_step(
            work,
            tmp_path,
            {"phase": "test", "command": ["true"], "timeout_seconds": 1},
        )


def test_agent_discovers_labeled_running_containers(monkeypatch):
    monkeypatch.setattr("testforge.agent.shutil.which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(
        "testforge.agent.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout="testforge-abc|execution-1|3\nunrelated-line\n",
        ),
    )
    assert observed_docker_runtimes() == [
        {
            "container_name": "testforge-abc",
            "execution_id": "execution-1",
            "generation": 3,
        }
    ]
