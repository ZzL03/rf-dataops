<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from "vue";

const props = defineProps({
  api: { type: [Object, Function], required: true },
  user: { type: Object, required: true },
});
const emit = defineEmits(["open-segments", "navigate"]);
const files = ref([]),
  total = ref(0),
  page = ref(1),
  query = ref(""),
  status = ref(""),
  format = ref("");
const selected = ref(null),
  observations = ref([]),
  error = ref(""),
  message = ref(""),
  loading = ref(false);
const directory = ref("demo-phase3"),
  windowSize = ref(256),
  stride = ref(128),
  start = ref(0),
  end = ref("");
const activeJob = ref(null),
  submitting = ref(false);
let disposed = false;
let timer,
  sequence = 0,
  lastRequest = null;
const names = {
  PENDING: "排队中",
  RUNNING: "处理中",
  RETRY_WAIT: "等待重试",
  SUCCEEDED: "已完成",
  FAILED: "失败",
  ACTIVE: "可用",
  STALE: "旧修订",
  INVALID: "不可用",
};
const administrator = computed(() => props.user.role === "ADMIN");
const windows = computed(() =>
  Math.max(
    0,
    Math.floor(
      ((end.value === ""
        ? selected.value?.sample_count || 0
        : Number(end.value)) -
        start.value -
        windowSize.value) /
        stride.value,
    ) + 1,
  ),
);
const short = (id) => id?.slice(0, 8);
const fail = (exception) => {
  error.value =
    exception.response?.data?.error?.message || "请求失败，请检查连接后重试。";
};
async function reload() {
  loading.value = true;
  error.value = "";
  try {
    const { data } = await props.api.get("/files", {
      params: {
        limit: 20,
        offset: (page.value - 1) * 20,
        q: query.value || undefined,
        status: status.value || undefined,
        format: format.value || undefined,
      },
    });
    files.value = data.items;
    total.value = data.total;
    if (selected.value)
      selected.value = (
        await props.api.get(`/files/${selected.value.id}`)
      ).data;
  } catch (exception) {
    fail(exception);
  } finally {
    loading.value = false;
  }
}
async function search() {
  page.value = 1;
  await reload();
}
async function choose(row) {
  if (!row) return;
  const current = ++sequence;
  selected.value = row;
  start.value = 0;
  end.value = "";
  windowSize.value = Math.min(256, row.sample_count);
  stride.value = Math.max(1, Math.floor(windowSize.value / 2));
  observations.value = [];
  try {
    const { data } = await props.api.get(`/files/${row.id}/observations`);
    if (current === sequence) observations.value = data.items;
  } catch (exception) {
    if (current === sequence) fail(exception);
  }
}
async function enqueue(body) {
  if (submitting.value || activeJob.value) return;
  error.value = "";
  message.value = "";
  submitting.value = true;
  const fingerprint = JSON.stringify(body);
  if (!lastRequest || lastRequest.fingerprint !== fingerprint)
    lastRequest = { fingerprint, key: crypto.randomUUID() };
  try {
    const { data } = await props.api.post("/jobs", body, {
      headers: { "Idempotency-Key": lastRequest.key },
    });
    lastRequest = null;
    if (disposed) return;
    activeJob.value = data;
    poll(data.id, 0);
  } catch (exception) {
    fail(exception);
  } finally {
    submitting.value = false;
  }
}
async function poll(id, attempts) {
  try {
    const { data } = await props.api.get(`/jobs/${id}`);
    if (disposed) return;
    activeJob.value = data;
    if (["SUCCEEDED", "FAILED"].includes(data.status)) {
      activeJob.value = null;
      if (data.status === "FAILED") error.value = data.last_error || "任务失败";
      else if (data.job_type === "SCAN") {
        message.value = `扫描结束：新增 ${data.result.registered}，已存在 ${data.result.unchanged}，未完成 ${data.result.skipped}，错误 ${data.result.errors.length}。`;
        if (data.result.errors.length)
          error.value = data.result.errors
            .map((item) => `${item.path}: ${item.message}`)
            .join("；");
      } else message.value = `区间索引已完成：${data.result.total} 个引用。`;
      await reload();
      return;
    }
    if (attempts < 90) timer = setTimeout(() => poll(id, attempts + 1), 1000);
    else {
      activeJob.value = null;
      message.value = "任务仍在运行，请到后台任务页继续查询。";
    }
  } catch (exception) {
    activeJob.value = null;
    fail(exception);
  }
}
function segment() {
  enqueue({
    job_type: "SEGMENT",
    raw_file_id: selected.value.id,
    config: {
      window_samples: Number(windowSize.value),
      stride_samples: Number(stride.value),
    },
    start: Number(start.value),
    ...(end.value === "" ? {} : { end: Number(end.value) }),
  });
}
onMounted(reload);
onBeforeUnmount(() => {
  disposed = true;
  clearTimeout(timer);
  sequence++;
});
defineExpose({ reload });
</script>

<template>
  <div class="asset-center">
    <el-alert
      v-if="message"
      :title="message"
      type="success"
      :closable="false"
      class="notice"
    />
    <el-alert
      v-if="error"
      :title="error"
      type="error"
      :closable="false"
      class="notice"
    />
    <section v-if="administrator" class="panel scan-panel">
      <div>
        <h2>扫描受控目录</h2>
        <p class="muted">
          采集文件需有 .iq.json 元数据及 .done 完成标记。每次最多检查 100
          个路径。
        </p>
      </div>
      <form
        class="inline-form"
        @submit.prevent="enqueue({ job_type: 'SCAN', directory })"
      >
        <label for="scan-directory">相对目录</label
        ><el-input
          id="scan-directory"
          v-model="directory"
          placeholder="例如 demo-phase3"
          maxlength="512"
        /><el-button
          native-type="submit"
          type="primary"
          :loading="submitting"
          :disabled="!!activeJob || !directory"
          >提交扫描</el-button
        >
      </form>
    </section>
    <div v-if="activeJob" class="job-note">
      <el-tag>{{ names[activeJob.status] }}</el-tag
      ><span
        >任务 {{ short(activeJob.id) }} · 尝试 {{ activeJob.attempts }}/{{
          activeJob.max_attempts
        }}</span
      ><el-button text @click="emit('navigate', 'jobs')"
        >查看后台任务</el-button
      >
    </div>
    <form class="asset-filters" @submit.prevent="search">
      <el-input
        v-model="query"
        aria-label="搜索文件路径"
        placeholder="按相对路径搜索"
        clearable
      /><el-select
        v-model="status"
        placeholder="全部状态"
        aria-label="文件状态"
        @change="search"
        ><el-option label="全部状态" value="" /><el-option
          v-for="item in ['ACTIVE', 'STALE', 'INVALID']"
          :key="item"
          :value="item"
          :label="names[item]" /></el-select
      ><el-select
        v-model="format"
        placeholder="全部格式"
        aria-label="文件格式"
        @change="search"
        ><el-option label="全部格式" value="" /><el-option
          label="BIN"
          value="BIN" /><el-option label="HDF5" value="HDF5" /></el-select
      ><el-button native-type="submit">查询文件</el-button
      ><span class="muted">{{ total }} 个文件修订</span>
    </form>
    <section class="panel">
      <el-table
        :data="files"
        :empty-text="loading ? '正在读取文件...' : '暂无文件'"
        highlight-current-row
        @current-change="choose"
        ><el-table-column
          prop="relative_path"
          label="相对路径"
          min-width="260"
          show-overflow-tooltip
        /><el-table-column
          prop="format"
          label="格式"
          width="85"
        /><el-table-column label="状态" width="110"
          ><template #default="{ row }"
            ><el-tag :type="row.status === 'ACTIVE' ? 'success' : 'warning'">{{
              names[row.status]
            }}</el-tag></template
          ></el-table-column
        ><el-table-column prop="sample_count" label="采样数" /><el-table-column
          prop="segment_count"
          label="片段引用数"
        /><el-table-column label="大小" width="100"
          ><template #default="{ row }"
            >{{ (row.byte_size / 1024).toFixed(1) }} KiB</template
          ></el-table-column
        ></el-table
      ><el-pagination
        v-model:current-page="page"
        layout="prev, pager, next, total"
        :total="total"
        :page-size="20"
        @current-change="reload"
      />
    </section>
    <section v-if="selected" class="panel file-detail">
      <div class="panel-title">
        {{ selected.relative_path
        }}<el-button @click="emit('open-segments', selected.id)"
          >查看片段与预测</el-button
        >
      </div>
      <div class="metadata-grid">
        <span>文件编号</span><code>{{ selected.id }}</code
        ><span>完整 SHA-256</span><code>{{ selected.sha256 }}</code
        ><span>采样率 / 中心频率</span
        ><span
          >{{ selected.iq_metadata.sample_rate_hz }} Hz /
          {{ selected.iq_metadata.center_frequency_hz }} Hz</span
        ><span>元数据</span
        ><code>{{ JSON.stringify(selected.iq_metadata) }}</code>
      </div>
      <template v-if="administrator"
        ><h3>创建区间索引</h3>
        <p class="muted">
          只记录采样区间，不复制 IQ。单次最多 1,000 个片段，超限请调整起止位置。
        </p>
        <form class="form-grid" @submit.prevent="segment">
          <label
            >窗口采样数<input
              v-model.number="windowSize"
              type="number"
              min="1"
              max="1000000"
              required
              class="native-field"
              aria-label="窗口采样数" /></label
          ><label
            >步长采样数<input
              v-model.number="stride"
              type="number"
              min="1"
              :max="windowSize"
              required
              class="native-field"
              aria-label="步长采样数" /></label
          ><label
            >起始采样<input
              v-model.number="start"
              type="number"
              min="0"
              required
              class="native-field"
              aria-label="起始采样" /></label
          ><label
            >结束采样（不填则到末尾）<input
              v-model="end"
              type="number"
              min="1"
              :max="selected.sample_count"
              class="native-field"
              aria-label="结束采样"
          /></label>
          <div class="form-span inline-form">
            <span class="muted"
              >预计 {{ Number.isFinite(windows) ? windows : 0 }} 个片段</span
            ><el-button
              native-type="submit"
              type="primary"
              :disabled="
                selected.status !== 'ACTIVE' ||
                !windows ||
                windows > 1000 ||
                !!activeJob ||
                submitting
              "
              >提交分段任务</el-button
            >
          </div>
        </form></template
      >
      <h3>最近接入记录</h3>
      <div
        v-for="record in observations"
        :key="record.id"
        class="observation-row"
      >
        <code>{{ record.event }}</code
        ><span>{{ new Date(record.created_at).toLocaleString("zh-CN") }}</span>
      </div>
    </section>
  </div>
</template>
