<script setup>
import { onMounted, ref } from "vue";
const props = defineProps({
  api: { type: [Object, Function], required: true },
  user: { type: Object, required: true },
});
const jobs = ref([]),
  total = ref(0),
  page = ref(1),
  status = ref(""),
  type = ref(""),
  selected = ref(null),
  events = ref([]),
  error = ref(""),
  message = ref(""),
  busy = ref(false);
const names = {
  PENDING: "排队中",
  RUNNING: "处理中",
  RETRY_WAIT: "等待重试",
  SUCCEEDED: "已完成",
  FAILED: "失败",
};
const short = (id) => id.slice(0, 8);
const fail = (exception) => {
  error.value =
    exception.response?.data?.error?.message || "读取任务失败，请重试。";
};
async function reload() {
  try {
    const { data } = await props.api.get("/jobs", {
      params: {
        limit: 20,
        offset: (page.value - 1) * 20,
        status: status.value || undefined,
        job_type: type.value || undefined,
      },
    });
    jobs.value = data.items;
    total.value = data.total;
    if (selected.value) await show(selected.value);
  } catch (exception) {
    fail(exception);
  }
}
async function filter() {
  page.value = 1;
  await reload();
}
async function show(row) {
  try {
    const [detail, log] = await Promise.all([
      props.api.get(`/jobs/${row.id}`),
      props.api.get(`/jobs/${row.id}/events`, { params: { limit: 100 } }),
    ]);
    selected.value = detail.data;
    events.value = log.data.items;
  } catch (exception) {
    fail(exception);
  }
}
async function rerun(row) {
  busy.value = true;
  error.value = "";
  message.value = "";
  try {
    const { data } = await props.api.post(
      `/jobs/${row.id}/reruns`,
      {},
      { headers: { "Idempotency-Key": crypto.randomUUID() } },
    );
    message.value = `已新建任务 ${short(data.id)}，原失败记录保留。`;
    selected.value = data;
    await reload();
  } catch (exception) {
    fail(exception);
  } finally {
    busy.value = false;
  }
}
onMounted(reload);
defineExpose({ reload });
</script>
<template>
  <div class="job-center">
    <el-alert
      v-if="error"
      :title="error"
      type="error"
      :closable="false"
      class="notice"
    /><el-alert
      v-if="message"
      :title="message"
      type="success"
      :closable="false"
      class="notice"
    />
    <div class="asset-filters">
      <el-select
        v-model="status"
        placeholder="全部状态"
        aria-label="任务状态"
        @change="filter"
        ><el-option label="全部状态" value="" /><el-option
          v-for="(label, value) in names"
          :key="value"
          :value="value"
          :label="label" /></el-select
      ><el-select
        v-model="type"
        placeholder="全部类型"
        aria-label="任务类型"
        @change="filter"
        ><el-option label="全部类型" value="" /><el-option
          v-for="value in ['SCAN', 'SEGMENT', 'QUALITY', 'INFER', 'PUBLISH']"
          :key="value"
          :value="value"
          :label="value" /></el-select
      ><span class="muted">查看状态、重试次数与处理历史。</span>
    </div>
    <section class="panel">
      <el-table :data="jobs" highlight-current-row @row-click="show"
        ><el-table-column label="任务编号" min-width="110"
          ><template #default="{ row }">{{
            short(row.id)
          }}</template></el-table-column
        ><el-table-column prop="job_type" label="类型" /><el-table-column
          label="状态"
          min-width="110"
          ><template #default="{ row }"
            ><el-tag
              :type="
                row.status === 'FAILED'
                  ? 'danger'
                  : row.status === 'SUCCEEDED'
                    ? 'success'
                    : 'info'
              "
              >{{ names[row.status] }}</el-tag
            ></template
          ></el-table-column
        ><el-table-column label="尝试次数"
          ><template #default="{ row }"
            >{{ row.attempts }} / {{ row.max_attempts }}</template
          ></el-table-column
        ><el-table-column
          prop="execution_version"
          label="执行版本"
        /><el-table-column
          prop="last_error"
          label="错误原因"
          min-width="180"
          show-overflow-tooltip
        /><el-table-column label="操作" min-width="110"
          ><template #default="{ row }"
            ><el-button
              v-if="
                row.status === 'FAILED' &&
                (user.role === 'ADMIN' ||
                  (user.role === 'RESEARCHER' && row.job_type === 'PUBLISH'))
              "
              text
              type="primary"
              :disabled="busy"
              @click.stop="rerun(row)"
              >重新发起</el-button
            ><el-button v-else text @click.stop="show(row)"
              >详情</el-button
            ></template
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
        任务 {{ short(selected.id)
        }}<el-tag>{{ names[selected.status] }}</el-tag>
      </div>
      <div class="metadata-grid">
        <span>任务编号</span><code>{{ selected.id }}</code
        ><span>执行者 / 租约到期</span
        ><code
          >{{ selected.worker_id || "—" }} /
          {{ selected.lease_until || "—" }}</code
        ><span>重发来源</span
        ><code>{{ selected.parent_job_id || "首次发起" }}</code>
      </div>
      <h3>处理参数</h3>
      <pre class="json-block">{{
        JSON.stringify(selected.payload, null, 2)
      }}</pre>
      <h3>结果</h3>
      <pre class="json-block">{{
        selected.result
          ? JSON.stringify(selected.result, null, 2)
          : selected.last_error || "尚无正式结果"
      }}</pre>
      <h3>事件记录（最多显示100条）</h3>
      <div v-for="item in events" :key="item.id" class="observation-row">
        <code>{{ item.event }} · v{{ item.execution_version }}</code
        ><span>{{ new Date(item.created_at).toLocaleString("zh-CN") }}</span>
      </div>
    </section>
  </div>
</template>
