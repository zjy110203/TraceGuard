<script setup lang="ts">
// Horizontal bar list. Chosen over SVG bars because labels stay selectable,
// the layout is responsive without measurement, and long Chinese labels never
// overflow — each row is a grid with a fixed label column.
import { computed } from 'vue'

export interface BarDatum {
  label: string
  value: number
  color?: string
}

const props = withDefaults(
  defineProps<{ data: BarDatum[]; max?: number; total?: number; emptyHint?: string }>(),
  { emptyHint: '暂无数据' },
)

const rows = computed(() => {
  const total = props.total ?? props.data.reduce((s, d) => s + d.value, 0)
  const max = props.max ?? Math.max(1, ...props.data.map((d) => d.value))
  return props.data.map((d, i) => ({
    ...d,
    pct: Math.round((d.value / max) * 100),
    share: total > 0 ? Math.round((d.value / total) * 100) : 0,
    key: `${d.label}-${i}`,
  }))
})
</script>

<template>
  <div v-if="!data.length" class="empty small">{{ emptyHint }}</div>
  <ul v-else class="bars">
    <li v-for="r in rows" :key="r.key">
      <span class="bar-label truncate" :title="r.label">{{ r.label }}</span>
      <span class="bar-track">
        <span
          class="bar-fill"
          :style="{ width: `${Math.max(r.pct, r.value > 0 ? 2 : 0)}%`, background: r.color ?? 'var(--tg-c1)' }"
        />
      </span>
      <span class="bar-value mono">{{ r.value }}</span>
      <span class="bar-share small muted">{{ r.share }}%</span>
    </li>
  </ul>
</template>

<style scoped>
.bars {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 9px;
}

.bars li {
  display: grid;
  grid-template-columns: minmax(64px, 34%) 1fr 32px 34px;
  align-items: center;
  gap: 10px;
  min-width: 0;
}

.bar-label {
  font-size: 12px;
  color: var(--tg-text-2);
}

.bar-track {
  height: 7px;
  border-radius: 999px;
  background: var(--tg-panel-3);
  overflow: hidden;
  min-width: 0;
}

.bar-fill {
  display: block;
  height: 100%;
  border-radius: 999px;
  transition: width 0.4s ease;
}

.bar-value {
  text-align: right;
  font-size: 12px;
  color: var(--tg-text);
  font-variant-numeric: tabular-nums;
}

.bar-share {
  text-align: right;
  font-variant-numeric: tabular-nums;
}

@media (max-width: 480px) {
  .bars li {
    grid-template-columns: minmax(52px, 40%) 1fr 26px;
  }
  .bar-share {
    display: none;
  }
}
</style>
