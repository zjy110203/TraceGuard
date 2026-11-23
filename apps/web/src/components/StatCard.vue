<script setup lang="ts">
// A KPI tile for the overview: label + value + optional context line and a
// real sparkline. Renders as <button> when clickable so keyboard users get the
// same affordance as mouse users.
import AppIcon from './AppIcon.vue'
import Sparkline from './Sparkline.vue'

withDefaults(
  defineProps<{
    label: string
    value: number | string
    hint?: string
    tone?: 'default' | 'success' | 'danger' | 'warn' | 'info'
    icon?: string
    trend?: number[]
    clickable?: boolean
  }>(),
  { tone: 'default', clickable: false },
)

const emit = defineEmits<{ open: [] }>()

const toneColor: Record<string, string> = {
  default: 'var(--tg-c1)',
  success: 'var(--tg-success)',
  danger: 'var(--tg-danger)',
  warn: 'var(--tg-warn)',
  info: 'var(--tg-info)',
}
</script>

<template>
  <component
    :is="clickable ? 'button' : 'div'"
    class="stat"
    :class="[tone, { clickable }]"
    :type="clickable ? 'button' : undefined"
    @click="clickable ? emit('open') : undefined"
  >
    <div class="stat-top">
      <span class="stat-label">{{ label }}</span>
      <span v-if="icon" class="stat-icon" :style="{ color: toneColor[tone] }">
        <AppIcon :name="icon" :size="15" />
      </span>
    </div>
    <div class="stat-value">{{ value }}</div>
    <div v-if="hint" class="stat-hint">{{ hint }}</div>
    <div v-if="trend && trend.length > 1" class="stat-spark">
      <Sparkline :values="trend" :color="toneColor[tone]" :width="132" :height="26" />
    </div>
  </component>
</template>

<style scoped>
.stat {
  display: flex;
  flex-direction: column;
  gap: 3px;
  text-align: left;
  font: inherit;
  color: inherit;
  background: var(--tg-panel);
  border: 1px solid var(--tg-border);
  border-radius: var(--tg-radius);
  padding: 13px 15px 12px;
  box-shadow: var(--tg-shadow-sm);
  min-width: 0;
  position: relative;
  overflow: hidden;
}

.stat.clickable {
  cursor: pointer;
  transition:
    border-color 0.14s ease,
    box-shadow 0.14s ease,
    transform 0.14s ease;
}

.stat.clickable:hover {
  border-color: var(--tg-border-strong);
  box-shadow: var(--tg-shadow-md);
  transform: translateY(-1px);
}

.stat::after {
  content: '';
  position: absolute;
  inset: 0 auto 0 0;
  width: 3px;
  background: var(--tg-c1);
  opacity: 0;
}

.stat.success::after {
  background: var(--tg-success);
}
.stat.danger::after {
  background: var(--tg-danger);
}
.stat.warn::after {
  background: var(--tg-warn);
}
.stat.info::after {
  background: var(--tg-info);
}

.stat:hover::after,
.stat.clickable::after {
  opacity: 1;
}

.stat-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.stat-label {
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  color: var(--tg-muted);
}

.stat-icon {
  display: inline-flex;
  opacity: 0.9;
}

.stat-value {
  font-size: 26px;
  font-weight: 650;
  line-height: 1.15;
  letter-spacing: -0.02em;
  font-variant-numeric: tabular-nums;
}

.stat-hint {
  font-size: 11.5px;
  color: var(--tg-muted);
  line-height: 1.4;
}

.stat-spark {
  margin-top: 4px;
  line-height: 0;
}
</style>
