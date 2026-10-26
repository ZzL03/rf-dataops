# 当前 API 契约（阶段五）

实际可用路由以 http://127.0.0.1:8000/docs 为准；工作台及同源代理地址为 http://127.0.0.1:18080。
数据库 head 为 `0005_datasets`。时间为 UTC ISO 8601，业务分页为 `{items,total,limit,offset}`，
limit 1–100、默认20，offset>=0。非法 UUID/类型/额外字段为422，校验错误不回显 input。

## 登录与权限

| 方法与路径 | 权限 / 返回 |
|---|---|
| GET /health/live | 公开；200 进程存活 |
| GET /health/ready | 公开；200 真实数据库可用且schema=0005_datasets，否则503 |
| GET /docs、/redoc、/openapi.json | 公开接口文档 |
| POST /api/v1/auth/token | 公开 JSON 登录；200 access_token、token_type=bearer、expires_in |
| GET /api/v1/auth/me | 登录；id、username、role |
| POST /api/v1/auth/logout | 登录；204，当前会话撤销 |

```json
{"username":"demo_reviewer","password":"从本地私有账号文件获取"}
```

所有业务接口现在要求 `Authorization: Bearer <access_token>`；RF_ADMIN_TOKEN 静态值已停用。
登录会话默认30分钟；过期、禁用账号、无效会话为401，角色不足403。Swagger点Authorize填登录后取得的
access_token。账号通过可信本机管理CLI创建，无公开注册。密码和登录限流边界见PHASE_4.md。
用户名规范化后每60秒最多5次登录尝试（包括成功），跨进程计数；超出为429。

## 资产及预览（所有已登录角色可读）

| 方法与路径 | 参数与结果 |
|---|---|
| GET /api/v1/files | limit、offset、status? ACTIVE/STALE/INVALID、format? BIN/HDF5、q? 相对路径包含匹配 |
| GET /api/v1/files/{id} | 修订身份、元数据、SHA-256、segment_count |
| GET /api/v1/files/{id}/observations | limit、offset；追加接入历史 |
| GET /api/v1/segments | limit、offset、raw_file_id?、config_id? |
| GET /api/v1/segments/{id} | 区间与固定 processing_config |
| GET /api/v1/segments/{id}/preview | max_points 32–2048，默认512；I/Q、频率偏移、相对功率谱和统计 |
| GET /api/v1/segments/{id}/quality | items，含quality-v1算法、指标及来源任务 |

源非ACTIVE、完成标记/元数据变化、读取时stat变化等返回409；预览是便捷检查，不替代完整SHA-256。
对象不在当前root_key内按404处理，服务器不接受客户端任意绝对路径。

## 任务（已登录读；ADMIN写，RESEARCHER另可发布/重发PUBLISH）

| 方法与路径 | 返回 / 语义 |
|---|---|
| POST /api/v1/scans | 202，SCAN便捷入口 |
| POST /api/v1/jobs | 202，类型为SCAN、SEGMENT、QUALITY、INFER、PUBLISH |
| GET /api/v1/jobs | 分页；status?、job_type? |
| GET /api/v1/jobs/{id} | 状态、attempts、版本、租约、last_error、result |
| GET /api/v1/jobs/{id}/events | 分页追加历史；HTTP提交的origin记录user UUID |
| POST /api/v1/jobs/{id}/reruns | 202，仅最新FAILED可新建generation；JSON {} 或max_attempts；研究人员限PUBLISH |

所有任务POST要求8–128字符 `Idempotency-Key`（ASCII字母、数字、`_.-`）。同键同解析请求复用任务，
同键异体409；非法键或头长度均为422。成功含 `Location: /api/v1/jobs/{id}`。202只表示持久化，不表示
处理已完成。需查询SUCCEEDED或FAILED；无Worker时任务保持排队。

```json
{"job_type":"SCAN","directory":"demo-phase3","priority":0,"max_attempts":3}
```

```json
{"job_type":"SEGMENT","raw_file_id":"实际文件UUID","config":{"window_samples":256,"stride_samples":128},"start":0}
```

```json
{"job_type":"QUALITY","segment_id":"实际片段UUID"}
```

```json
{"job_type":"INFER","segment_id":"实际片段UUID","model_version_id":"实际模型版本UUID"}
```

调度参数priority=-100..100，max_attempts=1..10、默认3（含第一次）。SCAN新请求键产生新观察任务；
SEGMENT/QUALITY/INFER按规范业务身份复用最近generation。模型版本不同产生新预测，不覆盖旧版本。
请求幂等检查保留原表达形式，等效stride/overlap的不同表达用同一请求键仍可能409。
SCAN SUCCEEDED仍需检查result.errors和skipped，不能解释为所有文件接入成功。
单次扫描至多100路径、分段至多1000片段，超限需调用者手动按目录或start/end拆分。

## 模型与人工审核

| 方法与路径 | 权限 / 语义 |
|---|---|
| GET /api/v1/models | 已登录；分页模型版本，config不返回本地权重路径 |
| POST /api/v1/models | ADMIN；JSON name、version，登记Mock版本，201；等内容可重放、异内容同版本409 |
| GET /api/v1/segments/{id}/predictions | 已登录；分页预测，含模型版本、分数、is_mock和来源job |
| GET /api/v1/reviews | 已登录；分页、status? PENDING/REVIEWED/UNKNOWN、max_confidence? 0..1 |
| GET /api/v1/reviews/{id} | 已登录；当前人工版本、状态及current_annotation |
| GET /api/v1/reviews/{id}/annotations | 已登录；分页，版本倒序，含操作者UUID、用户名和时间 |
| POST /api/v1/reviews/{id}/annotations | REVIEWER/ADMIN；201追加新人工记录，版本冲突409 |

列表按可用预测的最低分数、创建时间、UUID排序。分数排序只是审核提示。所有模型预测与人工结论
分别保存。每段首次成功推理创建PENDING v0；后续新预测不会改变已有人工状态。

```json
{"expected_version":0,"label":"QPSK","action":"CORRECT","comment":"人工查看后修改","prediction_id":"参考预测UUID"}
```

prediction_id可省略；CONFIRM必须指定同一片段的预测且label与预测一致。UNKNOWN必须同时使用
label=UNKNOWN和action=UNKNOWN。其他可选类别：BPSK、QPSK、8PSK、16QAM、64QAM；备注最多2000字符。
客户端不可传reviewer_id；它由登录身份确定。提交只对expected_version执行CAS，不自动覆盖冲突。

```json
{"error":{"code":"REVIEW_VERSION_CONFLICT","message":"Review changed; current version is 1. Reload before deciding.","details":{}}}
```

前端收到该409保留草稿、禁止重复提交，用户显式重载后再决定。错误不包含密码、连接串或绝对文件路径。
Mock的is_mock=true和模型adapter=mock会同时保留并展示，不能把分数当成真实模型准确率或人工真值。
真实TorchScript注册仅通过可信本机CLI，合同与未验证项见PHASE_4.md。

## 数据集与不可变版本（已实现）

| 方法与路径 | 权限 / 返回 |
|---|---|
| POST /api/v1/datasets | RESEARCHER/ADMIN；name、description，201；当前root重名409 |
| GET /api/v1/datasets | 已登录；分页，含version_count |
| GET /api/v1/datasets/{id} | 已登录；基础信息 |
| POST /api/v1/datasets/{id}/selection-preview | RESEARCHER/ADMIN；选择预览、摘要、类别/来源统计和前20样例 |
| POST /api/v1/datasets/{id}/versions | RESEARCHER/ADMIN；202真实PUBLISH任务，要求Idempotency-Key |
| GET /api/v1/datasets/{id}/versions | 已登录；分页，仅已封存版本 |
| GET /api/v1/dataset-versions/{id} | 已登录；固定版本、哈希、统计 |
| GET /api/v1/dataset-versions/{id}/items | 已登录；分页固定成员，按segment UUID排序 |
| GET /api/v1/dataset-versions/{id}/manifest | 已登录；确切历史UTF-8字节、attachment、ETag、X-Manifest-SHA256 |

预览请求：

```json
{
  "selection":{"raw_file_ids":["实际文件UUID"],"segment_ids":[],"labels":[],"review_status":"REVIEWED"},
  "label_policy":"HUMAN_ONLY",
  "model_version_id":null,
  "allow_mock_predictions":false
}
```

ID列表不选表示不限制，对多个筛选条件取交集；全部受root_key隔离。labels空表示全部类别。
review_status可为REVIEWED、UNKNOWN、PENDING或null（全部），默认REVIEWED；无审核任务也按PENDING。
默认只使用人工记录。MODEL_ALLOWED需指定模型版本，人工结论包括UNKNOWN始终优先；直接采用Mock预测
还须allow_mock_predictions=true，否则422 MOCK_ACK_REQUIRED。该字段不自动把Mock变成真实识别结果。

预览返回item_count、file_count、selection_digest、label_distribution、source_counts、
contains_mock_predictions、items（最多20）及truncated。预览只做数据库筛选，完整源校验在发布Worker。
无合格成员为422 EMPTY_SELECTION，不会创建空版本。

发布请求在相同预览请求上添加：

```json
{"version":"v1.0.0","expected_selection_digest":"预览返回的64位摘要"}
```

完整请求须同时包含筛选与来源策略。202表示入队，Location指向job详情；SUCCEEDED.result含
数据集版本UUID、样本数、清单哈希及统计。版本名1–64位字母/数字/`_.-`，首位字母/数字。
同请求键重放返回原任务；已存在版本的新请求为409 VERSION_EXISTS；两个并发发布同名版本只允许一个成功。
待处理同参数请求可能复用逻辑任务；同版本不同参数竞争最终由数据库唯一性裁决。

预览后标签/成员变化使后台任务FAILED，last_error含SELECTION_CHANGED；需要重新预览并提交新请求。
源非ACTIVE、完整哈希不符、读期间变化等也会拒绝，且无部分版本。REPEATABLE READ建立一致视图后
发生的后续审核不会混入本版，成员/清单/成功状态同时提交。所有读取只返回当前root下已封存版本。

单版上限10000成员、100源、16MiB Manifest；超限缩小选择。版本和成员没有修改/删除接口；
封存后数据库也拒绝版本UPDATE/DELETE及成员INSERT/UPDATE/DELETE。Manifest下载不重读源文件，
因此源失踪后仍能下载历史元数据；不等于仍能读取源IQ，也不是IQ导出压缩包。
