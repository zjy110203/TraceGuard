<script setup lang="ts">
// SVG donut with a centred total. Segments are drawn as stroke-dash arcs on a
// single circle, which keeps the markup tiny and scales cleanly.
import { computed } from 'vue'

export interface DonutSegment {
  label: string
  value: number
  color: string
}

const props = withDefaults(
  defineProps<{ segments: DonutSegment[]; size?: number; thickness?: number; centerLabel?: string }>(),
  { size: 132, thickness: 13, centerLabel: '总计' },
)

const R = 42
const CIRC = 2 * Math.PI * R

const total = computed(() => props.segments.reduce((s, x) => s + x.value, 0))

const arcs = computed(() => {
  const t = total.value || 1
  let offset = 0
  return props.segments
    .filter((s) => s.value > 0)
    .map((s) => {
      const len = (s.value / t) * CIRC
      const arc = { ...s, dash: `${len} ${CIRC - len}`, offset: -offset }
      offset += len
      return arc
    })
})
</script>

<template>
  <div class="donut-wrap">
    <svg :width="size" :height="size" viewBox="0 0 100 100" role="img" :aria-label="centerLabel">
      <circle cx="50" cy="50" :r="R" fill="none" stroke="var(--tg-panel-3)" :stroke-width="thickness" />
      <circle
        v-for="a in arcs"
        :key="a.label"
        cx="50"
        cy="50"
        :r="R"
        fill="none"
        :stroke="a.color"
        :stroke-width="thickness"
        :stroke-dasharray="a.dash"
        :stroke-dashoffset="a.offset"
        stroke-linecap="butt"
        transform="rotate(-90 50 50)"
      />
      <text x="50" y="47" text-anchor="middle" class="donut-total">{{ total }}</text>
      <text x="50" y="60" text-anchor="middle" class="donut-sub">{{ centerLabel }}</text>
    </svg>
    <ul class="legend">
      <li v-for="s in segments" :key="s.label">
        <span class="swatch" :style="{ background: s.color }" />
        <span class="truncate">{{ s.label }}</span>
        <span class="mono val">{{ s.value }}</span>
      </li>
    </ul>
  </div>
</template>

<style scoped>
.donut-wrap {
  display: flex;
  align-items: center;
  gap: 20px;
  flex-wrap: wrap;
}

.donut-total {
  font-size: 19px;
  font-weight: 650;
  fill: var(--tg-text);
}

.donut-sub {
  font-size: 8px;
  fill: var(--tg-muted);
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

.legend {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 7px;
  font-size: 12px;
  min-width: 0;
  flex: 1 1 130px;
}

.legend li {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  color: var(--tg-text-2);
}

.swatch {
  width: 8px;
  height: 8px;
  border-radius: 2px;
  flex: 0 0 auto;
}

.val {
  margin-left: auto;
  color: var(--tg-text);
  font-variant-numeric: tabular-nums;
}
</style>
