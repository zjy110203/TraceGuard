<script setup lang="ts">
import { computed } from 'vue'
import { api } from '../api/client'
import { useAsync } from '../composables/useAsync'
import StateBlock from '../components/StateBlock.vue'
import PageHeader from '../components/PageHeader.vue'
import StatCard from '../components/StatCard.vue'
import StatusPill from '../components/StatusPill.vue'
import DonutChart from '../components/DonutChart.vue'
import BarList from '../components/BarList.vue'
import TrendBars from '../components/TrendBars.vue'
import EmptyState from '../components/EmptyState.vue'
import AppIcon from '../components/AppIcon.vue'
import { navigate } from '../router'
import type { RunBrief } from '../api/types'

const state = useAsync(() => api.overview())

const overview = computed(() => state.data.value)

const verdictTone: Record<string, string> = {
  PASSED: 'var(--tg-success)',
  FAILED: 'var(--tg-danger)',
  INCONCLUSIVE: 'var(--tg-warn)',
  NOT_EVALUATED: 'var(--tg-neutral)',
}

const verdictSegments = computed(() =>
  Object.entries(overview.value?.runs_by_verdict ?? {})
    .sort((a, b) => b[1] - a[1])
    .map(([label, value]) => ({ label, value, color: verdictTone[label] ?? 'var(--tg-c6)' })),
)

const statusTone: Record<string, string> = {
  COMPLETED: 'var(--tg-c3)',
  RUNNING: 'var(--tg-c1)',
  QUEUED: 'var(--tg-c4)',
  FAILED: 'var(--tg-c5)',
  TIMED_OUT: 'var(--tg-c5)',
  CANCELLED: 'var(--tg-neutral)',
}

const statusBars = computed(() =>
  Object.entries(overview.value?.runs_by_status ?? {})
    .sort((a, b) => b[1] - a[1])
    .map(([label, value]) => ({ label, value, color: statusTone[label] ?? 'var(--tg-c2)' })),
)

const trend = computed(() => overview.value?.runs_daily ?? [])
const recentRuns = computed<RunBrief[]>(() => overview.value?.recent_runs ?? [])

const evaluated = computed(() => overview.value?.evaluated ?? 0)
const successRate = computed(() => overview.value?.success_rate)
const failureRate = computed(() => overview.value?.failure_rate)

function rateText(v: number | null | undefined): string {
  return v === null || v === undefined ? '—' : `${v}%`
}

function fmtTime(iso: string | null): string {
  if (!iso) return '—'
  return iso.replace('T', ' ').slice(5, 16)
}

const expTotal = computed(() => overview.value?.explorations?.total ?? 0)
const expScripted = computed(() => overview.value?.explorations?.scripted ?? 0)
const expLlm = computed(() => overview.value?.explorations?.llm ?? 0)
</script>

<template>
  <section class="page">
    <PageHeader
      title="项目总览"
      subtitle="Agentic Web UI Testing & Quality Intelligence —— 真实运行数据，不合成指标"
    >
      <button class="btn" type="button" :disabled="state.loading.value" @click="state.reload()">
        <AppIcon name="refresh" :size="14" /> 刷新
      </button>
      <button class="btn" type="button" @click="navigate('/system')">
        <AppIcon name="system" :size="14" /> 系统状态
      </button>
    </PageHeader>

    <StateBlock :loading="state.loading.value" :error="state.error.value" :retry="state.reload">
      <div class="grid-cards kpis">
        <StatCard
          label="运行总数"
          :value="overview?.runs ?? 0"
          :hint="`${overview?.tasks ?? 0} 个任务`"
          icon="runs"
          :trend="trend.map((p) => p.count)"
          clickable
          @open="navigate('/runs')"
        />
        <StatCard
          label="成功率"
          :value="rateText(successRate)"
          :hint="evaluated ? `基于 ${evaluated} 次已判定运行` : '尚无已判定运行'"
          tone="success"
          icon="check"
          clickable
          @open="navigate('/runs')"
        />
        <StatCard
          label="失败率"
          :value="rateText(failureRate)"
          :hint="evaluated ? `基于 ${evaluated} 次已判定运行` : '尚无已判定运行'"
          tone="danger"
          icon="alert"
          clickable
          @open="navigate('/runs')"
        />
        <StatCard
          label="测试用例"
          :value="overview?.cases ?? 0"
          hint="正式资产（Case Library）"
          icon="cases"
          clickable
          @open="navigate('/cases')"
        />
        <StatCard
          label="证据记录"
          :value="overview?.evidence ?? 0"
          :hint="`${overview?.events ?? 0} 条事件`"
          icon="evidence"
          clickable
          @open="navigate('/evidence')"
        />
        <StatCard
          label="待审批修复"
          :value="overview?.repairs_pending ?? 0"
          :hint="(overview?.repairs_pending ?? 0) > 0 ? '需要人工审核' : '队列已清空'"
          :tone="(overview?.repairs_pending ?? 0) > 0 ? 'warn' : 'default'"
          icon="repairs"
          clickable
          @open="navigate('/repairs')"
        />
      </div>

      <div class="main-grid">
        <div class="col-main">
          <div class="card">
            <div class="card-head">
              <h2>运行趋势</h2>
              <span class="small muted">按创建时间统计</span>
            </div>
            <div class="card-body">
              <TrendBars v-if="trend.length" :points="trend" label="每日运行数" />
              <EmptyState
                v-else
                icon="runs"
                title="暂无运行数据"
                hint="提交第一个 Run 后，这里会显示真实的每日执行分布。"
                compact
              />
            </div>
          </div>

          <div class="card flush">
            <div class="card-head">
              <h2>最近运行</h2>
              <button class="btn sm ghost" type="button" @click="navigate('/runs')">
                全部 <AppIcon name="arrowRight" :size="12" />
              </button>
            </div>
            <EmptyState
              v-if="!recentRuns.length"
              icon="runs"
              title="尚无执行记录"
              hint="在任务队列创建任务并提交 Run 后，最近执行会出现在这里。"
              compact
            >
              <button class="btn sm" type="button" @click="navigate('/tasks')">前往任务队列</button>
            </EmptyState>
            <div v-else class="table-wrap bare">
              <table>
                <thead>
                  <tr>
                    <th>Run</th>
                    <th>Task</th>
                    <th>状态</th>
                    <th>Verdict</th>
                    <th class="num">时间</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="r in recentRuns" :key="r.run_id">
                    <td>
                      <a :href="`#/runs/${r.run_id}`" @click.prevent="navigate(`/runs/${r.run_id}`)">
                        <span class="mono">{{ r.run_id.slice(0, 20) }}…</span>
                      </a>
                    </td>
                    <td class="mono small">{{ r.task_id }}</td>
                    <td><StatusPill :value="r.status" /></td>
                    <td><StatusPill :value="r.test_verdict || 'NOT_EVALUATED'" /></td>
                    <td class="small muted num">{{ fmtTime(r.created_at) }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </div>

        <div class="col-side">
          <div class="card">
            <div class="card-head">
              <h2>Verdict 分布</h2>
            </div>
            <div class="card-body">
              <DonutChart
                v-if="verdictSegments.length"
                :segments="verdictSegments"
                center-label="运行"
              />
              <EmptyState v-else icon="diagnosis" title="尚无判定结果" hint="运行完成后生成 Verdict。" compact />
            </div>
          </div>

          <div class="card">
            <div class="card-head">
              <h2>Run 状态</h2>
            </div>
            <div class="card-body">
              <BarList :data="statusBars" empty-hint="尚无 Run 记录。" />
            </div>
          </div>

          <div class="card">
            <div class="card-head">
              <h2>探索任务</h2>
              <button class="btn sm ghost" type="button" @click="navigate('/explorer')">
                进入 <AppIcon name="arrowRight" :size="12" />
              </button>
            </div>
            <div class="card-body">
              <p class="exp-total">
                <strong>{{ expTotal }}</strong>
                <span class="muted small"> 次探索</span>
              </p>
              <ul class="kv">
                <li>
                  <span><span class="pill plain info">确定性</span> Scripted</span>
                  <b>{{ expScripted }}</b>
                </li>
                <li>
                  <span><span class="pill plain neutral">未启用</span> LLM</span>
                  <b>{{ expLlm }}</b>
                </li>
              </ul>
              <p class="hint tight">
                本轮不执行真实模型调用；LLM 列恒为 0 直到接口获批并验证。
              </p>
            </div>
          </div>
        </div>
      </div>

      <p class="hint metric-note">
        统计口径：全部数值来自后端 <code>/overview</code> 实时计算。成功率 / 失败率的分母是
        <b>已得出判定（PASSED / FAILED / INCONCLUSIVE）</b> 的运行数；无已判定运行时显示
        <code>—</code>，而不是伪造 0%。
      </p>
    </StateBlock>
  </section>
</template>

<style scoped>
.kpis {
  margin-bottom: 18px;
  /* Six KPIs must land as a balanced block (3x2), never 5 + 1 orphan. */
  grid-template-columns: repeat(3, minmax(0, 1fr));
}

@media (max-width: 1160px) {
  .kpis {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 560px) {
  .kpis {
    grid-template-columns: 1fr;
  }
}

.main-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.75fr) minmax(0, 1fr);
  gap: 16px;
  align-items: start;
}

.col-main,
.col-side {
  display: flex;
  flex-direction: column;
  gap: 16px;
  min-width: 0;
}

.col-main .card,
.col-side .card {
  margin-bottom: 0;
}

.table-wrap.bare {
  border: 0;
  border-radius: 0;
}

.exp-total strong {
  font-size: 24px;
  font-weight: 650;
  font-variant-numeric: tabular-nums;
}

.exp-total {
  margin-bottom: 6px;
}

.hint.tight {
  margin: 8px 0 0;
}

.metric-note {
  margin-top: 20px;
  padding-top: 14px;
  border-top: 1px solid var(--tg-border);
  max-width: 90ch;
}

@media (max-width: 1000px) {
  .main-grid {
    grid-template-columns: 1fr;
  }
}
</style>
