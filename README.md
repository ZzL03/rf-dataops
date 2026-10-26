# RF-DataOps · 电磁信号数据治理与标注管理平台

面向校招工程实践的模块化单体：FastAPI + SQLAlchemy 2 + PostgreSQL + 独立 Worker + Vue。
业务目标为“原始 IQ 文件接入 → 区间分段 → 可靠处理 → 模型预标注 → 人工审核 → 数据集快照”。

**五个阶段的核心MVP已实现。** 包括IQ接入、区间索引/预览、可靠PostgreSQL队列、多Worker、
登录/三角色、Mock预标注、人工审核历史/冲突保护，以及不可变数据集与Manifest。
Vue工作台支持资产接入、片段/预测、人工审核、后台任务、数据集发布与清单下载。
SCAN / SEGMENT / QUALITY / INFER / PUBLISH由独立Worker执行。
真实PyTorch适配合同已保留；尚无真实权重、准确率或生产容量验证。

第一次使用请先阅读 [功能总览与使用手册](docs/USER_GUIDE.md)：第1节用简单语言解释平台用途，第4.1节提供逐按钮的操作与成功判断，第8.1节说明原始HetRF-Mix的接入限制，第8.2节可手动扫描已准备好的20条兼容样本；另含页面截图、备份和VS Code开发方法。

## 项目位置与参考资料

本仓库在 ChatGPT 项目镜像的 `rf-dataops/` 子目录中；外层 `sources/` 为只读参考资料，未修改。
此目录单独初始化 Git，真实 IQ、凭据、虚拟环境和生成物均被忽略。

## 快速启动：Docker Compose

推荐在仓库根目录执行（需要Python 3.11+及已运行的Docker Linux引擎）：

```powershell
python scripts/quickstart.py
# 可选：同时跑一批独立合成IQ，从接入一直演示到两个固定版本：
python scripts/quickstart.py --demo
```

脚本复用/准备.venv、按锁安装依赖、初始化缺失的私有配置与演示账号，启动迁移/API/两个Worker/前端。
不覆盖已有配置或账号，不删除开发数据。首次安装需要网络。
工作台为 [http://127.0.0.1:18080](http://127.0.0.1:18080)，
密码见本地 `artifacts/demo-accounts.json`。演示步骤和已验证边界见 [PHASE_5.md](docs/PHASE_5.md)。

以下是手工启动方式：

需要 Python 3.11+（生成本地配置）和已运行的 Docker Engine / Docker Desktop Linux 引擎。
在本仓库根目录运行：

```powershell
# 仅全新环境且没有 .env 时运行：
python scripts/init_env.py
docker compose up --build -d --wait --scale worker=2 frontend api worker
docker compose ps -a
curl.exe http://127.0.0.1:8000/health/live
curl.exe http://127.0.0.1:8000/health/ready
```

`init_env.py` 生成随机数据库密码；已有 `.env` 时拒绝覆盖。也可复制 `.env.example`，
同时修改 `POSTGRES_PASSWORD` 与 `RF_DATABASE_PASSWORD`，两个数据库密码需一致。
默认数据库端口55432、API端口8000、工作台18080，均绑定本机回环地址。

访问 [OpenAPI 文档](http://127.0.0.1:8000/docs)。`live` 不依赖数据库，`ready` 检查真实连接和
迁移版本 `0005_datasets`。Compose顺序为数据库健康 → 迁移成功 → API / Worker → 工作台。
数据库失联时 API 可以存活，但 readiness 返回 503；不会在 API 启动时自动建表。

```powershell
docker compose logs --tail 100 api worker migrate db
docker compose stop
```

`stop` 保留本地数据卷。修改 `.env` 的数据库密码不会自动修改已有数据卷中的角色密码。
不要为了处理连接错误直接删除有用的数据卷。

## 本机开发

工作台地址为 [http://127.0.0.1:18080](http://127.0.0.1:18080)。
本次已创建的演示账号密码见本地 `artifacts/demo-accounts.json`，不应提交或分享。
全新环境需在安装下面依赖后运行：

```powershell
.\.venv\Scripts\python.exe scripts/dev.py admin demo-accounts --output ../artifacts/demo-accounts.json
.\.venv\Scripts\python.exe scripts/dev.py admin mock-model
```

重复创建会拒绝覆盖。登录、页面操作、个人账号与真实模型接入见 [PHASE_4.md](docs/PHASE_4.md)。
RF_ADMIN_TOKEN 静态令牌已停用，业务读取也需登录；HTTP 客户端需改用 `/api/v1/auth/token` 会话。

在仓库根目录运行。数据库可由 Compose 提供，API 在本机独立运行。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend/requirements.lock
.\.venv\Scripts\python.exe -m pip install --no-deps -e ./backend
# 首次运行且还没有 .env 时执行：
python scripts/init_env.py
docker compose up -d --wait db
.\.venv\Scripts\python.exe scripts/dev.py migrate
.\.venv\Scripts\python.exe scripts/dev.py current
.\.venv\Scripts\python.exe scripts/dev.py api
```

Linux 对应虚拟环境解释器为 `.venv/bin/python`。`scripts/dev.py` 从仓库根加载 `.env`，
切换到 backend 后执行命令，避免 Alembic 和 Uvicorn 的工作目录差异。
如同时运行容器 API 和本机 API，请停掉其中一个或调整 `API_PORT`。
另开 VS Code 终端运行 `.\.venv\Scripts\python.exe scripts/dev.py worker`。
仅修改本机源码不会刷新运行中的容器；容器开发时重新执行前面的 `up --build` 命令。
前端本机开发：`cd frontend` → `npm ci` → `npm run dev`，访问5173端口，同源代理到8000 API。

## 测试与检查

```powershell
.\.venv\Scripts\python.exe scripts/dev.py lint
.\.venv\Scripts\python.exe scripts/dev.py test -m 'not integration'
# 明确启用真实 PostgreSQL 测试：
$env:RF_TEST_DATABASE_NAME = 'rf_dataops_test'
.\.venv\Scripts\python.exe scripts/dev.py test
.\.venv\Scripts\python.exe scripts/dev.py check
```

测试为每次运行创建带随机后缀的数据库，并在结束后删除；需要本地开发数据库用户具有
`CREATEDB` 权限。不会迁移或清空开发数据库。不设置测试开关时集成测试明确显示 skipped。

也可在容器中执行（该测试容器只操作测试库）：

```powershell
docker compose --profile test run --build --rm tests
```

完整验证记录及未验证项见 [TESTING.md](docs/TESTING.md)。依赖范围在 `pyproject.toml`，
此次实际安装的精确版本在 `requirements.lock`；当前锁含开发工具以简化 MVP 镜像。
前端构建为 `npm --prefix frontend run build`；实际浏览器测试在 frontend 中运行 `npm run test:e2e`。
浏览器测试需要本地合成数据目录、演示账号、API和Worker，会新建独立小型合成输入及审核记录；
默认使用已安装的 Edge，凭据只从被忽略文件读取。
测试涵盖网页接入到发布/下载、v1在标签改变和发布v2后保持原字节不变，以及多人409草稿保护。

数据库备份与隔离恢复验证：`.\.venv\Scripts\python.exe scripts/backup.py --verify`。
备份为私有 `artifacts/backup-*.dump`，不包含IQ或模型文件，不会覆盖开发库。

## 从合成 IQ 到后台任务

以下命令均在仓库根目录执行，使用前面的本机开发步骤安装依赖并配置 `.env`。
生成器只创建小型合成数据，已有同名目录会拒绝覆盖。

```powershell
.\.venv\Scripts\python.exe scripts/dev.py migrate
.\.venv\Scripts\python.exe scripts/generate_demo.py
.\.venv\Scripts\python.exe scripts/dev.py scan --directory demo-phase2
# 上一步返回任务 UUID。Worker 完成后查询 result.files 获取文件 UUID：
.\.venv\Scripts\python.exe scripts/dev.py jobs --job-id <任务UUID>
.\.venv\Scripts\python.exe scripts/dev.py segment --file-id <文件UUID> --window 256 --stride 128
.\.venv\Scripts\python.exe scripts/dev.py jobs --job-id <分段任务UUID>
.\.venv\Scripts\python.exe scripts/dev.py quality --segment-id <片段UUID>
```

每个源文件旁边需要 `文件名.iq.json` 元数据和 `文件名.done` 完成标记。
元数据样例、安全约定和故障处理见 [阶段二说明](docs/PHASE_2.md)。
CLI 返回 0 表示提交成功，需查询 job.status 判断业务结果；阶段二同步 CLI 的返回语义已改变。
同参数分段复用逻辑任务及片段；新扫描使用新 request-key，便于发现目录新增文件。
扫描 SUCCEEDED 表示扫描流程完成，仍需检查 result.errors / skipped；坏文件不会被伪装成已接入。
分段只建区间引用，不复制片段。`--overlap 0.5` 可替代等效 `--stride`。

启动 API 后，在 `/docs` 调用 `/api/v1/files`、`/api/v1/segments` 和
`/api/v1/segments/{id}/preview`。当前工作台已用 ECharts 展示波形与频谱，并提供 Web 人工审核；操作见 [使用手册](docs/USER_GUIDE.md)。
HTTP 登录、Bearer 会话和 Idempotency-Key 示例见 [API.md](docs/API.md)。

## 文档与学习入口

- [源码审查与项目知识地图（2026-10-26）](docs/KNOWLEDGE_MAP.md)
- [按优先级的源码学习路线](docs/LEARNING_ROADMAP.md)
- [学习进度、薄弱点与待验证问题](docs/LEARNING_PROGRESS.md)
- [功能总览与使用手册（首次使用入口）](docs/USER_GUIDE.md)
- [阶段一交付与后续确认](docs/PHASE_1.md)
- [阶段二交付、接入协议与学习入口](docs/PHASE_2.md)
- [阶段三交付、运行方法与入门讲解](docs/PHASE_3.md)
- [阶段四交付、登录、模型与人工审核](docs/PHASE_4.md)
- [阶段五交付、不可变版本与完整启动/演示](docs/PHASE_5.md)
- [五分钟演示与面试讲解](docs/DEMO_AND_INTERVIEW.md)
- [需求审查、MVP、15 天计划](docs/REQUIREMENTS.md)
- [架构、数据流、事务与技术选择](docs/ARCHITECTURE.md)
- [ER 图、表结构与约束](docs/DATABASE.md)
- [任务状态机、SQL 与故障验证设计](docs/TASK_SCHEDULER.md)
- [当前 API 与后续 API 契约](docs/API.md)
- [测试方法与实际结果](docs/TESTING.md)

建议先阅读 `app/main.py` 的应用生命周期、`app/db/session.py` 的 Session 工厂、
`app/api/health.py` 的就绪检查，以及首个迁移与 `tests/test_database.py` 的真实事务测试。

## 后续阶段

五阶段核心范围已交付。后续可接入真实模型/数据，开展准确率、容量、长期稳定性和生产部署验证；
不把合成数据与Mock演示描述为真实模型效果或互联网生产验收。
