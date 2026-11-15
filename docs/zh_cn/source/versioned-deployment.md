# 版本化部署（Versioned Deployment）

Gerapy 的项目部署默认仍是**单版本**模式，行为与旧版本完全一致；在此之上新增了可选的
版本化部署能力：不可变版本、部署前健康检查、原子切换、失败自动回滚、版本保留与审计。

## 1. 核心概念

- **不可变版本（ProjectVersion）**：每次构建或上传的 egg 都以内容哈希（sha256）为身份，
  存放在工作区 `versions/<project>/<version>/` 下，包含 `package.egg` 与 `manifest.json`。
  内容不变时重复构建会复用同一版本，绝不覆盖。
- **版本号**：`rel-<14 位时间戳>-<5 位单调序号>-<8 位 uuid>-<8 位哈希>`，定长且按字典序
  与时间序一致。Scrapyd 总是调度字典序最高的版本。
- **暂存版本（staging）**：健康检查使用 `0stg-` 前缀，字典序恒低于任何 `rel-` 正式版本，
  因此暂存 egg 永远不会成为线上活动版本。
- **部署记录（DeploymentRecord）**：每次部署尝试（成功/失败/回滚/拒绝）都落库，记录版本
  差异、健康检查结果、回滚原因、运行中任务策略和操作者，可通过 API 审计查询。

## 2. 原子切换与自动回滚

一次部署严格按下列步骤执行：

1. 获取 `(client, project)` 级别的文件锁（flock），并发部署直接拒绝（HTTP 409）。
2. 预检：Scrapyd 守护进程可达性（端口冲突/进程未启动在此暴露）。
3. 计算与上一活动版本的文件级差异，并做数据兼容性检查（新版本若删除
   `models.py`/`items.py`/`migrations` 等数据定义文件会被拦截，可显式关闭）。
4. 按策略处理运行中任务：`continue`（默认，不中断，Scrapyd 让旧任务跑完）、
   `wait`（等待结束，超时则失败保持旧版本）、`abort`（有运行中任务则拒绝部署）。
5. 以 staging 版本上传 egg，Scrapyd 导入 egg 并枚举爬虫——依赖缺失、代码损坏在此失败，
   staging 被删除，**旧版本始终是最高版本，线上不受影响**。
6. 健康检查通过后，用正式版本号再次上传 egg，这一调用是流量切换的唯一时刻（不复制项目
   目录，仅上传 egg）。
7. 切换后再次校验活动版本与爬虫枚举；失败立即删除新发布版本，自动回到上一版本。
8. 按 `keep_versions` 清理远程与本地产物（默认 1），仍在客户端活动的版本受保护不被清理。

任何失败路径都不会产生“半切换”：旧版本在整个过程中保持可用。

## 3. API

所有接口都需要 Token 认证（`Authorization: Token <token>`）。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET/POST | `/api/project/<name>/versions` | 列出版本 / 从当前工作树构建不可变版本 |
| POST | `/api/project/<name>/versions/upload` | 上传 egg 注册为不可变版本（不切换线上） |
| GET | `/api/project/<name>/versions/diff?from=&to=` | 两个版本的文件差异 |
| POST | `/api/client/<id>/project/<name>/deploy` | 兼容旧接口的部署（默认单版本） |
| POST | `/api/client/<id>/project/<name>/version/<version>/deploy` | 部署指定版本 |
| POST | `/api/client/<id>/project/<name>/version/<version>/rollback` | 回滚到指定版本 |
| GET | `/api/client/<id>/preflight?project=<name>` | 独立健康预检 |
| GET | `/api/deployment/audit` | 审计查询（支持 project/client_id/status/version/operator/reason 过滤、limit/offset 分页） |
| GET | `/api/deployment/audit/<id>` | 单条审计详情（含脱敏后的完整日志） |

部署请求体可选参数：

```json
{
  "keep_versions": 3,
  "running_policy": "continue",
  "expected_spiders": ["sample"],
  "enforce_data_compatibility": true
}
```

## 4. 失败分类

审计记录的 `reason` 字段取值：`build_failed`、`port_conflict`、
`dependency_missing`、`health_check_failed`、`client_unreachable`、
`running_tasks`、`concurrent_deployment`、`data_incompatible`、`unknown`。

## 5. 日志、产物与密钥安全

- 单次部署日志写入 `logs/deploy/deploy-<record_id>.log`，单文件有大小上限并轮转，
  文件总数有保留上限；审计表行数同样有上限，超限只删最旧记录。
- 所有日志与审计文本在写入前经过脱敏：`PASSWORD`/`TOKEN`/`SECRET`/`API_KEY` 等环境变量、
  JSON 凭据字段以及 URL 中的 basic-auth 都会被替换为 `***`。
- `.env`、`*.pem`、`*.key` 等敏感文件只记录哈希，不读取内容进入清单或日志。
- 切换只上传 egg，不复制整份项目目录。

## 6. 配置项（环境变量）

| 变量 | 默认值 | 含义 |
| --- | --- | --- |
| `VERSIONS_FOLDER` | `versions` | 不可变产物目录 |
| `DEPLOY_LOGS_FOLDER` | `logs/deploy` | 部署日志目录 |
| `VERSION_RETENTION_COUNT` | `10` | 本地每项目保留版本数 |
| `DEPLOY_LOG_RETENTION_COUNT` | `1000` | 部署日志文件保留数 |
| `DEPLOY_LOG_MAX_BYTES` | `131072` | 单个部署日志字节上限 |
| `DEPLOY_RECORD_RETENTION_COUNT` | `1000` | 审计表行数上限 |
| `DEPLOY_LOCK_TIMEOUT` | `5` | 获取部署锁的等待秒数 |
| `HEALTH_CHECK_TIMEOUT` | `5` | 健康检查超时秒数 |
| `HEALTH_CHECK_RETRIES` | `2` | 读侧健康检查重试次数 |
