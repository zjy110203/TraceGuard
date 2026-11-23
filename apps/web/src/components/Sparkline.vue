<script setup lang="ts">
// Pure-SVG sparkline: no chart library, no canvas, crisp at any DPR.
// Values are normalised to the box; a flat series renders as a centred line
// rather than dividing by zero.
import { computed } from 'vue'

const props = withDefaults(
  defineProps<{
    values: number[]
    width?: number
    height?: number
    color?: string
    fill?: boolean
  }>(),
  { width: 120, height: 32, color: 'var(--tg-accent)', fill: true },
)

const box = computed(() => ({ w: props.width, h: props.height, pad: 2 }))

const geometry = computed(() => {
  const { w, h, pad } = box.value
  const vs = props.values.length ? props.values : [0]
  const max = Math.max(...vs)
  const min = Math.min(...vs)
  const span = max - min || 1
  const stepX = vs.length > 1 ? (w - pad * 2) / (vs.length - 1) : 0
  const pts = vs.map((v, i) => {
    const x = pad + i * stepX
    const y = pad + (h - pad * 2) * (1 - (v - min) / span)
    return [x, y] as const
  })
  const line = pts.map(([x, y], i) => `${i === 0 ? 'M' : 'L'}${x.toFixed(2)} ${y.toFixed(2)}`).join(' ')
  const area = `${line} L${pts[pts.length - 1][0].toFixed(2)} ${h - pad} L${pts[0][0].toFixed(2)} ${h - pad} Z`
  return { line, area, last: pts[pts.length - 1] }
})

const gradId = `spark-${Math.random().toString(36).slice(2, 8)}`
</script>

<template>
  <svg :width="width" :height="height" :viewBox="`0 0 ${width} ${height}`" role="img" aria-hidden="true">
    <defs>
      <linearGradient :id="gradId" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" :stop-color="color" stop-opacity="0.22" />
        <stop offset="100%" :stop-color="color" stop-opacity="0" />
      </linearGradient>
    </defs>
    <path v-if="fill" :d="geometry.area" :fill="`url(#${gradId})`" stroke="none" />
    <path
      :d="geometry.line"
      fill="none"
      :stroke="color"
      stroke-width="1.6"
      stroke-linecap="round"
      stroke-linejoin="round"
    />
    <circle :cx="geometry.last[0]" :cy="geometry.last[1]" r="2" :fill="color" stroke="none" />
  </svg>
</template>
