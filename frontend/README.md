# RF-DataOps 工作台（阶段五）

已使用 Vue 3、Vite、Element Plus、ECharts 和 Axios 实现登录、片段预标注、后台任务查看、
波形/频谱、模型建议、人工审核、历史与409冲突草稿保护。所有业务数据来自实际 API。

Compose 访问 http://127.0.0.1:18080；本机 `npm ci` 后 `npm run dev` 访问5173。
API代理目标为127.0.0.1:8000；令牌仅保留内存，刷新重新登录。
演示随机账号密码位于仓库本地 artifacts/demo-accounts.json。

`npm run build` 构建静态包；`npm run format:check` 检查格式；`npm run test:e2e` 使用实际 Edge、
API和Worker运行完整浏览器测试，并创建独立合成输入与保留的审核记录。
默认凭据路径为../artifacts/demo-accounts.json，URL可由RF_E2E_URL指定；无需下载额外浏览器。
截图写入../artifacts/phase4-review.png，不记录登录密码或会话。
现已补齐资产扫描/筛选/分段/历史、质量检查、任务详情/重发和数据集筛选/预览/发布/版本/Manifest。
发布为后台任务，版本成功封存后才可见；下载核对SHA-256，默认仅人工标签。
新增页面分别位于src/AssetCenter.vue、DatasetCenter.vue和JobCenter.vue，按需加载。
完整浏览器测试还验证标签变化后v1的Manifest字节不变；截图位于artifacts/phase5-*.png。
详细使用和边界见../docs/PHASE_5.md。后端与Worker可由仓库根的scripts/quickstart.py统一启动。
