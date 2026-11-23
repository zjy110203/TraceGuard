<script setup lang="ts">
import { ref } from 'vue'
import { api } from '../api/client'
import { messageOf, useAsync } from '../composables/useAsync'
import StateBlock from '../components/StateBlock.vue'
import PageHeader from '../components/PageHeader.vue'
import { navigate } from '../router'

const state = useAsync(() => api.intents())

const form = ref({ intent_id: '', description: '', source_ref: '' })
const creating = ref(false)
const createError = ref('')

async function submit(): Promise<void> {
  creating.value = true
  createError.value = ''
  try {
    await api.createIntent({ ...form.value })
    form.value = { intent_id: '', description: '', source_ref: '' }
    await state.reload()
  } catch (err) {
    createError.value = messageOf(err)
  } finally {
    creating.value = false
  }
}
</script>

<template>
  <section class="page">
    <PageHeader title="TestIntent" subtitle="测试意图清单与新增">
      <button class="btn" type="button" @click="state.reload()">刷新</button>
    </PageHeader>
    <p class="hint">
      来源为 <code>case</code> 的意图直接取自磁盘上的 TestCase 文件；<code>library</code> 为本页新增并持久化的意图。
    </p>

    <form class="form" @submit.prevent="submit">
      <label class="sr-only" for="intent-id">intent_id</label>
      <input id="intent-id" v-model="form.intent_id" placeholder="intent_id（如 INT-002）" required />
      <label class="sr-only" for="intent-desc">目标描述</label>
      <input id="intent-desc" v-model="form.description" placeholder="目标描述" required />
      <label class="sr-only" for="intent-src">来源引用</label>
      <input id="intent-src" v-model="form.source_ref" placeholder="来源引用（可选）" />
      <button class="btn primary" type="button" :disabled="creating" @click="submit">
        {{ creating ? '提交中…' : '新增意图' }}
      </button>
    </form>
    <p v-if="createError" class="error-text">{{ createError }}</p>

    <StateBlock
      :loading="state.loading.value"
      :error="state.error.value"
      :empty="!state.data.value?.length"
      empty-hint="尚无 TestIntent。用上方表单新增一条（如 INT-002），或先创建 TestCase 自动生成。"
      :retry="state.reload"
    >
      <div class="table-wrap"><table>
        <thead>
          <tr><th>intent_id</th><th>描述</th><th>来源</th><th>关联用例</th><th></th></tr>
        </thead>
        <tbody>
          <tr v-for="row in state.data.value ?? []" :key="row.intent_id">
            <td class="mono">{{ row.intent_id }}</td>
            <td>{{ row.description }}</td>
            <td><span class="tag">{{ row.origin }}</span></td>
            <td>
              <a v-if="row.case_id" href="#" @click.prevent="navigate(`/cases/${row.case_id}`)">
                {{ row.case_id }} v{{ row.case_version }}
              </a>
              <span v-else class="muted">未关联</span>
            </td>
            <td class="mono muted small">{{ row.source_ref || '—' }}</td>
          </tr>
        </tbody>
      </table></div>
    </StateBlock>
  </section>
</template>

<style scoped>
.form {
  display: grid;
  /* Two columns on narrow screens; a fixed 4-column track cannot fit 390px. */
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
  margin-bottom: 8px;
}
.form input {
  min-width: 0;
}
@media (min-width: 900px) {
  .form {
    grid-template-columns: 180px minmax(0, 1fr) 180px auto;
  }
}
.tag {
  font-size: 12px;
  border: 1px solid var(--tg-border);
  border-radius: 9px;
  padding: 0 6px;
  color: var(--tg-muted);
}
</style>
