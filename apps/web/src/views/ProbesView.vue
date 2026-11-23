<script setup lang="ts">
import { api } from '../api/client'
import { useAsync } from '../composables/useAsync'
import StateBlock from '../components/StateBlock.vue'
import StatusPill from '../components/StatusPill.vue'
import PageHeader from '../components/PageHeader.vue'

const registry = useAsync(() => api.probes())
const audit = useAsync(() => api.probeAudit())
</script>

<template>
  <section class="page">
    <PageHeader title="Probe 审计" subtitle="探针注册表与调用审计">
      <button
        class="btn"
        type="button"
        @click="registry.reload(); audit.reload()"
      >
        刷新
      </button>
    </PageHeader>
    <p class="hint">
      探针只能只读；每次调用都要过五项校验（权限/范围/风险/预算/参数），拒绝也留审计。
    </p>

    <h2>探针注册表（{{ registry.data.value?.length ?? 0 }}）</h2>
    <StateBlock
      :loading="registry.loading.value"
      :error="registry.error.value"
      :empty="!registry.data.value?.length"
      empty-hint="注册表为空。探针注册后才会出现在这里。"
      :retry="registry.reload"
    >
      <div class="table-wrap"><table>
        <thead>
          <tr>
            <th>probe_id</th><th>范围</th><th>风险</th><th>只读</th><th>副作用</th><th>预估成本</th><th>超时</th><th>权限</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="p in registry.data.value ?? []" :key="p.probe_id">
            <td class="mono">{{ p.probe_id }}</td>
            <td>{{ p.target_scope }}</td>
            <td><StatusPill :value="p.risk_level" /></td>
            <td>{{ p.read_only ? '是' : '否' }}</td>
            <td>{{ p.side_effects }}</td>
            <td>{{ p.estimated_cost }}</td>
            <td>{{ p.timeout }}s</td>
            <td class="mono small muted">{{ p.permission_required }}</td>
          </tr>
        </tbody>
      </table></div>
    </StateBlock>

    <h2>调用审计（{{ audit.data.value?.length ?? 0 }}）</h2>
    <StateBlock
      :loading="audit.loading.value"
      :error="audit.error.value"
      :empty="!audit.data.value?.length"
      empty-hint="本次会话尚无探针调用记录。调用探针后审计条目会出现在这里。"
      :retry="audit.reload"
    >
      <div class="table-wrap"><table>
        <thead><tr><th>probe_id</th><th>调用方</th><th>结果</th><th>校验项</th><th>原因</th><th>时间</th></tr></thead>
        <tbody>
          <tr v-for="(a, i) in audit.data.value ?? []" :key="i">
            <td class="mono">{{ a.probe_id }}</td>
            <td>{{ a.caller }}</td>
            <td><StatusPill :value="a.decision" /></td>
            <td class="mono small">{{ a.check }}</td>
            <td class="small muted">{{ a.reason }}</td>
            <td class="small muted">{{ a.at }}</td>
          </tr>
        </tbody>
      </table></div>
    </StateBlock>
  </section>
</template>
