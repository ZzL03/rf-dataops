# 阶段一交付说明

## 范围与验收

本阶段只完成架构与初始化，不进入接入、调度、模型、审核和前端编码。
验收要求为 API 可启动、真实 PostgreSQL 连接正常、迁移可执行、健康检查正常。
实际执行结果以 TESTING.md 为准；环境失败或跳过不得当作通过。

## 交付与重点阅读

| 文件 | 需要理解的内容 |
|---|---|
| README.md | 容器和本机两种启动方法、当前范围 |
| docs/REQUIREMENTS.md | 15 天边界、输入协议、需要你确认的假设 |
| docs/ARCHITECTURE.md | 组件职责、Session 生命周期、事务外计算 |
| docs/DATABASE.md | 目标 ER、身份唯一约束、人工版本与快照不变量 |
| docs/TASK_SCHEDULER.md | 领取 SQL、租约、执行版本、幂等与故障矩阵 |
| backend/app/main.py | factory/lifespan，无导入时数据库连接或 DDL |
| backend/app/core/config.py | SecretStr、校验、URL.create 正确处理密码特殊字符 |
| backend/app/db/session.py | 有界连接池、READ COMMITTED、每事务 Session |
| backend/app/models/user.py | 类型、约束及当前真实表结构 |
| backend/alembic/versions/0001_users.py | 可审查、可回退的首个 DDL 迁移 |
| backend/app/api/health.py | 进程与依赖就绪分离、数据库错误不泄密 |
| backend/tests/test_database.py | 真实提交/回滚、约束、迁移回退与 schema drift 检查 |
| docker-compose.yml | 数据库健康依赖、迁移一次性进程与 API 非 root 镜像 |

仓库使用明确的 `git init --initial-branch=main` 初始化；不擅自设定你的 Git 作者身份，
不创建远程仓库，不推送。是否生成首个 commit 见测试记录的实际说明。

## 第一阶段技术解释

- 不使用迁移而启动时 create_all：无法审查结构演进，也无法确认数据库与代码版本一致。
  首次迁移建 users，readiness 对照预期版本；`alembic check` 验证模型与数据库无漂移。
- 不显式控制 Session 生命周期：容易连接泄漏、失败未回滚或跨线程共享状态。
  工厂为每次业务事务创建 Session，异常自动回滚；测试在 flush 后故障注入验证回滚。
- 不区分 live/ready：数据库不可用会误触进程重启或错误接流量。
  当前 live 无依赖，ready 同时检查连接和 schema，迁移缺失返回 503。
- 用户名唯一性仅靠“先查再插”：并发时仍可能重复。数据库 UNIQUE 是最终裁决，
  CHECK 防止大小写和非法角色绕过服务校验；直接 INSERT 的测试验证它们。

这四项已有代码和测试。行锁领取、租约和数据集快照目前仅为设计，后续必须有独立实现与实测。

## 等待你确认的默认选择

1. 采集端完成协议默认 `.part → 原子重命名 → .done`；BIN 格式显式配置，HDF5 显式 dataset。
2. 同一路径/内容/解释参数去重；跨路径不自动合并。旧路径内容改变后旧资产报告完整性失效。
3. 发布时 Manifest 固定为 PostgreSQL 文本字节，下载导出，不预写磁盘文件。
4. 单机受控存储、Mock 先行、三个简单角色；先保核心正确性，再扩大容量和 UI。

这些假设已用于设计，可在进入阶段二前修改，不涉及现有业务数据迁移。

## 下一阶段

你确认后，先实现显式 IQ 元数据 schemas 与 processing_configs/raw_files/signal_segments 迁移，
再实现安全路径解析、BIN/HDF5 区间读取、完成标记与 SHA-256 登记、查询/预览 API 和扫描 CLI。
用合成小型 IQ 数据跑重扫、变更、未完成、无效元数据、路径逃逸及区间读取测试。
完整异步扫描 HTTP 随阶段三的真实队列启用，避免用 FastAPI BackgroundTasks 冒充可靠任务系统。
