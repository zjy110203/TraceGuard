<script setup lang="ts">
import { api } from '../api/client'
import { useAsync } from '../composables/useAsync'
import StateBlock from '../components/StateBlock.vue'
import StatusPill from '../components/StatusPill.vue'
import PageHeader from '../components/PageHeader.vue'
import { navigate } from '../router'

const props = defineProps<{ runId: string }>()
const run = useAsync(() => api.run(props.runId))
</script>

<template>
  <section class="page">
    <PageHeader title="步骤执行" :subtitle="props.runId">
      <button class="btn" type="button" @click="navigate(`/runs/${props.runId}`)">返回 Run</button>
    </PageHeader>

    <StateBlock
      :loading="run.loading.value"
      :error="run.error.value"
      :empty="!run.data.value?.steps.length"
      empty-hint="该 Run 尚无步骤记录。等待执行产生步骤，或点击右上角返回 Run 查看整体状态。"
      :retry="run.reload"
    >
      <table>
        <thead><tr><th>#</th><th>step_id</th><th>action</th><th>结果</th><th>细节</th></tr></thead>
        <tbody>
          <tr v-for="s in run.data.value?.steps ?? []" :key="s.index">
            <td class="muted">{{ s.index }}</td>
            <td class="mono">{{ s.step_id }}</td>
            <td>{{ s.action }}</td>
            <td><StatusPill :value="s.ok ? 'PASSED' : 'FAILED'" /></td>
            <td class="small">{{ s.detail }}</td>
          </tr>
        </tbody>
      </table>
    </StateBlock>
  </section>
</template>
