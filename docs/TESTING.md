# 测试、故障注入与实际记录

## 运行方式

从仓库根通过 `scripts/dev.py` 加载 `.env`：

```powershell
.\.venv\Scripts\python.exe scripts/dev.py lint
.\.venv\Scripts\python.exe scripts/dev.py test -m 'not integration'
$env:RF_TEST_DATABASE_NAME = 'rf_dataops_test'
.\.venv\Scripts\python.exe scripts/dev.py test
.\.venv\Scripts\python.exe scripts/dev.py migrate
.\.venv\Scripts\python.exe scripts/dev.py check
```

在 Linux 用 `.venv/bin/python`，用 `export RF_TEST_DATABASE_NAME=rf_dataops_test`。
容器测试：`docker compose --profile test run --build --rm tests`。

## 测试边界

- health/config 测试：无数据库 liveness/OpenAPI、连接拒绝返回 503、密码不出现在响应、
  端口校验、含特殊字符密码的 URL 安全构造。
- integration 测试：使用真实 PostgreSQL；建立随机命名测试库，迁移、降级、升级、重复升级、
  schema drift、正常就绪/未迁移就绪、数据库唯一性/CHECK、Session 成功提交与异常回滚。
- `RF_TEST_DATABASE_NAME` 是明确开关；缺失时会报告 skipped，不视为验收完成。
  测试库只允许 `rf_dataops_test` 前缀，并额外追加随机后缀，清理仅针对本次创建成功的库。
  使用 CREATE DATABASE 需要开发账号有 CREATEDB；生产最小权限账号不应拿来运行该测试。
- 集成测试不会使用 SQLite，也不 mock PostgreSQL 锁语义。阶段一没有 Worker，
  用户事务测试不证明队列领取或故障恢复正确。

## 本次执行记录 · 2026-10-19（北京时间）

环境初查：Windows，Python 3.11.7 可用，Git/Docker/Compose 客户端可用；
Docker 初始未运行。随附 Python 3.12 的 ensurepip 在沙箱临时目录出现 PermissionError；
后续使用本机 Python 虚拟环境，并将安装临时目录限制到项目 `.tmp/`。

沙箱内首次 pip 安装因网络访问被拒绝失败；首次 ASGI TestClient 运行挂起，已终止。
在允许本机依赖安装、Docker 管道与 loopback 的执行环境中重跑成功。Docker 首次拉取
PostgreSQL 镜像出现 registry EOF，重新下载后成功；没有更换为模拟数据库。

| 检查 | 实际结果 |
|---|---|
| Git 初始化 | `main` 分支初始化成功；未创建提交、未设置作者、无远程推送 |
| Compose 配置 | `docker compose config --quiet` 退出 0 |
| Ruff 静态检查 | `All checks passed!` |
| Ruff 格式检查 | `20 files already formatted` |
| Python 依赖检查 | `pip check`：No broken requirements found |
| 离线 DDL | Alembic 输出 BEGIN / users 建表 / 版本插入 / COMMIT |
| 本机 Uvicorn | 127.0.0.1:8001 启动成功；无数据库时 live=200、ready=503 |
| Compose 首次启动 | PostgreSQL healthy；迁移退出 0；API healthy |
| 最终真实 HTTP | 容器 8000 的 live/ready 均 200；临时本机 8001 的 ready 200 |
| 数据库实际版本 | PostgreSQL 17.11 / Debian，开发库仅 users、alembic_version 两张表 |
| 隔离测试清理 | 查询 rf_dataops_test 前缀数据库数量为 0 |
| 容器运行身份 | `id -u` 为 10001 |
| Alembic current | `0001_users (head)` |
| Alembic check | `No new upgrade operations detected.` |
| Windows / Python 3.11.7 单元测试 | `5 passed, 5 deselected, 1 warning in 2.65s` |
| Windows / Python 3.11.7 完整测试 | `10 passed, 1 warning in 3.06s`，无 skipped |
| Linux / Python 3.12 容器完整测试 | `10 passed, 1 warning in 0.65s`，无 skipped |

本机完整测试生成 JUnit XML：`artifacts/pytest-local.xml`，属于被 Git 忽略的本地生成物。
10 项包括：配置 3 项、健康 API 2 项、真实数据库迁移 1 项、事务 1 项、参数化约束 3 项。
完整套件使用随机隔离 PostgreSQL 数据库，并在 fixture 结束时清理。

已知警告：当前锁定的 Starlette 1.9.0 提示 `httpx` TestClient 将弃用，建议未来迁移 httpx2。
现有 FastAPI/httpx 测试已实际通过；未屏蔽此警告，也未将它当作失败。后续升级依赖时统一评估。
容器构建时 pip 以 root 安装依赖，运行时改为 UID 10001；两者职责不同。

Compose 镜像采用补丁浮动标签，依赖锁精确固定 Python 包版本但未包含下载哈希；
不是整个 OS 镜像按位可复现的发布锁。后续正式发布应固定镜像 digest 并补包哈希。

交付时保留 Compose API（8000）与数据库（55432）运行，便于你查看；临时本机 API（8001）
验证后停止。真实密码仅保存在被忽略的 `.env` 中。Git 文件已暂存便于审查，尚无首个提交。

## 第二阶段最终记录 · 2026-10-19（北京时间）

阶段一结果保留为历史基线；阶段二交付时 head 为 `0002_assets`。新增 NumPy 2.4.6、h5py 3.16.0。

| 检查 | 最终实际结果 |
|---|---|
| Windows / Python 3.11.7 完整测试 | `57 passed, 2 skipped, 1 warning in 6.73s` |
| Linux / Python 3.12 最终镜像完整测试 | `59 passed, 1 warning in 1.50s`，无 skipped |
| Ruff / 格式 / pip check | All checks passed；40 files formatted；No broken requirements |
| 开发库迁移与结构检查 | `0001_users -> 0002_assets` 成功；current=head；No new upgrade operations |
| 真实生成与 CLI 接入 | 2 个小型合成源（BIN、HDF5）均登记成功 |
| 真实 CLI 重扫 | registered=0、unchanged=2，两个 UUID 保持一致 |
| 真实 CLI 区间索引 | window=256、stride=128，每文件31片段，共62引用，无片段副本 |
| 最终 Compose | db/api healthy，独立 migrate 退出0 |
| 实际跨系统 HTTP | Windows扫描、Linux API读取两文件均200；各预览64点；RMS约0.99999998 |
| 健康接口 | ready=200，schema=0002_assets |

Windows 跳过原因精确为创建符号链接缺少权限（WinError 1314），其余 OSError 不会被当成跳过。
对应两项在 Linux 容器真实运行且通过，分别覆盖文件链接与父目录链接逃逸。
Starlette 的 httpx TestClient 弃用警告沿用第一阶段，未屏蔽。

覆盖场景：

- BIN 的两种 dtype、两种端序、区间和上限；HDF5 complex64/[N,2] 的 chunked 切片。
- HDF5 外部链接、父路径软链接、外部存储、VDS、过大 Chunk、不支持布局均拒绝。
- 绝对路径、穿越、盘符/ADS、反斜杠、NUL；Linux实际符号链接逃逸。
- 非法采样率/频率/dtype/额外元数据、非法切分配置、未完成文件、异常长度、失踪文件。
- 重扫幂等；同大小同mtime改写的完整哈希检出；解释元数据变化产生新修订；旧内容恢复重新激活。
- 两个真实 PostgreSQL 会话并发登记同一路径，唯一资产和一个 REGISTERED 结果。
- 读后变更与旧观察提交拒绝；完成标记撤销失效；一个路径最多一个 ACTIVE 的数据库约束。
- 两种源文件端到端登记→区间→API→预览；分页/参数/404/409与数据库不可用503。
- 源/片段身份、配置及观察历史被直接SQL修改/删除时拒绝；直接SQL越界插入拒绝。
- 配置插入后、片段提交前故障注入，整个事务回滚；超大批次在写入前拒绝。
- Windows/Linux挂载设备号差异兼容；有界波形/FFT、非有限IQ拒绝与已知单音峰值验证。

构建中的问题与处理：首次 PyPI 文件下载超时导致 Docker build 失败；添加 BuildKit 下载缓存、
60秒超时和有限重试后构建成功。其后一次索引查询短暂 SSL EOF/超时，也在重试后成功。
中间构建或测试结果不替代最终结果；上表列出最终源码及实际运行镜像的验证结果。

联调发现 Windows 和 Linux bind mount 的 device/inode/ctime 不同，预览改为跨环境只比较
size/mtime，在单次读取内比较完整本地 stat。实际 Windows CLI→Linux API 的成功请求验证了修正。
快速预览不等价于完整内容校验，限制详见 PHASE_2.md，未宣称 stat 能保证内容不变。

本地证据（被 Git 忽略）：`artifacts/pytest-phase2-local.xml`、`artifacts/phase2-scan.json`、
`artifacts/phase2-http.json`。合成数据位于 `data/demo-phase2/`，无真实采集或模型识别结果。
第一阶段暂存区未重置，本阶段源码与新文件保留供 VS Code 审查；没有提交或远程推送。

## 第三阶段最终记录 · 2026-10-20（北京时间）

当前迁移 head 为 `0003_jobs`。没有新增 Python 依赖；阶段二的 59 项测试继续作为回归基线，
新增 20 项任务与故障测试（含参数化），共 79 项。

| 检查 | 最终实际结果 |
|---|---|
| Windows / Python 3.11.7 完整测试 | `77 passed, 2 skipped, 1 warning in 12.77s` |
| Linux / Python 3.12 最终镜像完整测试 | `79 passed, 1 warning in 6.43s`，无 skipped |
| Ruff / 格式 / pip check | All checks passed；49 files already formatted；No broken requirements |
| 开发库 Alembic | `0003_jobs (head)`；No new upgrade operations detected |
| 隔离库迁移 | 迁移降级/升级/重复升级及模型漂移检查通过 |
| Compose 最终状态 | db/api healthy，migrate 退出0，两个 Worker 容器持续运行 |
| 真实 HTTP 扫描→分段→质量 | 新增 BIN/HDF5 各一；共62区间、4个质量历史；均由真实 Worker 完成 |
| 真实并行服务 | 两个不同 worker_id 均有成功任务 |
| 真实预览 | 四个片段各64点；RMS 约0.99999998145429 |
| HTTP 门禁与幂等 | 无令牌401；同 key 返回同任务；同 key 异体409；分段新 key 复用逻辑任务 |
| HTTP 手工重发 | 不存在目录任务FAILED；创建空目录后新 generation 成功，旧任务仍FAILED |
| 最终开发库 | 4源、124片段、4质量结果、8成功任务、1预期失败任务；阶段二数据保留 |
| 隔离测试清理 | rf_dataops_test 前缀数据库数量0 |
| 私有文件保护 | .env、合成 IQ、artifacts 报告均被 Git 忽略 |

故障测试使用真实 PostgreSQL 和实际子进程：

- `test_two_processes_claim_only_one_job`：两个 spawn 子进程用 Barrier 同时争一个任务，只有一个令牌。
- `test_skip_locked_and_priority`：另一个 Session 锁住高优先级行，领取跳过它，释放后按优先级领取。
- `test_killed_worker_recovers_and_old_execution_cannot_publish`：真实 Worker 领取后在测试替换的
  计算入口阻塞，父进程收到令牌才强杀；测试 SQL 调过期、恢复再领取，旧令牌心跳/失败/完成均拒绝。
  该强杀测试聚焦领取后的进程丢失；正常 IQ 计算由端到端任务测试和 HTTP 联调另行覆盖。
- `test_heartbeat_renews_lease_and_cannot_revive_expired_token`：真实心跳线程续租，已过期不能复活。
- 次数耗尽覆盖可重试异常和租约过期两条路径；退避覆盖尚未到 next_run_at 无法领取。
- 成功结果重放只有一组业务结果及成功事件，不同摘要冲突；worker_id、execution_version 分别核对。
- `test_result_transaction_rolls_back_all_business_rows`：正式写入后注入异常，配置/片段全部回滚。
- `test_final_lease_fence_rolls_back_already_inserted_rows`：写入后 pg_sleep 使租约过期，最后条件
  UPDATE 拒绝成功并回滚业务结果，覆盖“开始提交时尚有效、提交途中到期”。
- SCAN 计划阶段无正式登记，旧执行不能发布；逐文件错误保留、新请求可再次观察目录。
- QUALITY 持久化真实统计；NaN/Inf 不可重试且不写质量；请求映射、历史、来源及终态直连 SQL 保护。
- HTTP 写令牌、未配置写入关闭、分页、事件、路径逃逸、错误重发和幂等请求冲突。

开发时一次回滚测试因共享 processing_config 已由更早测试创建，断言错误地假定为空而失败；
改用独有窗口参数隔离测试后通过。最终两套完整测试结果均基于修正后的源码。
Windows 两项跳过仍仅为符号链接权限不足，Linux 对应项通过；Starlette 弃用警告保留。
没有加入仅用于演示的生产故障开关，也未把未运行的断网、吞吐压力或长时间稳定性测试列为通过。

证据：`artifacts/pytest-phase3-local.xml`、`artifacts/phase3-http.json` 和本次终端容器测试输出。
`artifacts/check_phase3_http.py` 为本次本地联调脚本，假设 demo-phase3 尚未登记，不作为幂等测试工具。
开发库的1个FAILED是预期的“缺失目录后修复重发”证据，属于保留审计记录。
当前服务保留运行供查看，API地址为 `http://127.0.0.1:8000/docs`。

## 第四阶段最终记录 · 2026-10-20（北京时间）

当前head为0004_labeling。新增11项后端测试，原79项继续回归；没有增加Python运行依赖。

| 检查 | 实际结果 |
|---|---|
| Windows / Python 3.11.7完整套件 | 88 passed, 2 skipped, 1 warning in 21.93s |
| Linux / Python 3.12最终后端镜像 | 90 passed, 1 warning in 9.03s，无跳过 |
| 新增登录/模型/审核测试 | 11 passed；含实际并发HTTP、直接SQL约束及故障回滚 |
| Alembic | 新库完整升级、降级base、重新升级与schema drift通过；开发库head=0004_labeling |
| Ruff / Python格式 / pip check | All checks passed；60 files already formatted；No broken requirements |
| Vite构建 | Windows与Linux镜像均成功；按组件引入并拆分图表chunk后无大包警告 |
| 前端格式 | Prettier检查通过 |
| 真实HTTP（经过Nginx代理） | 三个INFER均SUCCEEDED、is_mock=true；两个Worker都有成功任务 |
| 真实HTTP审核竞争 | 两会话同一expected_version提交，结果201和409；仅一条新历史 |
| 真实HTTP权限/撤销 | 未登录401；RESEARCHER写入403；退出后原token401 |
| 真实浏览器（本机Edge、Playwright） | 1 passed；最终自带独立合成输入版6.7s，无pageerror |
| 最终Compose | API/db/frontend健康；migrate退出0；两个Worker运行；网页18080可访问 |
| 开发库保留演示结果 | 5源、126片段、5个Mock预测、5个审核任务、3条人工演示历史；15成功任务、1原有预期失败 |
| 隔离测试库 | rf_dataops_test前缀数量0 |

新增后端测试覆盖：随机盐密码哈希、登录失败统一响应、共享数据库限流、会话摘要、过期、退出、
账号禁用、三角色读写、非法密码不回显；Mock确定性/分数归一化/NaN拒绝；真实Worker保存预测、
幂等重放忽略浮动耗时；创建PENDING而非自动写人工标签；两审核用户并发冲突并追加后续版本；
新模型预测不覆盖人工结论；旧执行不能发布；预测写入后异常回滚；审核CAS后插入失败回滚；
模型/预测/人工历史直连SQL不可改，当前指针不能单独递增；跨root404；Torch输入合同/缺运行时拒绝。

真实浏览器测试会创建一个独立小型合成BIN（128个采样）、sidecar和done；经HTTP队列扫描/分段后，
在页面选片段提交INFER，等待Worker，打开IQ时域与频谱两张canvas、检查Mock提示，再确认建议。
另一个浏览器上下文事先打开同版本，提交后出现409，验证草稿仍保留、按钮禁用、显式重载丢弃草稿，
并验证审核历史与退出登录。每次生成新输入，因此不依赖上次演示样本仍处于待审核状态。
测试保留不可变业务审计记录，只对data/browser-e2e-*合成输入写文件，不改sources或真实IQ。

本地重跑：在frontend中 `npm ci`、`npm run test:e2e`；需要已运行的API/Worker、相同data挂载、
已安装Edge和私有artifacts/demo-accounts.json。默认URL为http://127.0.0.1:18080，可设RF_E2E_URL。
账号级60秒5次登录限制也约束联调，请勿把密集重复登录导致的429当作队列错误。

证据：artifacts/pytest-phase4-local.xml、phase4-http.json、phase4-review.png；
可重复的浏览器测试源码为frontend/tests/review.spec.js。账号文件与所有证据均被Git忽略。
当前浏览器中的人工类别是对合成数据的演示操作，不是实际调制真值。

构建/联调中发现并修正：Alembic命名约定重复添加CHECK前缀，迁移事务回滚后改为显式op.f命名；
旧健康测试需带Bearer才能验证认证后的数据库失联503；Sites安装helper在Windows找不到npm脚本，
改用本机npm.cmd安装成功；默认8080端口绑定被系统拒绝，改18080并修正容器内部健康检查端口。
内置浏览器工具缺内核资源，改用本机Edge/Playwright完成真实浏览器验证。首版浏览器测试依赖既有
待审核记录，后改为每次独立生成输入以支持重复运行，最终版实际通过。

已知警告：Windows符号链接两项权限跳过（Linux通过），Starlette TestClient弃用；
构建npm对esbuild安装脚本策略给出提示但实际Vite构建通过；Playwright终端色彩环境提示不影响测试。
没有真实模型权重，不安装或下载PyTorch，不宣称真实推理或准确率测试通过；真实模型加载/精度、
长时间运行、互联网安全部署、全尺寸性能与阶段五数据集发布仍未验证。

## 第五阶段交付记录 · 2026-10-20（北京时间）

当前head为0005_datasets。新增22项数据集测试（含参数化），原90项继续回归，总112项。
没有新增Python依赖，前端增加按需加载的资产/任务/数据集组件。

| 检查 | 实际结果 |
|---|---|
| Windows / Python 3.11.7完整后端套件 | 110 passed, 2 skipped, 1 warning in 32.40s |
| Linux / Python 3.12后端镜像完整套件 | 112 passed, 1 warning in 14.80s，无跳过 |
| 迁移/模型一致性 | 隔离库降级base、升级head、重复升级和Alembic drift通过；开发库0005_datasets |
| 数据集并发与故障专项 | 22项通过，见下方；包含原生SQL约束及两个并发会话 |
| Windows/容器前端构建 | Vite成功；三个管理页面按需分块，单chunk低于500kB，无大包警告 |
| Ruff / Python格式 / pip check | All checks passed；69 files formatted；No broken requirements |
| 前端格式 | Prettier通过 |
| 真实浏览器 | publishing.spec.js与review.spec.js均通过；包含真实Edge及两个独立登录上下文 |
| 一键启动及真实HTTP演示 | scripts/quickstart.py --demo退出0；两源、8片段、4质量、4Mock预测、4审核样本及两个固定版本 |
| 演示旧版本不变 | v1固定4个QPSK演示标签；v2为1 BPSK + 3 QPSK；v1下载字节保持原样 |
| 数据库备份/恢复 | scripts/backup.py --verify退出0；真实pg_dump二进制、隔离新库pg_restore、全部Manifest复核通过后清理 |
| 版本读取与下载 | attachment、ETag、X-Manifest-SHA256；浏览器按实际下载字节验证哈希再保存 |
| 最终服务 | frontend/API/db健康；migrate退出0；两个Worker独立运行 |
| 隔离库清理 | rf_dataops_test和rf_dataops_restore前缀数量0 |

新增测试对应以下实际行为：

- 规范UTF-8字节/下载哈希、人工历史ID/人员/时间、源哈希/元数据、区间及处理配置完整追溯。
- 新标签和v2不改变v1；源丢失后历史Manifest仍可下载，不动态拼接当前状态。
- HUMAN_ONLY默认不使用预测；MODEL_ALLOWED指定版本并明确允许Mock；UNKNOWN人工结论优先。
- 空集、边界、预览摘要失效、哈希后再核对选择；同大小/mtime改写仍被完整SHA-256检出。
- REPEATABLE READ：发布事务中另一个会话完成审核，版本仍只用同一个一致视图中的旧人工历史。
- 成员插入后、封存前注入异常，全事务回滚；同令牌重放只一组版本和成员。
- 旧执行拒绝；正式版本已写入后租约到期，最终fence令整批版本/成员回滚。
- 两个发布者争同一版本，只有一个SUCCEEDED；唯一冲突的另一任务FAILED，无半份副本。
- 版本UPDATE/DELETE、成员UPDATE/DELETE/事后INSERT直连SQL均拒绝；第二个连接插入时争父行锁，
  释放锁后仍因封存拒绝；未封存版本不能提交。
- 即便重新计算了伪造清单的SHA-256，Manifest与固定成员不一致也拒绝；错误来源和伪造snapshot拒绝。
- RESEARCHER/ADMIN发布、REVIEWER只读数据集、root隔离404、分页和重复请求键复用。

浏览器最终流程实际点击扫描、分段、质量、Mock预测、人工审核、创建数据集、源文件筛选、预览、
发布v1并下载；再改人工标签、发布v2，核对v1原字节及两个版本。此前的两人409草稿保护仍通过。
两个测试每次生成独立合成输入，保留真实审计记录。模拟标签明确注明不是实测真值。

证据为artifacts/pytest-phase5-local.xml、phase5-demo.json、phase5-demo-manifest.json、
phase5-assets.png、phase5-datasets.png、backup-*.dump，以及终端Linux和浏览器测试输出。
备份含用户哈希及会话摘要，按私有生成物处理；备份不含IQ、模型或.env。
Git暂存区保留阶段一原状态，后续源码/文档供VS Code审查；未提交或推送。

开发问题及处理：首版浏览器定位器同时匹配多选框与listbox，改为唯一combobox；随后发现输入框
被组件自己的placeholder覆盖，改为点击实际选择器外层（与用户点击一致），未用force绕过交互。
完整浏览器流程随后通过。一次Docker Hub镜像元数据请求EOF导致前端重建失败，重试成功；
最终交付以成功构建和运行镜像为准。界面截图回到页面顶部再捕获，避免固定侧栏的滚动位置影响截图。

保留的警告/边界：Windows两项仅为符号链接权限（Linux通过），Starlette TestClient弃用，
Playwright终端色彩变量提示。未宣称真实模型推理/准确率、生产吞吐、长期稳定性、高可用或公网安全
部署通过。数据库快照固定引用，不提供宿主机IQ物理不可变存储；IQ导出包也未实现。

## 分阶段测试状态

| 阶段 | 必须验证 | 当前状态 |
|---|---|---|
| 二 | 重扫去重、变更检出、显式元数据、未完成跳过、路径安全、BIN/HDF5 区间 | 已实现，最终结果见上方 |
| 三 | 并发领取、强杀进程、过期恢复、旧执行拒绝、有限重试、幂等、部分结果回滚 | 已实现，Windows/Linux实际通过，见上方 |
| 四 | Mock 推理、预测不覆盖人工、审核历史、两用户409、登录权限和浏览器操作 | 已实现；90项后端与真实浏览器验证通过 |
| 五 | 原子发布、直接SQL修改/追加拒绝、新旧版本隔离、Manifest追溯、网页完整链路及备份恢复 | 已实现；112项后端与两个浏览器测试通过 |

阶段三用子进程进行 Worker 强杀，使用进程通信确认已领取再 kill；过期测试仅在隔离测试库中
调整租约时间来避免长时间 sleep。生产开发库未注入强杀或过期 SQL。
