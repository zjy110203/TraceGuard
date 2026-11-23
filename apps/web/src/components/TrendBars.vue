<script setup lang="ts">
// Responsive column chart built from plain elements (no SVG text stretching):
// each column is a flex item, so it reflows correctly at every viewport width.
import { computed } from 'vue'

const props = withDefaults(
  defineProps<{
    points: { date: string; count: number }[]
    label?: string
    tone?: string
  }>(),
  { label: '每日运行数', tone: 'var(--tg-c1)' },
)

const max = computed(() => Math.max(1, ...props.points.map((p) => p.count)))
const total = computed(() => props.points.reduce((s, p) => s + p.count, 0))
const peak = computed(() => props.points.reduce((a, b) => (b.count > a.count ? b : a), props.points[0]))

function shortDate(iso: string): string {
  return iso.slice(5).replace('-', '/')
}

// Label every third day so the axis stays readable on 390px screens.
function tickIndex(i: number): boolean {
  return i % 3 === 0 || i === props.points.length - 1
}
</script>

<template>
  <div class="trend">
    <div class="trend-head">
      <div>
        <p class="micro">{{ label }}</p>
        <p class="trend-total">
          <strong>{{ total }}</strong>
          <span class="muted small"> · 近 {{ points.length }} 天</span>
        </p>
      </div>
      <p v-if="peak && peak.count > 0" class="small muted">
        峰值 {{ peak.count }} · {{ shortDate(peak.date) }}
      </p>
    </div>

    <div class="plot" role="img" :aria-label="`${label}，共 ${total} 次`">
      <div v-for="(p, i) in points" :key="p.date" class="col" :title="`${p.date} · ${p.count}`">
        <span
          class="bar"
          :class="{ zero: p.count === 0 }"
          :style="{ height: `${(p.count / max) * 100}%`, background: tone }"
        />
        <span v-if="tickIndex(i)" class="tick">{{ shortDate(p.date) }}</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.trend {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.trend-head {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 12px;
}

.trend-total {
  font-size: 13px;
  color: var(--tg-text-2);
}

.trend-total strong {
  font-size: 20px;
  font-weight: 650;
  color: var(--tg-text);
  font-variant-numeric: tabular-nums;
}

.plot {
  display: flex;
  align-items: flex-end;
  gap: 3px;
  height: 96px;
  padding-top: 4px;
  border-bottom: 1px solid var(--tg-border);
}

.col {
  flex: 1 1 0;
  min-width: 0;
  height: 100%;
  display: flex;
  flex-direction: column;
  justify-content: flex-end;
  align-items: center;
  position: relative;
}

.bar {
  width: 100%;
  max-width: 26px;
  border-radius: 3px 3px 0 0;
  min-height: 2px;
  transition: height 0.35s ease;
  opacity: 0.9;
}

.col:hover .bar {
  opacity: 1;
}

.bar.zero {
  background: var(--tg-panel-3) !important;
  min-height: 2px;
}

.tick {
  position: absolute;
  top: calc(100% + 5px);
  font-size: 9.5px;
  color: var(--tg-faint);
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}

.plot {
  margin-bottom: 16px;
}
</style>
