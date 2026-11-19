# API 使用示例

## 1. 创建并发布项目

```bash
curl -sS http://127.0.0.1:8000/api/v1/projects \
  -H 'content-type: application/json' \
  -d '{
    "name": "pytest-demo",
    "repository_url": "https://github.com/example/pytest-demo.git",
    "framework": "pytest",
    "owner": "qa@example.com",
    "concurrency_limit": 2,
    "draft_config": {
      "image_digest": "python@sha256:REPLACE_WITH_REAL_DIGEST",
      "recipe": [{"phase":"test","command":["pytest","-q"],"timeout_seconds":3600}],
      "required_labels": {"os":"linux"},
      "artifact_paths": ["junit.xml"]
    }
  }'
```

取得返回的 `id` 后发布：

```bash
curl -sS -X POST http://127.0.0.1:8000/api/v1/projects/PROJECT_ID/releases
```

## 2. 提交执行

`idempotency_key` 用于防止流水线重试时重复创建任务。

```bash
curl -sS http://127.0.0.1:8000/api/v1/executions \
  -H 'content-type: application/json' \
  -d '{
    "project_id":"PROJECT_ID",
    "source_revision":"FULL_GIT_COMMIT_SHA",
    "parameters":{"environment":"staging"},
    "idempotency_key":"pipeline-123-job-456"
  }'
```

## 3. 查询、取消和日志

```bash
curl -sS http://127.0.0.1:8000/api/v1/executions/EXECUTION_ID
curl -sS -X POST http://127.0.0.1:8000/api/v1/executions/EXECUTION_ID/cancel
curl -sS 'http://127.0.0.1:8000/api/v1/executions/EXECUTION_ID/logs?after_sequence=-1'
curl -sS http://127.0.0.1:8000/api/v1/executions/EXECUTION_ID/artifacts
```

## 运维对账

`POST /api/v1/admin/reconcile` 会将过期租约标记为 `EXPIRED`，并将相关运行任务转入 `RECONCILING`。生产中应使用定时任务调用，并在重新排队前检查 Docker 容器的 `testforge.execution_id` 和 `testforge.lease_generation` 标签。
