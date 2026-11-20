# 运维与验收

## 生产配置

- PostgreSQL 是多副本控制面的必选项；SQLite 仅用于本地开发。
- 在启动 API 前执行 `alembic upgrade head`。
- 分别生成高强度 `TESTFORGE_CONTROL_API_KEY` 和 `TESTFORGE_AGENT_API_KEY`。部署平台应将它们放入 Secret Manager，不写入镜像或 Git。
- 多 API 副本可同时运行维护循环；数据库行锁保护调度和 Outbox 领取。如果使用外部定时系统，可关闭 `TESTFORGE_ENABLE_BACKGROUND_MAINTENANCE`，定时调用 `/admin/reconcile` 和 `/admin/deliveries/dispatch`。
- 将 `TESTFORGE_ARTIFACT_ROOT` 放在耐久卷。大规模部署应使用现场对象存储适配器，保留 `artifact://execution/path` 逻辑地址契约。
- Docker Agent 需要访问宿主 Docker Engine，但测试容器永不挂载 Docker Socket。Native Agent 必须使用独立主机、`executor_pool=native` 标签和显式开关。

## 监控

- 存活：`GET /api/v1/health`
- 就绪：`GET /api/v1/ready`
- Prometheus：`GET /api/v1/metrics`
- 审计：`GET /api/v1/admin/audit`

建议对以下情况告警：`RECONCILING` 停留、Outbox `DEAD_LETTER`、公共服务 cleanup 失败、Agent 心跳过期、排队时间和产物上传错误。

## 发布前验收

1. 分别用 Robot+Headless Chrome、Pytest、JMeter 和 GTest 真实工程固定源码与镜像 Digest。
2. 从 50 并发逐步提高到 200，记录 CPU、内存、镜像拉取、浏览器峰值和产物带宽；不使用固定的“单机任务数”假设。
3. 在 `PREPARING`、`RUNNING` 和 `FINALIZING` 分别重启 API 与 Agent，确认不产生重复容器。
4. 断开 Agent 网络超过租约时间，确认进入 `RECONCILING`，原容器只能被更高 generation 接管。
5. 在测试和上传期间取消，确认进程组/容器终止、产物封存、公共服务清理。
6. 注入公共服务、产物存储和分析回调故障，确认幂等重试、指数退避、告警和人工解决路径。
7. 确认历史记录能唯一定位 project/service release、source revision、image digest、租约 generation 和 artifact manifest hash。
