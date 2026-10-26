<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";

const props = defineProps({
  api: { type: [Object, Function], required: true },
  user: { type: Object, required: true },
});
const datasets = ref([]),
  total = ref(0),
  page = ref(1),
  selected = ref(null),
  versions = ref([]),
  versionTotal = ref(0),
  versionPage = ref(1);
const name = ref(""),
  description = ref(""),
  error = ref(""),
  message = ref(""),
  saving = ref(false);
const versionName = ref("v1.0.0"),
  rawIds = ref([]),
  labels = ref([]),
  reviewStatus = ref("REVIEWED"),
  policy = ref("HUMAN_ONLY"),
  modelId = ref(""),
  allowMock = ref(false);
const fileOptions = ref([]),
  modelOptions = ref([]),
  sourceQuery = ref(""),
  selectionPreview = ref(null),
  checking = ref(false),
  publishing = ref(false),
  job = ref(null);
const viewed = ref(null),
  members = ref([]),
  memberPage = ref(1),
  downloading = ref(false);
const canPublish = computed(() =>
  ["ADMIN", "RESEARCHER"].includes(props.user.role),
);
const chosenModel = computed(() =>
  modelOptions.value.find((item) => item.id === modelId.value),
);
const short = (id) => id?.slice(0, 8);
let disposed = false;
let timer,
  lastSubmission,
  sequence = 0;
const fail = (exception) => {
  error.value =
    exception.response?.data?.error?.code === "SELECTION_CHANGED"
      ? "样本或标签已变化，请重新预览后再发布。"
      : exception.response?.data?.error?.message || "请求失败，请重试。";
};
function requestBody() {
  return {
    selection: {
      raw_file_ids: rawIds.value,
      segment_ids: [],
      labels: labels.value,
      review_status: reviewStatus.value === "ALL" ? null : reviewStatus.value,
    },
    label_policy: policy.value,
    model_version_id:
      policy.value === "MODEL_ALLOWED" ? modelId.value || null : null,
    allow_mock_predictions: policy.value === "MODEL_ALLOWED" && allowMock.value,
  };
}
watch(
  [rawIds, labels, reviewStatus, policy, modelId, allowMock],
  () => {
    selectionPreview.value = null;
    lastSubmission = null;
  },
  { deep: true },
);
async function reload() {
  try {
    const { data } = await props.api.get("/datasets", {
      params: { limit: 10, offset: (page.value - 1) * 10 },
    });
    datasets.value = data.items;
    total.value = data.total;
    if (selected.value) await loadVersions();
  } catch (exception) {
    fail(exception);
  }
}
async function create() {
  if (saving.value) return;
  saving.value = true;
  error.value = "";
  message.value = "";
  try {
    const { data } = await props.api.post("/datasets", {
      name: name.value,
      description: description.value,
    });
    name.value = "";
    description.value = "";
    page.value = 1;
    await reload();
    await choose(data);
    message.value = "数据集已创建，接下来预览并发布第一个版本。";
  } catch (exception) {
    fail(exception);
  } finally {
    saving.value = false;
  }
}
async function loadFiles() {
  try {
    fileOptions.value = (
      await props.api.get("/files", {
        params: { limit: 100, q: sourceQuery.value || undefined },
      })
    ).data.items;
  } catch (exception) {
    fail(exception);
  }
}
async function choose(dataset) {
  if (publishing.value) return;
  sequence++;
  selected.value = dataset;
  selectionPreview.value = null;
  viewed.value = null;
  members.value = [];
  versionPage.value = 1;
  rawIds.value = [];
  labels.value = [];
  error.value = "";
  message.value = "";
  lastSubmission = null;
  try {
    await loadVersions();
    if (canPublish.value) {
      await loadFiles();
      modelOptions.value = (
        await props.api.get("/models", { params: { limit: 100 } })
      ).data.items;
    }
  } catch (exception) {
    fail(exception);
  }
}
async function loadVersions() {
  if (!selected.value) return;
  const id = selected.value.id;
  try {
    const { data } = await props.api.get(`/datasets/${id}/versions`, {
      params: { limit: 10, offset: (versionPage.value - 1) * 10 },
    });
    if (selected.value?.id === id && !disposed) {
      versions.value = data.items;
      versionTotal.value = data.total;
    }
  } catch (exception) {
    fail(exception);
  }
}
async function previewSelection() {
  checking.value = true;
  error.value = "";
  message.value = "";
  selectionPreview.value = null;
  const fingerprint = JSON.stringify(requestBody()),
    datasetId = selected.value.id;
  try {
    const { data } = await props.api.post(
      `/datasets/${datasetId}/selection-preview`,
      requestBody(),
    );
    if (
      selected.value?.id === datasetId &&
      fingerprint === JSON.stringify(requestBody())
    )
      selectionPreview.value = data;
  } catch (exception) {
    fail(exception);
  } finally {
    checking.value = false;
  }
}
async function publish() {
  if (!selectionPreview.value || publishing.value) return;
  const body = {
    ...requestBody(),
    version: versionName.value,
    expected_selection_digest: selectionPreview.value.selection_digest,
  };
  const fingerprint = JSON.stringify({ dataset_id: selected.value.id, body });
  if (!lastSubmission || lastSubmission.fingerprint !== fingerprint)
    lastSubmission = { fingerprint, key: crypto.randomUUID() };
  publishing.value = true;
  error.value = "";
  message.value = "";
  try {
    const { data } = await props.api.post(
      `/datasets/${selected.value.id}/versions`,
      body,
      { headers: { "Idempotency-Key": lastSubmission.key } },
    );
    lastSubmission = null;
    if (disposed) return;
    job.value = data;
    poll(data.id, 0);
  } catch (exception) {
    publishing.value = false;
    fail(exception);
  }
}
async function poll(id, attempt) {
  try {
    const { data } = await props.api.get(`/jobs/${id}`);
    if (disposed) return;
    job.value = data;
    if (data.status === "SUCCEEDED") {
      publishing.value = false;
      job.value = null;
      selectionPreview.value = null;
      versionPage.value = 1;
      await reload();
      const version = (
        await props.api.get(
          `/dataset-versions/${data.result.dataset_version_id}`,
        )
      ).data;
      await viewVersion(version);
      message.value = `版本 ${version.version} 已封存，共 ${version.item_count} 个样本。后续标签修改不会改变此版本。`;
      return;
    }
    if (data.status === "FAILED") {
      publishing.value = false;
      job.value = null;
      selectionPreview.value = null;
      error.value = `发布失败：${data.last_error}。请检查原因并重新预览。`;
      return;
    }
    if (attempt < 90) timer = setTimeout(() => poll(id, attempt + 1), 1000);
    else {
      publishing.value = false;
      job.value = null;
      message.value = "发布任务仍在运行，请到后台任务页查询，避免重复提交。";
    }
  } catch (exception) {
    publishing.value = false;
    job.value = null;
    fail(exception);
  }
}
async function viewVersion(version) {
  viewed.value = version;
  memberPage.value = 1;
  await loadMembers();
}
async function loadMembers() {
  if (!viewed.value) return;
  const id = viewed.value.id,
    current = ++sequence;
  try {
    const { data } = await props.api.get(`/dataset-versions/${id}/items`, {
      params: { limit: 20, offset: (memberPage.value - 1) * 20 },
    });
    if (current === sequence && viewed.value?.id === id)
      members.value = data.items;
  } catch (exception) {
    fail(exception);
  }
}
async function download() {
  downloading.value = true;
  error.value = "";
  try {
    const current = viewed.value;
    const { data, headers } = await props.api.get(
      `/dataset-versions/${current.id}/manifest`,
      { responseType: "blob" },
    );
    const digest = Array.from(
      new Uint8Array(
        await crypto.subtle.digest("SHA-256", await data.arrayBuffer()),
      ),
      (byte) => byte.toString(16).padStart(2, "0"),
    ).join("");
    if (
      digest !== current.manifest_sha256 ||
      digest !== headers["x-manifest-sha256"]
    )
      throw new Error("MANIFEST_HASH_MISMATCH");
    const url = URL.createObjectURL(data),
      link = document.createElement("a");
    link.href = url;
    link.download = `rf-dataops-${current.version}-${short(current.id)}.json`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    message.value = "Manifest 已下载，SHA-256 校验通过。";
  } catch (exception) {
    if (exception.message === "MANIFEST_HASH_MISMATCH")
      error.value = "下载内容校验失败，未保存文件。";
    else fail(exception);
  } finally {
    downloading.value = false;
  }
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
  <div class="dataset-center">
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
    <section v-if="canPublish" class="panel dataset-create">
      <h2>创建数据集</h2>
      <form class="inline-form" @submit.prevent="create">
        <el-input
          v-model="name"
          aria-label="数据集名称"
          placeholder="数据集名称"
          maxlength="100"
        /><el-input
          v-model="description"
          aria-label="数据集说明"
          placeholder="用途或采集说明（可选）"
          maxlength="2000"
        /><el-button
          type="primary"
          native-type="submit"
          :loading="saving"
          :disabled="!name.trim()"
          >创建数据集</el-button
        >
      </form>
    </section>
    <div class="dataset-layout">
      <section class="panel dataset-list">
        <div class="panel-title">
          数据集 <span>{{ total }}</span>
        </div>
        <p v-if="!datasets.length" class="empty">尚未创建数据集</p>
        <button
          v-for="dataset in datasets"
          :key="dataset.id"
          class="dataset-row"
          :class="{ selected: selected?.id === dataset.id }"
          :disabled="publishing"
          @click="choose(dataset)"
        >
          <b>{{ dataset.name }}</b
          ><span>{{ dataset.version_count }} 个已发布版本</span></button
        ><el-pagination
          v-model:current-page="page"
          small
          layout="prev, pager, next"
          :total="total"
          :page-size="10"
          @current-change="reload"
        />
      </section>
      <div v-if="selected" class="dataset-detail">
        <div class="detail-title">
          <div>
            <h2>{{ selected.name }}</h2>
            <p class="muted">
              {{
                selected.description ||
                "固定成员、标签和处理版本，保留每次发布时的事实。"
              }}
            </p>
          </div>
        </div>
        <section v-if="canPublish" class="panel publication-form">
          <div class="panel-title">
            发布新版本 <span>先预览，再交给后台完整校验与封存</span>
          </div>
          <div class="form-grid">
            <label
              >版本名<el-input
                v-model="versionName"
                aria-label="版本名"
                placeholder="例如 v1.0.0"
                maxlength="64"
                :disabled="publishing" /></label
            ><label
              >标签来源<el-select
                v-model="policy"
                aria-label="标签来源"
                :disabled="publishing"
                ><el-option
                  label="仅人工标签（默认）"
                  value="HUMAN_ONLY" /><el-option
                  label="人工优先，允许模型补充"
                  value="MODEL_ALLOWED" /></el-select></label
            ><label
              >审核状态<el-select
                v-model="reviewStatus"
                aria-label="发布审核状态"
                :disabled="publishing"
                ><el-option label="已人工审核" value="REVIEWED" /><el-option
                  label="待人工审核"
                  value="PENDING" /><el-option
                  label="人工标记未知"
                  value="UNKNOWN" /><el-option
                  label="全部状态"
                  value="ALL" /></el-select></label
            ><label
              >类别<el-select
                v-model="labels"
                multiple
                clearable
                aria-label="筛选类别"
                placeholder="不选则包含全部类别"
                :disabled="publishing"
                ><el-option
                  v-for="item in [
                    'BPSK',
                    'QPSK',
                    '8PSK',
                    '16QAM',
                    '64QAM',
                    'UNKNOWN',
                  ]"
                  :key="item"
                  :value="item"
                  :label="item" /></el-select></label
            ><label class="form-span"
              >源文件（不选则按其他条件筛选）<el-select
                v-model="rawIds"
                multiple
                filterable
                clearable
                aria-label="筛选源文件"
                :disabled="publishing"
                placeholder="可选择一个采集批次"
                ><el-option
                  v-for="file in fileOptions"
                  :key="file.id"
                  :value="file.id"
                  :label="file.relative_path" /></el-select
            ></label>
            <form class="inline-form form-span" @submit.prevent="loadFiles">
              <el-input
                v-model="sourceQuery"
                aria-label="查找源文件选项"
                placeholder="目录或文件名，最多返回100个选项"
              /><el-button native-type="submit">查找源文件</el-button>
            </form>
            <label v-if="policy === 'MODEL_ALLOWED'" class="form-span"
              >指定模型版本<el-select
                v-model="modelId"
                aria-label="发布模型版本"
                :disabled="publishing"
                ><el-option
                  v-for="model in modelOptions"
                  :key="model.id"
                  :value="model.id"
                  :label="`${model.name} / ${model.version}${model.is_mock ? ' · Mock' : ''}`" /></el-select
            ></label>
          </div>
          <el-checkbox
            v-if="policy === 'MODEL_ALLOWED' && chosenModel?.is_mock"
            v-model="allowMock"
            :disabled="publishing"
            class="mock-ack"
            >我明确允许包含 Mock 模拟标签，仅用于工程演示</el-checkbox
          >
          <p v-if="policy === 'MODEL_ALLOWED'" class="mock-note">
            已有人工结论始终优先，包括人工标记未知；未人工审核的样本才使用指定模型。模型分数不等于人工真值。
          </p>
          <div class="review-actions">
            <el-button
              :loading="checking"
              :disabled="publishing"
              @click="previewSelection"
              >预览选择</el-button
            ><el-button
              type="primary"
              :loading="publishing"
              :disabled="
                !selectionPreview ||
                !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/.test(versionName)
              "
              @click="publish"
              >发布不可变版本</el-button
            >
          </div>
          <div v-if="job" class="job-note">
            发布任务 {{ short(job.id) }} · {{ job.status }} · 尝试
            {{ job.attempts }}/{{ job.max_attempts }}
          </div>
          <div v-if="selectionPreview" class="selection-preview">
            <div class="summary-tiles">
              <div>
                <b>{{ selectionPreview.item_count }}</b
                ><span>将固定的样本</span>
              </div>
              <div>
                <b>{{ selectionPreview.file_count }}</b
                ><span>原始文件</span>
              </div>
              <div>
                <b
                  >{{ selectionPreview.source_counts.HUMAN || 0 }} /
                  {{ selectionPreview.source_counts.MODEL || 0 }}</b
                ><span>人工 / 模型标签</span>
              </div>
            </div>
            <p
              v-if="selectionPreview.contains_mock_predictions"
              class="mock-note"
            >
              当前选择含 Mock 标签，清单中将保留明确标识。
            </p>
            <div class="label-chips">
              <el-tag
                v-for="(count, label) in selectionPreview.label_distribution"
                :key="label"
                >{{ label }} · {{ count }}</el-tag
              >
            </div>
            <p class="muted">
              预览后若标签或样本变化，发布会拒绝，需重新预览。完整文件校验在后台执行。{{
                selectionPreview.truncated ? "这里只显示前20个样本。" : ""
              }}
            </p>
            <el-table :data="selectionPreview.items" size="small"
              ><el-table-column
                prop="relative_path"
                label="源文件"
                min-width="170"
                show-overflow-tooltip /><el-table-column
                prop="start_sample"
                label="起始采样" /><el-table-column
                prop="label"
                label="类别" /><el-table-column
                prop="label_source"
                label="来源"
            /></el-table>
          </div>
        </section>
        <section class="panel version-list">
          <div class="panel-title">
            已发布版本 <span>仅显示完整封存版本</span>
          </div>
          <el-table
            :data="versions"
            highlight-current-row
            @row-click="viewVersion"
            ><el-table-column prop="version" label="版本" /><el-table-column
              prop="item_count"
              label="样本数"
            /><el-table-column label="人工 / 模型"
              ><template #default="{ row }"
                >{{ row.source_counts.HUMAN || 0 }} /
                {{ row.source_counts.MODEL || 0 }}</template
              ></el-table-column
            ><el-table-column label="发布时间" min-width="160"
              ><template #default="{ row }">{{
                new Date(row.created_at).toLocaleString("zh-CN")
              }}</template></el-table-column
            ><el-table-column label="操作" width="90"
              ><template #default="{ row }"
                ><el-button text type="primary" @click.stop="viewVersion(row)"
                  >查看版本</el-button
                ></template
              ></el-table-column
            ></el-table
          ><el-pagination
            v-model:current-page="versionPage"
            layout="prev, pager, next, total"
            :total="versionTotal"
            :page-size="10"
            @current-change="loadVersions"
          />
        </section>
        <section v-if="viewed" class="panel frozen-version">
          <div class="panel-title">
            版本 {{ viewed.version }} · 固定快照<el-button
              type="primary"
              :loading="downloading"
              @click="download"
              >下载 Manifest</el-button
            >
          </div>
          <div class="label-chips">
            <el-tag type="success"
              >已封存 · {{ viewed.item_count }} 个样本</el-tag
            ><el-tag
              v-for="(count, label) in viewed.label_distribution"
              :key="label"
              >{{ label }} · {{ count }}</el-tag
            >
          </div>
          <p class="muted">清单 SHA-256</p>
          <code class="hash-block">{{ viewed.manifest_sha256 }}</code>
          <p class="muted">
            以下内容来自发布时固定的成员记录，后续标签修改不影响这里。
          </p>
          <el-table :data="members"
            ><el-table-column
              prop="source.relative_path"
              label="原始文件"
              min-width="190"
              show-overflow-tooltip
            /><el-table-column
              prop="interval.start_sample"
              label="起始采样"
            /><el-table-column
              prop="interval.sample_count"
              label="长度"
            /><el-table-column prop="label" label="固定标签" /><el-table-column
              prop="label_source"
              label="来源"
            /><el-table-column label="标签版本"
              ><template #default="{ row }">{{
                row.annotation
                  ? `人工 v${row.annotation.version}`
                  : row.prediction?.model.version
              }}</template></el-table-column
            ></el-table
          ><el-pagination
            v-model:current-page="memberPage"
            layout="prev, pager, next, total"
            :total="viewed.item_count"
            :page-size="20"
            @current-change="loadMembers"
          />
        </section>
      </div>
      <section v-else class="panel select-empty">
        <div>▧</div>
        <h2>选择一个数据集</h2>
        <p>查看固定版本，或预览样本并发布新版本。</p>
      </section>
    </div>
  </div>
</template>
