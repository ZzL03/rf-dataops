<script setup>
import {
  computed,
  defineAsyncComponent,
  nextTick,
  onBeforeUnmount,
  ref,
} from "vue";
import axios from "axios";
const AssetCenter = defineAsyncComponent(() => import("./AssetCenter.vue"));
const DatasetCenter = defineAsyncComponent(() => import("./DatasetCenter.vue"));
const JobCenter = defineAsyncComponent(() => import("./JobCenter.vue"));
import * as echarts from "echarts/core";
import { LineChart } from "echarts/charts";
import { GridComponent, TooltipComponent } from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";

echarts.use([LineChart, GridComponent, TooltipComponent, CanvasRenderer]);
const api = axios.create({ baseURL: "/api/v1", timeout: 15000 });
const user = ref(null),
  username = ref(""),
  password = ref(""),
  signing = ref(false);
const section = ref("reviews"),
  message = ref(""),
  error = ref(""),
  busy = ref(false);
const reviews = ref([]),
  total = ref(0),
  page = ref(1),
  filter = ref("PENDING"),
  lowOnly = ref(false);
const selected = ref(null),
  predictions = ref([]),
  history = ref([]),
  historyTotal = ref(0);
const predictionId = ref(""),
  label = ref(""),
  comment = ref(""),
  conflict = ref(false);
const modelList = ref([]),
  modelId = ref(""),
  segments = ref([]),
  segmentTotal = ref(0),
  segmentPage = ref(1),
  selectedSegment = ref("");
const pendingJob = ref(null);
const assetCenter = ref(null),
  datasetCenter = ref(null),
  jobCenter = ref(null),
  segmentFilterFile = ref("");
const wave = ref(null),
  spectrum = ref(null),
  preview = ref(null),
  chartObjects = [];
const canReview = computed(() =>
  ["ADMIN", "REVIEWER"].includes(user.value?.role),
);
const chosenPrediction = computed(() =>
  predictions.value.find((item) => item.id === predictionId.value),
);
const human = computed(() => selected.value?.current_annotation);
const names = {
  PENDING: "待处理",
  RUNNING: "处理中",
  RETRY_WAIT: "等待重试",
  SUCCEEDED: "已完成",
  FAILED: "失败",
  REVIEWED: "已审核",
  UNKNOWN: "人工标记未知",
};
const title = computed(
  () =>
    ({
      assets: "数据资产中心",
      datasets: "数据集管理",
      reviews: "人工审核工作台",
      segments: "片段与模型建议",
      jobs: "后台任务",
    })[section.value],
);
let detailSequence = 0,
  pollTimer = null;
const short = (id) => (id ? id.slice(0, 8) : "—");
const percent = (value) => `${(value * 100).toFixed(1)}%`;
const date = (value) =>
  new Date(value).toLocaleString("zh-CN", { hour12: false });
const fail = (exception) => {
  error.value =
    exception.response?.data?.error?.message ||
    (exception.response?.status === 422
      ? "请检查输入参数"
      : "请求未完成，请检查服务并重试");
};
api.interceptors.response.use(
  (response) => response,
  (exception) => {
    if (exception.response?.status === 401 && user.value) clearSession();
    return Promise.reject(exception);
  },
);

function disposeCharts() {
  chartObjects.splice(0).forEach((chart) => chart.dispose());
}
function clearSession() {
  detailSequence++;
  clearTimeout(pollTimer);
  disposeCharts();
  delete api.defaults.headers.common.Authorization;
  user.value = null;
  selected.value = null;
  pendingJob.value = null;
  password.value = "";
  reviews.value = [];
  segments.value = [];
  predictions.value = [];
  history.value = [];
}
async function signIn() {
  if (!username.value || !password.value) return;
  signing.value = true;
  error.value = "";
  message.value = "";
  try {
    const { data } = await api.post("/auth/token", {
      username: username.value,
      password: password.value,
    });
    api.defaults.headers.common.Authorization = `Bearer ${data.access_token}`;
    password.value = "";
    user.value = (await api.get("/auth/me")).data;
    await refreshSection();
  } catch (exception) {
    delete api.defaults.headers.common.Authorization;
    fail(exception);
  } finally {
    signing.value = false;
  }
}
async function signOut() {
  try {
    await api.post("/auth/logout");
  } catch (exception) {
    fail(exception);
  } finally {
    clearSession();
  }
}
async function refreshSection() {
  error.value = "";
  try {
    if (section.value === "reviews") await loadReviews();
    if (section.value === "segments") await loadSegments();
    if (section.value === "jobs") await jobCenter.value?.reload();
    if (section.value === "assets") await assetCenter.value?.reload();
    if (section.value === "datasets") await datasetCenter.value?.reload();
  } catch (exception) {
    fail(exception);
  }
}
async function navigate(target) {
  detailSequence++;
  disposeCharts();
  section.value = target;
  selected.value = null;
  message.value = "";
  await refreshSection();
}
async function loadReviews() {
  const { data } = await api.get("/reviews", {
    params: {
      limit: 20,
      offset: (page.value - 1) * 20,
      status: filter.value || undefined,
      max_confidence: lowOnly.value ? 0.6 : undefined,
    },
  });
  reviews.value = data.items;
  total.value = data.total;
}
async function applyFilter() {
  page.value = 1;
  await refreshSection();
}
async function openReview(review) {
  if (busy.value) return;
  const sequence = ++detailSequence;
  selected.value = null;
  preview.value = null;
  error.value = "";
  conflict.value = false;
  disposeCharts();
  try {
    const [detail, forecast, audit, signal] = await Promise.all([
      api.get(`/reviews/${review.id}`),
      api.get(`/segments/${review.segment_id}/predictions`, {
        params: { limit: 100 },
      }),
      api.get(`/reviews/${review.id}/annotations`, { params: { limit: 100 } }),
      api.get(`/segments/${review.segment_id}/preview`, {
        params: { max_points: 512 },
      }),
    ]);
    if (sequence !== detailSequence || !user.value) return;
    selected.value = detail.data;
    predictions.value = forecast.data.items;
    history.value = audit.data.items;
    historyTotal.value = audit.data.total;
    preview.value = signal.data;
    predictionId.value = predictions.value.at(-1)?.id || "";
    label.value = "";
    comment.value = "";
    await nextTick();
    drawCharts();
  } catch (exception) {
    if (sequence === detailSequence) fail(exception);
  }
}
function drawCharts() {
  disposeCharts();
  if (!wave.value || !spectrum.value || !preview.value) return;
  const data = preview.value;
  const base = {
    animation: false,
    tooltip: { trigger: "axis" },
    grid: { left: 58, right: 20, top: 20, bottom: 36 },
    xAxis: {
      type: "value",
      scale: true,
      axisLabel: { color: "#7b8999" },
      splitLine: { show: false },
    },
    yAxis: {
      type: "value",
      axisLabel: { color: "#7b8999" },
      splitLine: { lineStyle: { color: "#edf1f5" } },
    },
  };
  const a = echarts.init(wave.value),
    b = echarts.init(spectrum.value);
  a.setOption({
    ...base,
    series: [
      {
        name: "I",
        type: "line",
        showSymbol: false,
        color: "#3478c8",
        data: data.sample_indices.map((x, i) => [x, data.i[i]]),
      },
      {
        name: "Q",
        type: "line",
        showSymbol: false,
        color: "#14a39a",
        data: data.sample_indices.map((x, i) => [x, data.q[i]]),
      },
    ],
  });
  b.setOption({
    ...base,
    series: [
      {
        name: "相对功率 dB",
        type: "line",
        showSymbol: false,
        color: "#3478c8",
        areaStyle: { opacity: 0.08 },
        data: data.frequency_offset_hz.map((x, i) => [x, data.power_db[i]]),
      },
    ],
  });
  chartObjects.push(a, b);
}
const resize = () => chartObjects.forEach((chart) => chart.resize());
window.addEventListener("resize", resize);
onBeforeUnmount(() => {
  window.removeEventListener("resize", resize);
  clearTimeout(pollTimer);
  disposeCharts();
});
async function save(action) {
  if (!selected.value || busy.value || conflict.value) return;
  error.value = "";
  message.value = "";
  const prediction = chosenPrediction.value;
  const value =
    action === "CONFIRM"
      ? prediction?.label
      : action === "UNKNOWN"
        ? "UNKNOWN"
        : label.value;
  if (!value) {
    error.value = "请先选择一个人工标签";
    return;
  }
  busy.value = true;
  const review = selected.value;
  try {
    await api.post(`/reviews/${review.id}/annotations`, {
      expected_version: review.label_version,
      label: value,
      action,
      comment: comment.value,
      prediction_id: prediction?.id || null,
    });
    message.value = "人工结论已保存，历史记录已追加。";
    busy.value = false;
    await openReview(review);
    await loadReviews();
  } catch (exception) {
    if (
      exception.response?.status === 409 &&
      exception.response?.data?.error?.code === "REVIEW_VERSION_CONFLICT"
    ) {
      conflict.value = true;
      error.value =
        "这份样本已被另一位审核者修改。你的草稿已保留，请重新加载后再决定。";
    } else fail(exception);
  } finally {
    busy.value = false;
  }
}
async function loadSegments() {
  const [assets, models] = await Promise.all([
    api.get("/segments", {
      params: {
        limit: 20,
        offset: (segmentPage.value - 1) * 20,
        raw_file_id: segmentFilterFile.value || undefined,
      },
    }),
    api.get("/models", { params: { limit: 100 } }),
  ]);
  segments.value = assets.data.items;
  segmentTotal.value = assets.data.total;
  modelList.value = models.data.items;
  if (!modelId.value) modelId.value = modelList.value[0]?.id || "";
}
async function submitInference() {
  if (!selectedSegment.value || !modelId.value || pendingJob.value) return;
  error.value = "";
  message.value = "";
  try {
    const { data } = await api.post(
      "/jobs",
      {
        job_type: "INFER",
        segment_id: selectedSegment.value,
        model_version_id: modelId.value,
      },
      { headers: { "Idempotency-Key": crypto.randomUUID() } },
    );
    pendingJob.value = data;
    message.value = `任务 ${short(data.id)} 已提交，正在等待 Worker。`;
    pollJob(data.id, 0);
  } catch (exception) {
    fail(exception);
  }
}
async function pollJob(id, attempt) {
  try {
    const { data } = await api.get(`/jobs/${id}`);
    if (!user.value) return;
    pendingJob.value = data;
    if (["SUCCEEDED", "FAILED"].includes(data.status)) {
      message.value =
        data.status === "SUCCEEDED"
          ? data.job_type === "QUALITY"
            ? "质量检查已完成，可查看片段的指标。"
            : "预标注已完成，请到人工审核工作台查看。"
          : `处理失败：${data.last_error}`;
      pendingJob.value = null;
      if (data.job_type === "QUALITY" && section.value === "segments")
        await loadSegments();
      return;
    }
    if (attempt < 60)
      pollTimer = setTimeout(() => pollJob(id, attempt + 1), 1000);
    else {
      pendingJob.value = null;
      message.value = "任务仍在处理，可在后台任务页继续查询。";
    }
  } catch (exception) {
    pendingJob.value = null;
    fail(exception);
  }
}
async function openFileSegments(id) {
  segmentFilterFile.value = id;
  segmentPage.value = 1;
  await navigate("segments");
}
async function submitQuality() {
  if (!selectedSegment.value || pendingJob.value) return;
  error.value = "";
  try {
    const { data } = await api.post(
      "/jobs",
      { job_type: "QUALITY", segment_id: selectedSegment.value },
      { headers: { "Idempotency-Key": crypto.randomUUID() } },
    );
    pendingJob.value = data;
    pollJob(data.id, 0);
  } catch (exception) {
    fail(exception);
  }
}
</script>

<template>
  <main v-if="!user" class="login-page">
    <section class="login-intro">
      <div class="brandmark">RF<span>DATAOPS</span></div>
      <p class="eyebrow">SIGNAL DATA WORKBENCH</p>
      <h1>让每一个标签<br />都有据可查。</h1>
      <p>
        从原始 IQ 到模型建议，再到人工结论。<br />清晰地管理信号数据的每一步。
      </p>
      <div class="signal-art">
        <i
          v-for="n in 47"
          :key="n"
          :style="{ height: `${18 + Math.abs(Math.sin(n * 0.58)) * 70}px` }"
        ></i>
      </div>
      <small>电磁信号数据治理与标注管理平台</small>
    </section>
    <section class="login-panel">
      <form @submit.prevent="signIn">
        <p class="eyebrow">欢迎回来</p>
        <h2>登录工作台</h2>
        <p class="muted">使用管理员创建的账号继续。</p>
        <label for="username">用户名</label
        ><el-input
          id="username"
          v-model="username"
          autocomplete="username"
          size="large"
          maxlength="64"
          placeholder="请输入用户名"
        /><label for="password">密码</label
        ><el-input
          id="password"
          v-model="password"
          autocomplete="current-password"
          type="password"
          show-password
          size="large"
          maxlength="128"
          placeholder="请输入密码"
        /><el-button
          native-type="submit"
          type="primary"
          size="large"
          :loading="signing"
          class="login-button"
          >登录</el-button
        >
        <p v-if="error" role="alert" class="error-text">{{ error }}</p>
        <p class="muted session-note">会话有效期有限；刷新页面后需重新登录。</p>
      </form>
    </section>
  </main>
  <div v-else class="workbench">
    <aside class="sidebar">
      <div class="brandmark">RF<span>DATAOPS</span></div>
      <p class="side-caption">信号数据工作台</p>
      <nav aria-label="主导航">
        <button
          v-for="(name, key) in {
            assets: '数据资产',
            datasets: '数据集管理',
            reviews: '人工审核',
            segments: '片段与预测',
            jobs: '后台任务',
          }"
          :key="key"
          :class="{ active: section === key }"
          :disabled="busy"
          @click="navigate(key)"
        >
          <span>{{
            {
              assets: "▦",
              datasets: "▧",
              reviews: "◈",
              segments: "▤",
              jobs: "◷",
            }[key]
          }}</span
          >{{ name }}
        </button>
      </nav>
      <div class="sidebar-bottom">
        数据有来源，结论有依据<br /><small>结果可追溯，历史可保留</small>
      </div>
    </aside>
    <div class="main-area">
      <header class="topbar">
        <span>RF-DataOps <b>/</b> {{ title }}</span>
        <div>
          <span class="user-badge">{{ user.username }} · {{ user.role }}</span
          ><el-button text @click="signOut">退出登录</el-button>
        </div>
      </header>
      <div class="page-body">
        <div class="page-heading">
          <div>
            <p class="eyebrow">SIGNAL GOVERNANCE</p>
            <h1>{{ title }}</h1>
            <p class="muted">
              {{
                {
                  assets: "接入完成的采集文件，查看修订并创建片段索引。",
                  datasets: "筛选已审核样本，发布可追溯的固定版本。",
                  reviews: "查看信号、参考模型建议，记录你自己的判断。",
                  segments: "选择已登记片段，由后台 Worker 运行指定模型版本。",
                  jobs: "查看真实处理状态、错误与历史，重新发起失败任务。",
                }[section]
              }}
            </p>
          </div>
          <el-button :disabled="busy" @click="refreshSection"
            >刷新列表</el-button
          >
        </div>
        <el-alert
          v-if="message"
          :title="message"
          type="success"
          :closable="false"
          show-icon
          class="notice"
        />
        <el-alert
          v-if="error"
          :title="error"
          type="error"
          :closable="false"
          show-icon
          class="notice"
        />
        <AssetCenter
          v-if="section === 'assets'"
          ref="assetCenter"
          :api="api"
          :user="user"
          @open-segments="openFileSegments"
          @navigate="navigate"
        />
        <DatasetCenter
          v-else-if="section === 'datasets'"
          ref="datasetCenter"
          :api="api"
          :user="user"
        />
        <template v-else-if="section === 'reviews'">
          <div class="filters">
            <el-select
              v-model="filter"
              aria-label="审核状态"
              @change="applyFilter"
              ><el-option label="待审核" value="PENDING" /><el-option
                label="已审核"
                value="REVIEWED" /><el-option
                label="人工标记未知"
                value="UNKNOWN" /><el-option
                label="全部状态"
                value="" /></el-select
            ><el-checkbox v-model="lowOnly" @change="applyFilter"
              >仅看模型分数 ≤ 60%</el-checkbox
            ><span class="muted">{{ total }} 条记录 · 低分优先</span>
          </div>
          <div class="review-layout">
            <section class="panel review-list">
              <div class="panel-title">
                样本队列 <span>{{ total }}</span>
              </div>
              <p v-if="!reviews.length" class="empty">
                没有匹配的审核任务。<br />可先到“片段与预测”提交预标注。
              </p>
              <button
                v-for="review in reviews"
                :key="review.id"
                class="review-row"
                :class="{ selected: selected?.id === review.id }"
                :disabled="busy"
                @click="openReview(review)"
              >
                <span class="row-top"
                  >IQ · {{ short(review.segment_id)
                  }}<span
                    class="status-dot"
                    :class="review.status"
                  ></span></span
                ><span class="row-bottom"
                  >{{ names[review.status] }} · v{{ review.label_version
                  }}<b>{{
                    review.min_model_confidence === null
                      ? "—"
                      : percent(review.min_model_confidence)
                  }}</b></span
                ></button
              ><el-pagination
                v-model:current-page="page"
                small
                layout="prev, pager, next"
                :total="total"
                :page-size="20"
                @current-change="refreshSection"
              />
            </section>
            <div v-if="selected" class="review-detail">
              <div class="detail-title">
                <div>
                  <h2>IQ · {{ short(selected.segment_id) }}</h2>
                  <p class="muted">
                    {{ names[selected.status] }} · 当前人工版本 v{{
                      selected.label_version
                    }}
                  </p>
                </div>
                <el-tag v-if="human" type="success"
                  >人工标签：{{ human.label }}</el-tag
                ><el-tag v-else type="info">尚无人工标签</el-tag>
              </div>
              <section class="panel signal-panel">
                <div class="panel-title">
                  信号预览
                  <span v-if="preview"
                    >{{ preview.sample_count }} samples · RMS
                    {{ preview.stats.rms.toFixed(4) }}</span
                  >
                </div>
                <div class="chart-label">时域 · I / Q</div>
                <div
                  ref="wave"
                  class="chart"
                  role="img"
                  aria-label="信号 I Q 时域波形"
                ></div>
                <div class="chart-label">相对功率谱 · 频率偏移 Hz</div>
                <div
                  ref="spectrum"
                  class="chart"
                  role="img"
                  aria-label="信号相对功率谱"
                ></div>
              </section>
              <section class="panel">
                <div class="panel-title">
                  模型建议 <span>独立于人工结论</span>
                </div>
                <div v-if="predictions.length">
                  <el-select
                    v-model="predictionId"
                    aria-label="选择模型预测"
                    class="model-select"
                    ><el-option
                      v-for="prediction in predictions"
                      :key="prediction.id"
                      :value="prediction.id"
                      :label="`${prediction.model.name} / ${prediction.model.version}`" /></el-select
                  ><template v-if="chosenPrediction"
                    ><div class="prediction-summary">
                      <strong>{{ chosenPrediction.label }}</strong
                      ><span>{{ percent(chosenPrediction.confidence) }}</span
                      ><el-tag
                        :type="chosenPrediction.is_mock ? 'warning' : 'info'"
                        >{{
                          chosenPrediction.is_mock
                            ? "Mock · 模拟结果"
                            : "模型预测"
                        }}</el-tag
                      >
                    </div>
                    <p v-if="chosenPrediction.is_mock" class="mock-note">
                      这些分数由模拟规则生成，仅用于验证流程，不代表识别准确率。
                    </p>
                    <div
                      class="score-row"
                      v-for="(score, name) in chosenPrediction.scores"
                      :key="name"
                    >
                      <span>{{ name }}</span
                      ><el-progress
                        :percentage="Number((score * 100).toFixed(1))"
                        :stroke-width="6"
                        :show-text="false"
                      /><b>{{ percent(score) }}</b>
                    </div></template
                  >
                </div>
                <p v-else class="empty">暂无模型建议</p>
              </section>
              <section class="panel">
                <div class="panel-title">
                  人工结论
                  <span>{{
                    canReview ? "每次保存都会追加历史" : "当前角色仅可查看"
                  }}</span>
                </div>
                <template v-if="canReview"
                  ><p v-if="conflict" class="conflict-message">
                    草稿暂未提交。<el-button
                      type="warning"
                      @click="openReview(selected)"
                      >重新加载并放弃当前草稿</el-button
                    >
                  </p>
                  <label for="human-label">选择人工标签</label
                  ><el-select
                    id="human-label"
                    v-model="label"
                    placeholder="请选择你判断的类别"
                    :disabled="busy || conflict"
                    ><el-option
                      v-for="value in [
                        'BPSK',
                        'QPSK',
                        '8PSK',
                        '16QAM',
                        '64QAM',
                      ]"
                      :key="value"
                      :label="value"
                      :value="value" /></el-select
                  ><label for="review-comment">审核备注</label
                  ><el-input
                    id="review-comment"
                    v-model="comment"
                    type="textarea"
                    :rows="3"
                    maxlength="2000"
                    show-word-limit
                    :disabled="busy || conflict"
                    placeholder="记录判断依据（可选）"
                  />
                  <div class="review-actions">
                    <el-button
                      type="primary"
                      :disabled="!label || conflict"
                      :loading="busy"
                      @click="save('CORRECT')"
                      >保存人工标签</el-button
                    ><el-button
                      :disabled="!chosenPrediction || conflict || busy"
                      @click="save('CONFIRM')"
                      >确认当前模型建议</el-button
                    ><el-button
                      :disabled="conflict || busy"
                      @click="save('UNKNOWN')"
                      >标记未知</el-button
                    >
                  </div></template
                >
                <p v-else class="muted">
                  研究人员可查看预测和审核历史；修改标签需要审核人员或管理员权限。
                </p>
              </section>
              <section class="panel">
                <div class="panel-title">
                  审核历史
                  <span>共 {{ historyTotal }} 条，显示最近 100 条</span>
                </div>
                <p v-if="!history.length" class="empty">尚无人工操作记录</p>
                <article
                  v-for="record in history"
                  :key="record.id"
                  class="history-row"
                >
                  <div>
                    <b>v{{ record.version }} · {{ record.label }}</b
                    ><span
                      >{{ record.reviewer_username }} ·
                      {{ date(record.created_at) }}</span
                    >
                  </div>
                  <p>{{ record.comment || "未填写备注" }}</p>
                </article>
              </section>
            </div>
            <section v-else class="panel select-empty">
              <div>◈</div>
              <h2>选择一个信号片段</h2>
              <p>从左侧队列打开样本，查看信号与审核记录。</p>
            </section>
          </div>
        </template>
        <template v-else-if="section === 'segments'">
          <div v-if="segmentFilterFile" class="job-note">
            已限定源文件 {{ short(segmentFilterFile)
            }}<el-button
              text
              @click="
                segmentFilterFile = '';
                segmentPage = 1;
                refreshSection();
              "
              >查看全部片段</el-button
            >
          </div>
          <section class="panel inference-bar">
            <div>
              <h2>提交模型预标注</h2>
              <p class="muted">
                提交后生成模型建议和待审核任务，人工标签保持独立。
              </p>
            </div>
            <div class="inference-controls">
              <el-select
                v-model="modelId"
                aria-label="选择模型版本"
                placeholder="选择模型版本"
                ><el-option
                  v-for="model in modelList"
                  :key="model.id"
                  :value="model.id"
                  :label="`${model.name} / ${model.version}${model.is_mock ? ' · Mock' : ''}`" /></el-select
              ><el-button
                v-if="user.role === 'ADMIN'"
                type="primary"
                :disabled="!selectedSegment || !modelId || !!pendingJob"
                @click="submitInference"
                >{{
                  pendingJob ? names[pendingJob.status] : "为所选片段提交预标注"
                }}</el-button
              ><span v-else class="muted">管理员可提交处理任务</span>
            </div>
            <p v-if="!modelList.length" class="mock-note">
              尚未登记模型，请先按项目说明运行模型初始化命令。
            </p>
          </section>
          <el-button
            v-if="user.role === 'ADMIN'"
            :disabled="!selectedSegment || !!pendingJob"
            @click="submitQuality"
            class="quality-button"
            >为所选片段执行质量检查</el-button
          >
          <section class="panel">
            <el-table
              :data="segments"
              highlight-current-row
              @current-change="(row) => (selectedSegment = row?.id || '')"
              ><el-table-column label="片段编号" min-width="140"
                ><template #default="{ row }"
                  >IQ · {{ short(row.id) }}</template
                ></el-table-column
              ><el-table-column label="源文件编号" min-width="140"
                ><template #default="{ row }">{{
                  short(row.raw_file_id)
                }}</template></el-table-column
              ><el-table-column
                prop="start_sample"
                label="起始采样"
              /><el-table-column
                prop="sample_count"
                label="采样数"
              /><el-table-column label="创建时间" min-width="180"
                ><template #default="{ row }">{{
                  date(row.created_at)
                }}</template></el-table-column
              ></el-table
            ><el-pagination
              v-model:current-page="segmentPage"
              layout="prev, pager, next, total"
              :total="segmentTotal"
              :page-size="20"
              @current-change="refreshSection"
            />
          </section>
        </template>
        <JobCenter v-else ref="jobCenter" :api="api" :user="user" />
      </div>
      <footer>RF-DataOps · 模型建议与人工标签分别保存</footer>
    </div>
  </div>
</template>
