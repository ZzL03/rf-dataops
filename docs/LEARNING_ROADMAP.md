# RF-DataOps 源码学习路线

制定日期：2026-10-26。面向通信专业研究生，目标是通用后端面试、真实调用链定位与独立调试。依据 [源码知识地图][map]，不把未接入的技术当作项目经历。

## 1. 优先级和学习顺序

P0 表示应优先形成项目主干能力；P1 表示在主干理解后补齐业务和工程能力。学习顺序不等于功能重要性的排名：例如固定版本发布很重要，但先学事务和任务完成机制会更容易理解它。

| 顺序 | 模块 | 优先级 | 本模块只围绕的核心问题 |
|---|---|---|---|
| M0 | 架构与完整业务链路 | P0 | 点击提交以后，API、Worker、数据库各做什么？ |
| M1 | HTTP、FastAPI 与依赖 | P0 | 一个请求如何被路由、校验、鉴权并返回？ |
| M2 | SQL、SQLAlchemy 与事务 | P0 | 多次数据库修改怎样一起成功或一起失败？ |
| M3 | 任务领取与并发控制 | P0 | 两个 Worker 如何安全竞争待执行任务？ |
| M4 | 租约、失败恢复、重试和幂等 | P0 | 重复执行与旧 Worker 为什么不会乱写正式结果？ |
| M5 | 文件接入、读取与区间索引 | P1 | 如何准确解释文件、追踪修订并控制读取范围？ |
| M6 | 身份、模型建议与人工审核 | P1 | 模型建议怎样变成人工历史，又怎样防止覆盖？ |
| M7 | 固定版本与一致性发布 | P0，进阶 | 如何保存“实验当时使用的标签和样本”？ |
| M8 | 测试、错误、日志与部署 | P1 | 如何证明行为、定位失败并运行一致的服务？ |

各模块从第一次实验起就使用少量测试工具；M8 是系统复盘工程方法，不是等到最后才学运行测试。暂不安排微服务拆分、Redis/Celery、模型训练、大规模调优或背诵所有源码。

## 2. 每个模块固定的五步节奏

1. **业务理解**：先解释真实问题和协作关系，不急着引入所有技术名词。
2. **源码追踪**：每轮一个完整小环节，给出入口、输入输出、数据库/状态变化及异常分支，不贴大段源码。
3. **原理与取舍**：项目用法 → 原理 → 为什么这样设计 → 不这样做的后果 → 替代方案 → 高频追问。需要 SQL、锁、线程等前置知识时，先讲一个小例子。
4. **动手验证**：先让学习者预测结果，再使用纯测试、临时文件或独立测试库复现；记录实际输出，不伪造成功。没有明确要求不修改核心业务代码或业务数据。
5. **口述与追问**：学习者先用自己的语言讲，导师据回答追问一个问题；指出具体错点并让学习者修正，再提高难度。

一个模块可能需要多轮。读过讲解或回答“明白”都不直接算掌握；必须同时通过业务、调用链、原理取舍、异常处理、实验、口述追问六项验收。未做的项目明确留空，记录在 [进度表][progress]。

## 3. 各模块的目标、入口和验收

### M0：架构与一次任务的双调用链

- **学习目标**：独立说明业务背景，区分原始文件/片段/预测/人工记录/数据集版本；区分 HTTP 提交与 Worker 执行。
- **前置知识**：文件、函数、进程是什么；HTTP 请求/响应和数据库先用简单例子认识，不要求已经会 SQL。
- **关键源码**：[App.vue][app] 的任务提交、[jobs API][jobs-api] 的 `create_job`、[JobQueue][queue] 的 `submit/claim/complete`、[Worker][worker] 的 `run_once`、[Compose][compose]。
- **面试重点**：模块化单体的含义；进程分离为何不是微服务；202/成功状态区别；队列为何不等于自动串起全流程；文件和元数据为什么分别保存。
- **低风险验证计划**：用已有 `test_liveness_and_openapi_without_database` 观察服务路由与响应；对照源码画出“提交链”和“执行链”，不向业务服务提交任务。
- **基本掌握标准**：不看介绍稿，能从“提交扫描”讲到查询结果并定位上述五个入口；说明 Worker 未运行时为什么可能一直排队，以及扫描成功仍可能有坏文件；完成验证并回答一次针对自己表述的追问。

### M1：HTTP、FastAPI 依赖与同步/异步边界

- **学习目标**：跟踪请求参数校验、Depends、认证上下文、Session 生命周期、业务异常和响应；准确说明当前使用同步数据库。
- **前置知识**：M0；URL、方法、状态码、JSON、函数调用。线程/协程/阻塞的基础会先补，不默认已掌握 asyncio。
- **关键源码**：[create_app][main]、[get_session / DbSession][assets-api]、[current_user / require_*][auth]、[JobSubmission][schema]、[异常对象][errors]。
- **面试重点**：ASGI 与业务函数的关系；同步 `def` 与 `async def`；依赖注入不是“自动创建所有对象”；401/403/409/422/503；响应校验；为什么不能在 async 函数里直接执行耗时阻塞操作。
- **低风险验证计划**：独立 TestClient/已有健康与校验测试，先预测错误输入是否进入业务函数；只使用临时应用配置，不读取或回显真实密码。异步实验另外演示，不能说仓库用了 AsyncSession。
- **基本掌握标准**：沿一个读取接口解释路由→依赖→查询→序列化；能说清 get_session 的关闭不等于自动提交业务事务，解释至少一种校验失败，并完成预测/验证/口述。

### M2：数据库、ORM、事务、约束和索引

- **学习目标**：能读简单 SELECT/INSERT/UPDATE/JOIN，认识主外键/唯一约束/索引；区分 Engine、连接、Session、事务，理解 `flush` 和 `commit`。
- **前置知识**：M1；SQL 从小表开始讲，随后才引入隔离级别和并发。
- **关键源码**：[build_engine/build_session_factory][db]、[资产模型][asset-model]、[任务模型][job-model]、[annotate][labeling]、[数据库测试][database-test] 和 Alembic 迁移。
- **面试重点**：ACID；READ COMMITTED 与 REPEATABLE READ 的边界；autoflush=False；事务上下文；部分唯一索引；ORM 校验与数据库约束为何都需要；连接池是每进程的资源。
- **低风险验证计划**：阅读 `postgres_database` 夹具后，在随机隔离数据库运行 `test_session_commit_and_rollback`；先预测 flush 后抛异常是否留下记录。禁止直接对业务库执行清表或 downgrade。
- **基本掌握标准**：能画出一笔事务的开始、SQL、flush、提交/回滚；写出一个简单条件更新；解释唯一冲突与回滚并验证；回答“只调用 add 为什么可能还没发 SQL”。

### M3：并发领取与短事务

- **学习目标**：理解 job 状态机前半段，解释多个 Worker 如何竞争；区分进程内锁、数据库行锁和事务级 advisory lock。
- **前置知识**：M2；先用两个人领工单的例子认识竞态，再读 CTE 与锁。
- **关键源码**：[JobQueue.claim/advisory_lock][queue]、[ProcessingJob 索引与约束][job-model]、[Worker.run_once][worker]、[领取测试][jobs-test]。
- **面试重点**：先查后改的竞态；`FOR UPDATE SKIP LOCKED`；CTE + UPDATE RETURNING；锁何时释放；优先级不等于严格公平；为什么不持有领取事务做完整计算。
- **低风险验证计划**：隔离库运行 `test_two_processes_claim_only_one_job` 和 `test_skip_locked_and_priority`；先预测一个高优先级任务被锁时另一个 Worker 会做什么。
- **基本掌握标准**：说清 PENDING/RETRY_WAIT→RUNNING 时哪些字段改变；解释行锁、原子领取和重复计算的边界；定位对应 SQLAlchemy 表达式并完成两进程验证与追问。

### M4：租约、恢复、重试、幂等和结果提交

- **学习目标**：分清请求幂等、逻辑任务去重、计算重试、成功重放和新 generation；说明旧执行为什么不能写入正式结果。
- **前置知识**：M3；时间、重试、事务回滚。先认识“允许工作到某个时间的凭证”再讲租约术语。
- **关键源码**：[JobQueue._conditions/heartbeat/complete/fail/recover/rerun][queue]、[LeaseGuard][worker]、[PreparedWork/prepare_work/apply_work][work]。
- **面试重点**：worker_id 与 execution_version 的区别；数据库时钟；退避上限与次数；不永久重试；最终租约检查；外部副作用与 exactly-once 的限制；成功提交后网络确认丢失怎么办。
- **低风险验证计划**：隔离库运行旧执行拒绝、最终租约到期回滚、重复完成测试；只在隔离测试使用强杀/过期/故障注入，不杀业务 Worker。
- **基本掌握标准**：能说明“先算、后事务写入”的原因；列出正式提交需要匹配的身份与有效租约；解释一种崩溃和一种重复请求，并完成验证。不能把不重复发布夸大成从不重复执行。

### M5：数据接入、文件完整性与区间读取

- **学习目标**：区分元数据、内容哈希、文件修订和片段；理解 dtype/端序/IQ 交织与 HDF5 dataset 路径；识别 TOCTOU 的基本含义。
- **前置知识**：M2/M4；通信背景可用于解释采样，但不默认熟悉操作系统路径/文件描述符。
- **关键源码**：[ControlledRoot][storage]、[inspect_file/read_metadata][ingestion]、[range_count/prepare_segments/persist_segments][segments]、[BinReader/Hdf5Reader][readers]、[小批量转换脚本][hetrf]。
- **面试重点**：流式 SHA-256；大小/mtime 为什么不等于内容身份；完成标记；避免片段复制；路径逃逸；压缩块读取成本；宿主机可改写这一信任边界。
- **低风险验证计划**：先运行纯读取器的端序/区间测试；再在 tmp_path/隔离库模拟撤销 `.done` 或内容变化。不改原始 HetRF 文件；转换保留多标签不等于平台已支持多标签审核。
- **基本掌握标准**：独立解释之前 `.h5` 导入失败的实际原因，不能只说“后缀错了”；算对窗口数量；说清快速预览与完整哈希校验的区别；完成一个异常验证和口述。

### M6：登录会话、预测与人工审核

- **学习目标**：区分密码哈希与会话摘要、认证与授权、模型建议与人工结论；掌握版本号条件更新和历史追加。
- **前置知识**：M1/M2/M4；哈希和枚举先做基础说明，不要求掌握密码学推导。
- **关键源码**：[login/hash_password/issue_session][authentication]、[current_user][auth]、[MockModel/TorchScriptModel][adapters]、[annotate/ensure_review][labeling]、[审核与预测模型][label-model]。
- **面试重点**：随机盐/PBKDF2；不透明令牌与 JWT 的区别；账号级限流的边界；乐观并发控制；当前指针与历史一致；UNKNOWN 与 PENDING；新模型为何不覆盖人工标签。
- **低风险验证计划**：隔离库运行 `test_two_reviewers_conflict_then_append_history`、`test_new_model_prediction_never_overwrites_human_label`；先预测两个 expected_version 相同的提交。真实权重加载不在已验证实验中。
- **基本掌握标准**：解释登录到授权的路径；独立讲清审核更新与插入为什么同事务，以及 409 草稿行为；完成并发验证。认证和审核可分多轮完成，不能一次灌入所有知识。

### M7：固定数据集版本与一致性发布

- **学习目标**：区分当前标签、已发布成员、Manifest 字节与物理源文件；理解两次选择核对、一致视图和数据库封存约束。
- **前置知识**：M2/M4/M5/M6；先掌握外键/事务，再学习 JSONB、触发器和延迟约束。
- **关键源码**：[publish API][dataset-api]、[select_members/selection_digest/prepare_publication/apply_publication][datasets]、[complete][queue]、[rf_guard_dataset_version/rf_guard_dataset_item/rf_require_sealed_version][snapshot]。
- **面试重点**：REPEATABLE READ 为什么在这里显式使用；预览不是数据库长期锁；原子封存；同一版本争用；哈希能证明什么；为什么 Manifest 存数据库可以减少跨资源双写问题。
- **低风险验证计划**：隔离库运行旧版本不变、预览后标签变化、成员插入后回滚、封存后追加拒绝测试；先预测各事务的可见性，不在业务数据库直接篡改版本。
- **基本掌握标准**：能讲出完整发布路径并解释一致视图中的历史标签；说明版本固定但源文件仍可能丢失；完成一个并发/回滚验证并回答相应追问。

### M8：工程验证、异常定位与部署

- **学习目标**：按证据区分单元/集成/浏览器测试，能安全运行一组相关检查；区分进程存活、数据库就绪和业务成功。
- **前置知识**：M0–M7 的核心链路；Docker 镜像、容器、卷、网络逐步补齐。
- **关键源码**：[测试夹具][fixtures]、[异常处理][main]、[health][health]、[Worker 日志][worker]、[Compose][compose]、[备份脚本][backup]、[浏览器用例][browser]。
- **面试重点**：故障注入验证了什么；mock 与真实数据库的区别；日志中的执行身份；构建代码与运行镜像；连接池超时处理缺口；依赖锁；备份恢复为何不等于全环境灾备。
- **低风险验证计划**：读取日志/运行纯测试；随后使用隔离库做回滚或 schema readiness 测试。浏览器端到端会新增开发记录，先说明这一副作用并在用户要求的环境执行；不默认自动重建服务。
- **基本掌握标准**：能从一个失败状态定位到日志、事件、源码和相关测试；提交真实命令输出和解释；给出有限、证据支持的结论，不把历史测试、压测计划或 agent 操作说成自己的经历。

## 4. 小型修改与验收记录的边界

用户明确要求练习修改时，才选择一个有明确回归证据的小任务，例如补一个参数边界、错误映射或纯函数测试；先由学习者描述改动与风险，再实施。未获要求时，以不改核心代码的小实验满足验证部分。

每次记录：日期、模块/环节、学习者自己的表述、具体误解、实验预期、命令与实际结果、追问回答、是否复验。教师讲过不等于学习者掌握；测试通过不等于能解释；每轮结束只选择一个下一步问题。

[map]: C:/Users/39675/.codex/.chatgpt-projects/g-p-6ab891bbc5488191b50279f531d676f2/rf-dataops/docs/KNOWLEDGE_MAP.md
[progress]: C:/Users/39675/.codex/.chatgpt-projects/g-p-6ab891bbc5488191b50279f531d676f2/rf-dataops/docs/LEARNING_PROGRESS.md
[app]: C:/Users/39675/.codex/.chatgpt-projects/g-p-6ab891bbc5488191b50279f531d676f2/rf-dataops/frontend/src/App.vue
[main]: C:/Users/39675/.codex/.chatgpt-projects/g-p-6ab891bbc5488191b50279f531d676f2/rf-dataops/backend/app/main.py
[jobs-api]: C:/Users/39675/.codex/.chatgpt-projects/g-p-6ab891bbc5488191b50279f531d676f2/rf-dataops/backend/app/api/jobs.py
[assets-api]: C:/Users/39675/.codex/.chatgpt-projects/g-p-6ab891bbc5488191b50279f531d676f2/rf-dataops/backend/app/api/assets.py
[auth]: C:/Users/39675/.codex/.chatgpt-projects/g-p-6ab891bbc5488191b50279f531d676f2/rf-dataops/backend/app/api/auth.py
[schema]: C:/Users/39675/.codex/.chatgpt-projects/g-p-6ab891bbc5488191b50279f531d676f2/rf-dataops/backend/app/schemas/jobs.py
[errors]: C:/Users/39675/.codex/.chatgpt-projects/g-p-6ab891bbc5488191b50279f531d676f2/rf-dataops/backend/app/core/errors.py
[db]: C:/Users/39675/.codex/.chatgpt-projects/g-p-6ab891bbc5488191b50279f531d676f2/rf-dataops/backend/app/db/session.py
[queue]: C:/Users/39675/.codex/.chatgpt-projects/g-p-6ab891bbc5488191b50279f531d676f2/rf-dataops/backend/app/services/job_queue.py
[work]: C:/Users/39675/.codex/.chatgpt-projects/g-p-6ab891bbc5488191b50279f531d676f2/rf-dataops/backend/app/services/job_work.py
[worker]: C:/Users/39675/.codex/.chatgpt-projects/g-p-6ab891bbc5488191b50279f531d676f2/rf-dataops/backend/app/workers/runner.py
[asset-model]: C:/Users/39675/.codex/.chatgpt-projects/g-p-6ab891bbc5488191b50279f531d676f2/rf-dataops/backend/app/models/assets.py
[job-model]: C:/Users/39675/.codex/.chatgpt-projects/g-p-6ab891bbc5488191b50279f531d676f2/rf-dataops/backend/app/models/jobs.py
[label-model]: C:/Users/39675/.codex/.chatgpt-projects/g-p-6ab891bbc5488191b50279f531d676f2/rf-dataops/backend/app/models/labeling.py
[labeling]: C:/Users/39675/.codex/.chatgpt-projects/g-p-6ab891bbc5488191b50279f531d676f2/rf-dataops/backend/app/services/labeling.py
[authentication]: C:/Users/39675/.codex/.chatgpt-projects/g-p-6ab891bbc5488191b50279f531d676f2/rf-dataops/backend/app/services/authentication.py
[storage]: C:/Users/39675/.codex/.chatgpt-projects/g-p-6ab891bbc5488191b50279f531d676f2/rf-dataops/backend/app/services/storage.py
[ingestion]: C:/Users/39675/.codex/.chatgpt-projects/g-p-6ab891bbc5488191b50279f531d676f2/rf-dataops/backend/app/services/ingestion.py
[segments]: C:/Users/39675/.codex/.chatgpt-projects/g-p-6ab891bbc5488191b50279f531d676f2/rf-dataops/backend/app/services/segments.py
[readers]: C:/Users/39675/.codex/.chatgpt-projects/g-p-6ab891bbc5488191b50279f531d676f2/rf-dataops/backend/app/signal_processing/readers.py
[adapters]: C:/Users/39675/.codex/.chatgpt-projects/g-p-6ab891bbc5488191b50279f531d676f2/rf-dataops/backend/app/signal_processing/models.py
[datasets]: C:/Users/39675/.codex/.chatgpt-projects/g-p-6ab891bbc5488191b50279f531d676f2/rf-dataops/backend/app/services/datasets.py
[dataset-api]: C:/Users/39675/.codex/.chatgpt-projects/g-p-6ab891bbc5488191b50279f531d676f2/rf-dataops/backend/app/api/datasets.py
[snapshot]: C:/Users/39675/.codex/.chatgpt-projects/g-p-6ab891bbc5488191b50279f531d676f2/rf-dataops/backend/alembic/sql/0005_snapshots.sql
[database-test]: C:/Users/39675/.codex/.chatgpt-projects/g-p-6ab891bbc5488191b50279f531d676f2/rf-dataops/backend/tests/test_database.py
[jobs-test]: C:/Users/39675/.codex/.chatgpt-projects/g-p-6ab891bbc5488191b50279f531d676f2/rf-dataops/backend/tests/test_jobs.py
[fixtures]: C:/Users/39675/.codex/.chatgpt-projects/g-p-6ab891bbc5488191b50279f531d676f2/rf-dataops/backend/tests/conftest.py
[compose]: C:/Users/39675/.codex/.chatgpt-projects/g-p-6ab891bbc5488191b50279f531d676f2/rf-dataops/docker-compose.yml
[health]: C:/Users/39675/.codex/.chatgpt-projects/g-p-6ab891bbc5488191b50279f531d676f2/rf-dataops/backend/app/api/health.py
[backup]: C:/Users/39675/.codex/.chatgpt-projects/g-p-6ab891bbc5488191b50279f531d676f2/rf-dataops/scripts/backup.py
[hetrf]: C:/Users/39675/.codex/.chatgpt-projects/g-p-6ab891bbc5488191b50279f531d676f2/rf-dataops/scripts/prepare_hetrf_demo.py
[browser]: C:/Users/39675/.codex/.chatgpt-projects/g-p-6ab891bbc5488191b50279f531d676f2/rf-dataops/frontend/tests/publishing.spec.js
