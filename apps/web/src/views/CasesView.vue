<script setup lang="ts">
import { computed, ref } from 'vue'
import { api } from '../api/client'
import { useAsync } from '../composables/useAsync'
import StateBlock from '../components/StateBlock.vue'
import Pagination from '../components/Pagination.vue'
import PageHeader from '../components/PageHeader.vue'
import { navigate } from '../router'

const state = useAsync(() => api.cases())
const page = ref(1)
const pageSize = 10
const filter = ref('')

const matched = computed(() => {
  const all = state.data.value ?? []
  const q = filter.value.trim().toLowerCase()
  return q
    ? all.filter((c) => c.case_id.toLowerCase().includes(q) || c.title.toLowerCase().includes(q))
    : all
})
const rows = computed(() => matched.value.slice((page.value - 1) * pageSize, page.value * pageSize))
const total = computed(() => matched.value.length)
const emptyHint = computed(() =>
  filter.value.trim()
    ? '没有匹配的 TestCase。请调整筛选关键字后重试。'
    : '尚无 TestCase。将用例文件放入用例目录后点击刷新。',
)
</script>

<template>
  <section class="page">
    <PageHeader title="TestCase" subtitle="磁盘用例文件清单">
      <label class="sr-only" for="case-filter">按 case_id / 标题筛选</label>
      <input
        id="case-filter"
        v-model="filter"
        class="filter"
        placeholder="按 case_id / 标题筛选"
        @input="page = 1"
      />
      <button class="btn" type="button" @click="state.reload()">刷新</button>
    </PageHeader>

    <StateBlock
      :loading="state.loading.value"
      :error="state.error.value"
      :empty="!total"
      :empty-hint="emptyHint"
      :retry="state.reload"
    >
      <table>
        <thead>
          <tr><th>case_id</th><th>标题</th><th>版本</th><th>步骤</th><th>断言</th><th>来源</th></tr>
        </thead>
        <tbody>
          <tr v-for="row in rows" :key="`${row.case_id}-${row.version}`">
            <td>
              <a href="#" class="mono" @click.prevent="navigate(`/cases/${row.case_id}`)">
                {{ row.case_id }}
              </a>
            </td>
            <td>{{ row.title }}</td>
            <td>
              <a href="#" @click.prevent="navigate(`/cases/${row.case_id}/versions`)">v{{ row.version }}</a>
            </td>
            <td>{{ row.steps }}</td>
            <td>{{ row.assertions }}</td>
            <td class="muted">{{ row.compiled_from }}</td>
          </tr>
        </tbody>
      </table>
      <Pagination v-model:page="page" :page-size="pageSize" :total="total" />
    </StateBlock>
  </section>
</template>

<style scoped>
.filter {
  width: 200px;
}
</style>
