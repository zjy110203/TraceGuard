<script setup lang="ts">
// Inline, dependency-free icon set (24x24 grid, 1.7px stroke, round caps).
// Why not an icon package: the shell needs ~25 glyphs and a new npm dependency
// would cost bundle weight and a supply-chain review for no functional gain.
// Circles are expressed as arcs so every glyph renders through one <path> loop.
import { computed } from 'vue'

const props = withDefaults(
  defineProps<{ name: string; size?: number | string; strokeWidth?: number }>(),
  { size: 16, strokeWidth: 1.7 },
)

const PATHS: Record<string, string> = {
  // nav
  overview: 'M3 3h7v7H3z|M14 3h7v7h-7z|M14 14h7v7h-7z|M3 14h7v7H3z',
  intent:
    'M12 2a10 10 0 1 0 0 20a10 10 0 1 0 0-20|M12 6a6 6 0 1 0 0 12a6 6 0 1 0 0-12|M12 10a2 2 0 1 0 0 4a2 2 0 1 0 0-4',
  candidate: 'M6 3v12|M6 21a3 3 0 1 0 0-6a3 3 0 0 0 0 6|M18 9a3 3 0 1 0 0-6a3 3 0 0 0 0 6|M18 9a9 9 0 0 1-9 9',
  cases: 'M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z|M14 2v6h6|M8 13h8|M8 17h5',
  explorer: 'M12 2a10 10 0 1 0 0 20a10 10 0 1 0 0-20|M16.2 7.8l-2.1 6.4-6.4 2.1 2.1-6.4z',
  task: 'M10 6h11|M10 12h11|M10 18h11|M3 6l1.5 1.5L7 5|M3 12l1.5 1.5L7 11|M3 18l1.5 1.5L7 17',
  runs: 'M12 2a10 10 0 1 0 0 20a10 10 0 1 0 0-20|M10 8l6 4-6 4z',
  events: 'M22 12h-4l-3 9L9 3l-3 9H2',
  evidence: 'M12 2l9 5-9 5-9-5z|M3 12l9 5 9-5|M3 17l9 5 9-5',
  graph:
    'M18 8a3 3 0 1 0 0-6a3 3 0 0 0 0 6|M6 15a3 3 0 1 0 0-6a3 3 0 0 0 0 6|M18 22a3 3 0 1 0 0-6a3 3 0 0 0 0 6|M8.6 10.7l6.8-4.4|M8.6 13.3l6.8 4.4',
  diagnosis: 'M11 4a7 7 0 1 0 0 14a7 7 0 1 0 0-14|M21 21l-4.3-4.3|M8.5 11l2 2 4-4',
  probes: 'M12 2a10 10 0 1 0 0 20a10 10 0 1 0 0-20|M12 6a6 6 0 1 0 0 12a6 6 0 1 0 0-12|M12 11a1 1 0 1 0 0 2a1 1 0 1 0 0-2',
  repairs:
    'M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94z',
  benchmark: 'M12 20V10|M18 20V4|M6 20v-4|M3 20h18',
  system: 'M4 4h16v6H4z|M4 14h16v6H4z|M8 7h.01|M8 17h.01',
  // actions & status
  refresh: 'M21 12a9 9 0 1 1-3-6.7|M21 3v6h-6',
  search: 'M11 4a7 7 0 1 0 0 14a7 7 0 1 0 0-14|M21 21l-4.3-4.3',
  external: 'M14 3h7v7|M21 3l-9 9|M20 14v5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h5',
  alert:
    'M10.3 3.9L1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z|M12 9v4|M12 17h.01',
  check: 'M20 6L9 17l-5-5',
  close: 'M18 6L6 18|M6 6l12 12',
  filter: 'M22 3H2l8 9.5V19l4 2v-8.5z',
  plus: 'M12 5v14|M5 12h14',
  play: 'M7 4l13 8-13 8z',
  arrowRight: 'M5 12h14|M13 6l6 6-6 6',
  chevron: 'M6 9l6 6 6-6',
  clock: 'M12 2a10 10 0 1 0 0 20a10 10 0 1 0 0-20|M12 6v6l4 2',
  shield: 'M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z',
  sun: 'M12 8a4 4 0 1 0 0 8a4 4 0 1 0 0-8|M12 2v2|M12 20v2|M4.9 4.9l1.4 1.4|M17.7 17.7l1.4 1.4|M2 12h2|M20 12h2|M4.9 19.1l1.4-1.4|M17.7 6.3l1.4-1.4',
  moon: 'M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8',
  menu: 'M3 6h18|M3 12h18|M3 18h18',
  zoomIn: 'M11 4a7 7 0 1 0 0 14a7 7 0 1 0 0-14|M21 21l-4.3-4.3|M11 8v6|M8 11h6',
  zoomOut: 'M11 4a7 7 0 1 0 0 14a7 7 0 1 0 0-14|M21 21l-4.3-4.3|M8 11h6',
  layers: 'M12 2l9 5-9 5-9-5z|M3 12l9 5 9-5',
  bolt: 'M13 2L3 14h7l-1 8 10-12h-7z',
  robot: 'M4 8h16v10H4z|M12 4v4|M8 13h.01|M16 13h.01|M2 12v4|M22 12v4',
  dot: 'M12 10a2 2 0 1 0 0 4a2 2 0 1 0 0-4',
}

const parts = computed(() => (PATHS[props.name] ?? PATHS.dot).split('|'))
const px = computed(() => (typeof props.size === 'number' ? `${props.size}px` : props.size))
</script>

<template>
  <svg
    :width="px"
    :height="px"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    :stroke-width="strokeWidth"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
    focusable="false"
  >
    <path v-for="(d, i) in parts" :key="i" :d="d" />
  </svg>
</template>

<style scoped>
svg {
  flex: 0 0 auto;
  display: block;
}
</style>
