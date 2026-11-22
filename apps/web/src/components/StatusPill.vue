<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{ value: string }>()

const tone = computed(() => {
  const v = props.value
  if (['PASSED', 'COMPLETED', 'CONFIRMED', 'PROMOTED', 'APPROVED', 'ALLOWED', 'VALIDATED'].includes(v)) {
    return 'ok'
  }
  if (
    ['FAILED', 'TIMED_OUT', 'REJECTED', 'VALIDATION_FAILED', 'PROMOTION_FAILED', 'CONTRADICT', 'SANDBOX_FAILED', 'DEAD'].includes(v)
  ) {
    return 'bad'
  }
  if (
    [
      'INCONCLUSIVE',
      'NOT_EVALUATED',
      'NOT_RUN',
      'NOT_CONNECTED',
      'NOT_IMPLEMENTED',
      'BLOCKED',
      'CANCELLED',
      'PENDING_APPROVAL',
      'NEEDS_MORE_EVIDENCE',
      'PROBABLE',
      'NO_BEHAVIORAL_CHANGE',
      'UNRESOLVED',
      'WARNING',
    ].includes(v)
  ) {
    return 'warn'
  }
  if (['RUNNING', 'QUEUED', 'PREPARING', 'VALIDATING', 'PROMOTING', 'FINALIZING', 'RETRYING', 'DISPATCHING'].includes(v)) {
    return 'busy'
  }
  return 'idle'
})
</script>

<template>
  <span class="pill" :class="tone">{{ value || '—' }}</span>
</template>

<style scoped>
.pill {
  display: inline-block;
  font-size: 11px;
  line-height: 18px;
  padding: 0 7px;
  border-radius: 9px;
  border: 1px solid transparent;
  white-space: nowrap;
}
.ok {
  background: var(--tg-accent-bg);
  color: var(--tg-success);
  border-color: var(--tg-success);
}
.bad {
  background: var(--tg-accent-bg);
  color: var(--tg-danger);
  border-color: var(--tg-danger);
}
.warn {
  background: var(--tg-accent-bg);
  color: var(--tg-warn);
  border-color: var(--tg-warn);
}
.busy {
  background: var(--tg-accent-bg);
  color: var(--tg-accent);
  border-color: var(--tg-accent);
}
.idle {
  background: var(--tg-panel-2);
  color: var(--tg-muted);
  border-color: var(--tg-border-strong);
}
</style>
