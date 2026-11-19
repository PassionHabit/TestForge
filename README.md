# TestForge

统一自动化测试执行平台。当前版本实现了设计文档中 M0（基础契约）和 M1（最小端到端闭环）的可运行基线。

## 已实现

- 项目草稿与不可变发布版本，发布时生成 SHA-256 快照哈希。
- 不可变执行单，固化源码 revision、镜像 digest、配方、参数与资源需求。
- `QUEUED → PREPARING → ASSIGNED → RUNNING → FINALIZING → 终态` 状态机。
- Agent 注册、心跳、资源/标签匹配、并发限制和带代次的租约领取。
- 幂等提交、过期租约对账、取消、分段日志、原子产物清单封存。
- Docker 执行 Agent：默认非特权、禁止 Docker Socket、独立工作目录、优雅取消后强制终止。
- OpenAPI、健康检查、Docker Compose 本地环境与 pytest 契约测试。

## 快速开始

需要 Python 3.11+。

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
alembic upgrade head
testforge-api
```

服务默认使用 `./testforge.db`，OpenAPI 位于 <http://127.0.0.1:8000/docs>。

启动 Agent（需要本机 Docker）：

```bash
TESTFORGE_AGENT_NAME=local-agent testforge-agent
```

也可使用 PostgreSQL 一键启动：

```bash
docker compose up --build
```

## 开发

```bash
pytest
ruff check .
```

详细 API 示例和实现边界见 [docs/getting-started.md](docs/getting-started.md) 与 [docs/architecture.md](docs/architecture.md)。
