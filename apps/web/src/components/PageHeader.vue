<script setup lang="ts">
defineProps<{
  title: string
  subtitle?: string
}>()
</script>

<template>
  <header class="page-head">
    <div class="head-main">
      <h1>{{ title }}</h1>
      <p v-if="subtitle" class="sub muted small">{{ subtitle }}</p>
    </div>
    <div class="page-actions"><slot /></div>
  </header>
</template>

<style scoped>
.page-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 20px;
  flex-wrap: wrap;
}

/* min-width:0 is what lets the title column shrink instead of forcing the
   actions off-screen; without it a narrow viewport squeezes the h1 into a
   one-character-per-line column. */
.head-main {
  min-width: 0;
  flex: 1 1 260px;
}

.sub {
  margin: 2px 0 0;
  max-width: 78ch;
  line-height: 1.5;
}

.page-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
  flex-wrap: wrap;
}

@media (max-width: 640px) {
  .page-head {
    flex-direction: column;
    align-items: stretch;
    gap: 12px;
  }

  /* In a column flex container flex-basis is the HEIGHT, so `flex: 1 1 260px`
     would reserve 260px of vertical space. Reset it to content height. */
  .head-main {
    flex: 0 0 auto;
  }

  /* Actions become a full-width row of equal-ish buttons under the title. */
  .page-actions {
    width: 100%;
  }

  .page-actions :deep(.btn) {
    flex: 1 1 auto;
  }
}
</style>
