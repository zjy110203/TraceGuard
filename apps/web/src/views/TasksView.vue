<script setup lang="ts">
import { computed, ref } from 'vue'
import { api } from '../api/client'
import { messageOf, useAsync } from '../composables/useAsync'
import StateBlock from '../components/StateBlock.vue'
import StatusPill from '../components/StatusPill.vue'
import Pagination from '../components/Pagination.vue'
import PageHeader from '../components/PageHeader.vue'
import { navigate } from '../router'

const tasks = useAsync(() => api.tasks())
const cases = useAsync(() => api.cases())

const page = ref(1)
const pageSize = 10
const rows = computed(() =>
  (tasks.data.value ?? []).slice((page.value - 1) * pageSize, page.value * pageSize),
)

const form = ref({ task_id: '', case_id: '', title: '' })
const timeoutS = ref(60)
const busy = ref(false)
const actionError = ref('')
const notice = ref('')

async function createTask(): Promise<void> {
  busy.value = true
  actionError.value = ''
  notice.value = ''
  try {
    const created = await api.createTask({ ...form.value })
    notice.value = `已创建 Task ${created.task_id}`
    form.value = { task_id: '', case_id: '', title: '' }
    await tasks.reload()
  } catch (err) {
    actionError.value = messageOf(err)
  } finally {
    busy.value = false
  }
}

async function launch(taskId: string): Promise<void> {
  busy.value = true
  actionError.value = ''
  notice.value = ''
  try {
    const run = await api.submitRun(taskId, { timeout_s: Number(timeoutS.value) || 60 })
    notice.value = `已提交 ${run.run_id}（每次提交都创建新 Run）`
  } catch (err) {
    actionError.value = messageOf(err)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <section class="page">
    <PageHeader title="Task" subtitle="逻辑任务队列与 Run 提交">
      <button class="btn" type="button" @click="tasks.reload()">刷新</button>
    </PageHeader>
    <p class="hint">Task 是逻辑任务；重试不会修改旧 Run，而是新建一次 Run。</p>

    <form class="form" @submit.prevent="createTask">
      <label class="sr-only" for="task-id">task_id</label>
      <input id="task-id" v-model="form.task_id" placeholder="task_id" required />
      <label class="sr-only" for="task-case">case_id</label>
      <select id="task-case" v-model="form.case_id" required>
        <option value="" disabled>选择 case_id</option>
        <option v-for="c in cases.data.value ?? []" :key="c.case_id" :value="c.case_id">
          {{ c.case_id }} · {{ c.title }}
        </option>
      </select>
      <label class="sr-only" for="task-title">标题</label>
      <input id="task-title" v-model="form.title" placeholder="标题" required />
      <label class="sr-only" for="task-timeout">超时秒数</label>
      <input id="task-timeout" v-model.number="timeoutS" type="number" min="1" placeholder="超时秒数" />
      <button class="btn primary" type="button" :disabled="busy" @click="createTask">
        {{ busy ? '处理中…' : '创建 Task' }}
      </button>
    </form>
    <p v-if="actionError" class="error-text">{{ actionError }}</p>
    <p v-if="notice" class="notice-text">{{ notice }}</p>

    <StateBlock
      :loading="tasks.loading.value"
      :error="tasks.error.value"
      :empty="!tasks.data.value?.length"
      empty-hint="尚无 Task。用上方表单创建第一个 Task，再点击行内「提交 Run」执行。"
      :retry="tasks.reload"
    >
      <table>
        <thead><tr><th>task_id</th><th>case_id</th><th>标题</th><th>状态</th><th>创建时间</th><th></th></tr></thead>
        <tbody>
          <tr v-for="row in rows" :key="row.task_id">
            <td class="mono">
              <a href="#" @click.prevent="navigate(`/runs?task=${encodeURIComponent(row.task_id)}`)">
                {{ row.task_id }}
              </a>
            </td>
            <td class="mono">{{ row.case_id }}</td>
            <td>{{ row.title }}</td>
            <td><StatusPill :value="row.status" /></td>
            <td class="muted small">{{ row.created_at }}</td>
            <td><button class="btn" type="button" :disabled="busy" @click="launch(row.task_id)">提交 Run</button></td>
          </tr>
        </tbody>
      </table>
      <Pagination v-model:page="page" :page-size="pageSize" :total="tasks.data.value?.length ?? 0" />
    </StateBlock>
  </section>
</template>

<style scoped>
.form {
  display: grid;
  grid-template-columns:
    minmax(140px, 180px)
    minmax(220px, 1.4fr)
    minmax(160px, 1fr)
    minmax(110px, 130px)
    auto;
  gap: 8px;
  margin-bottom: 8px;
}
@media (max-width: 900px) {
  .form {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
</style>
