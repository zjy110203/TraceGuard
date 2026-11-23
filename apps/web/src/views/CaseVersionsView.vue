<script setup lang="ts">
import { api } from '../api/client'
import { useAsync } from '../composables/useAsync'
import StateBlock from '../components/StateBlock.vue'
import PageHeader from '../components/PageHeader.vue'
import { navigate } from '../router'

const props = defineProps<{ caseId: string }>()
const state = useAsync(() => api.caseDetail(props.caseId))
</script>

<template>
  <section class="page">
    <PageHeader title="版本历史" :subtitle="props.caseId">
      <button class="btn" type="button" @click="navigate(`/cases/${props.caseId}`)">返回详情</button>
    </PageHeader>

    <StateBlock
      :loading="state.loading.value"
      :error="state.error.value"
      :empty="!state.data.value?.versions.length"
      empty-hint="该 case_id 暂无版本记录。用例编译晋升后版本会出现在这里。"
      :retry="state.reload"
    >
      <p class="hint">版本为 append-only：晋升只会新增版本，历史版本文件不会被覆盖。</p>
      <div class="table-wrap"><table>
        <thead><tr><th>版本</th><th>文件</th><th>编译来源</th><th>当前</th></tr></thead>
        <tbody>
          <tr v-for="v in state.data.value?.versions ?? []" :key="v.version">
            <td class="mono">v{{ v.version }}</td>
            <td class="mono">{{ v.file }}</td>
            <td class="muted">{{ v.compiled_from }}</td>
            <td>
              <span v-if="v.version === state.data.value?.version" class="cur">当前正式版</span>
              <span v-else class="muted">历史</span>
            </td>
          </tr>
        </tbody>
      </table></div>
    </StateBlock>
  </section>
</template>

<style scoped>
.cur {
  font-size: 12px;
  color: var(--tg-accent);
}
</style>
