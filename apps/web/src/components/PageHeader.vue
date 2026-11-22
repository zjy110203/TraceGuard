<script setup lang="ts">
defineProps<{
  title: string
  subtitle?: string
}>()
</script>

<template>
  <header class="page-head">
    <div>
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
}
.sub {
  margin: 2px 0 0;
}
.page-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}
</style>
