import hashlib
import json
import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from pydantic_settings import BaseSettings, SettingsConfigDict


class AgentSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TESTFORGE_AGENT_", env_file=".env")

    name: str = "docker-agent"
    api_url: str = "http://127.0.0.1:8000/api/v1"
    labels_json: str = "{}"
    cpu_millis: int = 4000
    memory_mb: int = 8192
    max_concurrency: int = 1
    poll_seconds: float = 2
    heartbeat_seconds: float = 15
    work_root: str = "./work/agent"
    graceful_stop_seconds: int = 10

    @property
    def labels(self) -> dict[str, str]:
        return json.loads(self.labels_json)


@dataclass
class Outcome:
    exit_code: int | None = None
    result_kind: str = "NOT_EVALUATED"
    failure_reason: str | None = None
    cancelled: bool = False
    cleanup_complete: bool = False
    logs: list[tuple[str, str]] = field(default_factory=list)


class ControlClient:
    def __init__(self, settings: AgentSettings):
        self.settings = settings
        self.http = httpx.Client(base_url=settings.api_url, timeout=30)
        self.agent_id: str | None = None

    def register(self) -> str:
        response = self.http.post(
            "/agents/register",
            json={
                "name": self.settings.name,
                "labels": self.settings.labels,
                "cpu_millis": self.settings.cpu_millis,
                "memory_mb": self.settings.memory_mb,
                "max_concurrency": self.settings.max_concurrency,
            },
        )
        response.raise_for_status()
        self.agent_id = response.json()["id"]
        return self.agent_id

    def claim(self) -> dict[str, Any] | None:
        assert self.agent_id
        response = self.http.post(f"/agents/{self.agent_id}/claim")
        if response.status_code == 204:
            return None
        response.raise_for_status()
        return response.json()

    def lease_payload(self, work: dict[str, Any]) -> dict[str, Any]:
        return {"lease_token": work["lease_token"], "generation": work["generation"]}

    def report_state(self, work: dict[str, Any], state: str) -> None:
        execution_id = work["execution"]["id"]
        response = self.http.post(
            f"/executions/{execution_id}/state",
            json={**self.lease_payload(work), "status": state},
        )
        response.raise_for_status()

    def heartbeat(self, work: dict[str, Any]) -> bool:
        execution_id = work["execution"]["id"]
        response = self.http.post(
            f"/executions/{execution_id}/lease/heartbeat", json=self.lease_payload(work)
        )
        response.raise_for_status()
        return bool(response.json()["cancel_requested"])

    def log(self, work: dict[str, Any], sequence: int, stream: str, content: str) -> None:
        execution_id = work["execution"]["id"]
        response = self.http.post(
            f"/executions/{execution_id}/logs",
            json={
                **self.lease_payload(work),
                "sequence": sequence,
                "stream": stream,
                "content": content,
            },
        )
        response.raise_for_status()

    def finalize(self, work: dict[str, Any], outcome: Outcome, artifacts: list[dict]) -> None:
        execution_id = work["execution"]["id"]
        response = self.http.post(
            f"/executions/{execution_id}/finalize",
            json={
                **self.lease_payload(work),
                "process_exit_code": outcome.exit_code,
                "result_kind": outcome.result_kind,
                "artifacts": artifacts,
                "cleanup_complete": outcome.cleanup_complete,
                "delivery_complete": True,
                "failure_reason": outcome.failure_reason,
            },
        )
        response.raise_for_status()


class DockerRunner:
    def __init__(self, settings: AgentSettings, client: ControlClient):
        self.settings = settings
        self.client = client
        self.sequence = 0

    def _emit(self, work: dict[str, Any], stream: str, content: str) -> None:
        if not content:
            return
        encoded = content.encode(errors="replace")
        chunk_size = 240 * 1024
        for offset in range(0, len(encoded), chunk_size):
            chunk = encoded[offset : offset + chunk_size].decode(errors="replace")
            self.client.log(work, self.sequence, stream, chunk)
            self.sequence += 1

    def _workspace(self, execution_id: str) -> Path:
        root = Path(self.settings.work_root).resolve()
        root.mkdir(parents=True, exist_ok=True)
        workspace = root / execution_id
        if workspace.exists():
            shutil.rmtree(workspace)
        workspace.mkdir(mode=0o700)
        return workspace

    def _checkout(self, execution: dict[str, Any], workspace: Path) -> None:
        source = workspace / "source"
        source.mkdir()
        repository = execution["context"]["repository_url"]
        revision = execution["source_revision"]
        commands = [
            ["git", "init", str(source)],
            ["git", "-C", str(source), "remote", "add", "origin", repository],
            ["git", "-C", str(source), "fetch", "--depth", "1", "origin", revision],
            ["git", "-C", str(source), "checkout", "--detach", "FETCH_HEAD"],
        ]
        for command in commands:
            result = subprocess.run(command, capture_output=True, text=True, timeout=300)
            if result.returncode:
                raise RuntimeError(f"source checkout failed: {result.stderr[-2000:]}")

    def _docker_command(
        self, execution: dict[str, Any], workspace: Path, step: dict[str, Any]
    ) -> tuple[list[str], str]:
        execution_id = execution["id"]
        container_name = f"testforge-{execution_id[:12]}-{self.sequence}"
        cpu = max(execution["cpu_millis"] / 1000, 0.1)
        command = [
            "docker",
            "run",
            "--rm",
            "--name",
            container_name,
            "--label",
            f"testforge.execution_id={execution_id}",
            "--label",
            f"testforge.lease_generation={execution['lease_generation']}",
            "--security-opt",
            "no-new-privileges:true",
            "--cap-drop",
            "ALL",
            "--pids-limit",
            "512",
            "--cpus",
            str(cpu),
            "--memory",
            f"{execution['memory_mb']}m",
            "--mount",
            f"type=bind,src={workspace / 'source'},dst=/workspace",
            "--workdir",
            "/workspace",
        ]
        for key, value in execution.get("parameters", {}).items():
            if isinstance(value, (str, int, float, bool)):
                env_name = "TESTFORGE_PARAM_" + "".join(
                    character if character.isalnum() else "_" for character in key.upper()
                )
                command.extend(["--env", f"{env_name}={value}"])
        command.extend([execution["image_digest"], *step["command"]])
        return command, container_name

    def _stop_container(self, container_name: str) -> None:
        subprocess.run(
            ["docker", "stop", "--time", str(self.settings.graceful_stop_seconds), container_name],
            capture_output=True,
            timeout=self.settings.graceful_stop_seconds + 5,
        )
        subprocess.run(["docker", "kill", container_name], capture_output=True, timeout=10)

    def _run_step(
        self,
        work: dict[str, Any],
        workspace: Path,
        step: dict[str, Any],
        *,
        honor_cancel: bool = True,
    ) -> tuple[int, bool]:
        command, container_name = self._docker_command(work["execution"], workspace, step)
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        cancelled = False
        last_heartbeat = 0.0
        deadline = time.monotonic() + step["timeout_seconds"]
        while process.poll() is None:
            now = time.monotonic()
            if now - last_heartbeat >= self.settings.heartbeat_seconds:
                cancel_requested = self.client.heartbeat(work)
                cancelled = cancel_requested and honor_cancel
                last_heartbeat = now
            if cancelled or now >= deadline:
                self._stop_container(container_name)
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                if now >= deadline and not cancelled:
                    self._emit(work, "system", f"step {step['phase']} timed out\n")
                break
            time.sleep(0.5)
        stdout, stderr = process.communicate(timeout=15)
        self._emit(work, "stdout", stdout)
        self._emit(work, "stderr", stderr)
        return process.returncode if process.returncode is not None else 137, cancelled

    def _artifacts(self, execution: dict[str, Any], workspace: Path) -> list[dict]:
        source = (workspace / "source").resolve()
        items: list[dict] = []
        for configured in execution["context"].get("artifact_paths", []):
            candidate = (source / configured).resolve()
            if source not in candidate.parents and candidate != source:
                continue
            paths = (
                [candidate]
                if candidate.is_file()
                else list(candidate.rglob("*"))
                if candidate.exists()
                else []
            )
            for path in paths:
                if not path.is_file():
                    continue
                digest = hashlib.sha256()
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                items.append(
                    {
                        "path": str(path.relative_to(source)),
                        "size": path.stat().st_size,
                        "sha256": digest.hexdigest(),
                        "uri": None,
                    }
                )
        return items

    def execute(self, work: dict[str, Any]) -> None:
        execution = work["execution"]
        workspace = self._workspace(execution["id"])
        outcome = Outcome()
        artifacts: list[dict] = []
        try:
            self._checkout(execution, workspace)
            self.client.report_state(work, "RUNNING")
            test_failed = False
            for step in execution["context"]["recipe"]:
                if step["phase"] == "cleanup":
                    continue
                exit_code, cancelled = self._run_step(work, workspace, step)
                outcome.exit_code = exit_code
                if cancelled:
                    outcome.cancelled = True
                    break
                if exit_code and not step["continue_on_failure"]:
                    test_failed = step["phase"] == "test"
                    outcome.failure_reason = f"{step['phase']} step exited with code {exit_code}"
                    break
            artifacts = self._artifacts(execution, workspace)
            self.client.report_state(work, "FINALIZING")
            if outcome.cancelled:
                outcome.result_kind = "CANCELLED"
            elif outcome.exit_code == 0 and not outcome.failure_reason:
                outcome.result_kind = "PASSED"
            elif test_failed:
                outcome.result_kind = "TEST_FAILED"
            else:
                outcome.result_kind = "INFRA_FAILED"
        except Exception as exc:
            outcome.result_kind = "INFRA_FAILED"
            outcome.failure_reason = str(exc)[:4000]
            try:
                if execution.get("status") == "ASSIGNED":
                    self.client.report_state(work, "RUNNING")
                self.client.report_state(work, "FINALIZING")
            except Exception:
                pass
        finally:
            cleanup_ok = True
            for step in execution["context"].get("recipe", []):
                if step["phase"] != "cleanup":
                    continue
                try:
                    exit_code, _ = self._run_step(work, workspace, step, honor_cancel=False)
                    cleanup_ok = cleanup_ok and exit_code == 0
                except Exception as exc:
                    cleanup_ok = False
                    self._emit(work, "system", f"cleanup failed: {exc}\n")
            outcome.cleanup_complete = cleanup_ok
            try:
                self.client.finalize(work, outcome, artifacts)
            finally:
                shutil.rmtree(workspace, ignore_errors=True)


def serve(settings: AgentSettings | None = None) -> None:
    settings = settings or AgentSettings()
    if shutil.which("docker") is None:
        raise RuntimeError("Docker CLI is required by the TestForge agent")
    client = ControlClient(settings)
    client.register()
    runner = DockerRunner(settings, client)
    while True:
        try:
            work = client.claim()
            if work:
                runner.execute(work)
            else:
                time.sleep(settings.poll_seconds)
        except httpx.HTTPError as exc:
            print(f"control plane unavailable: {exc}", flush=True)
            time.sleep(min(settings.poll_seconds * 5, 30))


def run() -> None:
    serve()


if __name__ == "__main__":
    run()
