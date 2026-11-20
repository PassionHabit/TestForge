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
from urllib.parse import quote

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
    api_key: str | None = None
    allow_native_execution: bool = False

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
        headers = {"Authorization": f"Bearer {settings.api_key}"} if settings.api_key else None
        self.http = httpx.Client(base_url=settings.api_url, timeout=30, headers=headers)
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

    def reconcile(self, runtimes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        assert self.agent_id
        response = self.http.post(f"/agents/{self.agent_id}/reconcile", json={"runtimes": runtimes})
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

    def service_phase(self, work: dict[str, Any], phase: str) -> None:
        execution_id = work["execution"]["id"]
        response = self.http.post(
            f"/executions/{execution_id}/services/start",
            json={**self.lease_payload(work), "phase": phase},
        )
        response.raise_for_status()

    def cleanup_services(self, work: dict[str, Any]) -> bool:
        execution_id = work["execution"]["id"]
        response = self.http.post(
            f"/executions/{execution_id}/services/cleanup",
            json=self.lease_payload(work),
        )
        response.raise_for_status()
        return response.json()["failed"] == 0

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

    def upload_artifact(
        self, work: dict[str, Any], artifact_path: str, local_path: Path
    ) -> dict[str, Any]:
        execution_id = work["execution"]["id"]
        with local_path.open("rb") as handle:
            response = self.http.put(
                f"/executions/{execution_id}/artifact-content/{quote(artifact_path)}",
                content=handle,
                headers={
                    "X-Lease-Token": work["lease_token"],
                    "X-Lease-Generation": str(work["generation"]),
                    "Content-Type": "application/octet-stream",
                },
            )
        response.raise_for_status()
        return response.json()


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
        if execution["context"].get("source_type", "git") == "svn":
            source.rmdir()
            commands = [["svn", "checkout", "-r", revision, repository, str(source)]]
        else:
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
            "--label",
            f"testforge.phase={step['phase']}",
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
        execution = work["execution"]
        native = execution["context"].get("execution_kind") == "native"
        if native and not self.settings.allow_native_execution:
            raise RuntimeError("native execution is disabled on this agent")
        if native:
            command = step["command"]
            container_name = ""
        else:
            command, container_name = self._docker_command(execution, workspace, step)
        environment = os.environ.copy()
        for key, value in execution.get("parameters", {}).items():
            if isinstance(value, (str, int, float, bool)):
                env_name = "TESTFORGE_PARAM_" + "".join(
                    character if character.isalnum() else "_" for character in key.upper()
                )
                environment[env_name] = str(value)
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            cwd=workspace / "source" if native else None,
            env=environment if native else None,
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
                if not native:
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

    def _artifacts(
        self, work: dict[str, Any], execution: dict[str, Any], workspace: Path
    ) -> list[dict]:
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
                relative_path = str(path.relative_to(source))
                uploaded = self.client.upload_artifact(work, relative_path, path)
                if uploaded["sha256"] != digest.hexdigest():
                    raise RuntimeError(f"artifact hash mismatch after upload: {relative_path}")
                items.append(uploaded)
        return items

    def execute(self, work: dict[str, Any]) -> None:
        execution = work["execution"]
        workspace = self._workspace(execution["id"])
        outcome = Outcome()
        artifacts: list[dict] = []
        try:
            self._checkout(execution, workspace)
            self.client.service_phase(work, "before")
            self.client.service_phase(work, "during")
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
            self.client.service_phase(work, "after")
            artifacts = self._artifacts(work, execution, workspace)
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
            try:
                cleanup_ok = self.client.cleanup_services(work) and cleanup_ok
            except Exception as exc:
                cleanup_ok = False
                self._emit(work, "system", f"service cleanup failed: {exc}\n")
            outcome.cleanup_complete = cleanup_ok
            try:
                self.client.finalize(work, outcome, artifacts)
            finally:
                shutil.rmtree(workspace, ignore_errors=True)

    def recover(self, work: dict[str, Any], container_name: str) -> None:
        execution = work["execution"]
        workspace = Path(self.settings.work_root).resolve() / execution["id"]
        outcome = Outcome(result_kind="INFRA_FAILED")
        waiter = subprocess.Popen(
            ["docker", "wait", container_name],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            while waiter.poll() is None:
                if self.client.heartbeat(work):
                    self._stop_container(container_name)
                    outcome.cancelled = True
                    outcome.result_kind = "CANCELLED"
                time.sleep(self.settings.heartbeat_seconds)
            stdout, stderr = waiter.communicate(timeout=5)
            if stderr:
                self._emit(work, "system", stderr)
            try:
                outcome.exit_code = int(stdout.strip().splitlines()[-1])
            except (ValueError, IndexError):
                outcome.exit_code = None
            outcome.failure_reason = (
                "agent restarted while a container was running; the existing container was "
                "adopted without duplicate execution"
            )
            self.client.report_state(work, "FINALIZING")
            artifacts = self._artifacts(work, execution, workspace) if workspace.exists() else []
            outcome.cleanup_complete = self.client.cleanup_services(work)
            self.client.finalize(work, outcome, artifacts)
        finally:
            shutil.rmtree(workspace, ignore_errors=True)


def observed_docker_runtimes() -> list[dict[str, Any]]:
    if shutil.which("docker") is None:
        return []
    result = subprocess.run(
        [
            "docker",
            "ps",
            "--filter",
            "label=testforge.execution_id",
            "--format",
            '{{.Names}}|{{.Label "testforge.execution_id"}}|'
            '{{.Label "testforge.lease_generation"}}',
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode:
        return []
    runtimes = []
    for line in result.stdout.splitlines():
        parts = line.split("|", 2)
        if len(parts) != 3 or not parts[2].isdigit():
            continue
        runtimes.append(
            {
                "container_name": parts[0],
                "execution_id": parts[1],
                "generation": int(parts[2]),
            }
        )
    return runtimes


def serve(settings: AgentSettings | None = None) -> None:
    settings = settings or AgentSettings()
    if shutil.which("docker") is None and not settings.allow_native_execution:
        raise RuntimeError("Docker CLI is required by the TestForge agent")
    client = ControlClient(settings)
    client.register()
    runner = DockerRunner(settings, client)
    observed = observed_docker_runtimes()
    recovered = client.reconcile(observed)
    containers = {runtime["execution_id"]: runtime["container_name"] for runtime in observed}
    for work in recovered:
        container_name = containers.get(work["execution"]["id"])
        if container_name:
            runner.recover(work, container_name)
    last_reconcile = time.monotonic()
    while True:
        try:
            if time.monotonic() - last_reconcile >= settings.heartbeat_seconds:
                observed = observed_docker_runtimes()
                recovered = client.reconcile(observed)
                containers = {
                    runtime["execution_id"]: runtime["container_name"] for runtime in observed
                }
                for work in recovered:
                    container_name = containers.get(work["execution"]["id"])
                    if container_name:
                        runner.recover(work, container_name)
                last_reconcile = time.monotonic()
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
