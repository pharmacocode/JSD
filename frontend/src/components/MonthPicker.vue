<script setup>
import { computed } from 'vue'
import { useUiStore } from '@/stores/ui'
import { monthLabel, monthKey } from '@/utils/format'

const ui = useUiStore()

const label = computed(() => monthLabel(ui.month))

function shift(delta) {
  const [y, m] = ui.month.split('-').map(Number)
  const d = new Date(y, m - 1 + delta, 1)
  ui.setMonth(monthKey(d))
}
</script>

<template>
  <div class="d-flex align-center justify-center ga-1 my-2">
    <v-btn icon="mdi-chevron-left" size="small" variant="text" @click="shift(-1)" />
    <v-menu>
      <template #activator="{ props }">
        <v-btn v-bind="props" variant="tonal" color="primary" prepend-icon="mdi-calendar">
          {{ label }}
        </v-btn>
      </template>
      <v-list density="compact">
        <v-list-item
          v-for="off in [-2, -1, 0, 1, 2]"
          :key="off"
          :title="monthLabel(monthKey(new Date(new Date().getFullYear(), new Date().getMonth() + off, 1)))"
          :active="monthKey(new Date(new Date().getFullYear(), new Date().getMonth() + off, 1)) === ui.month"
          @click="ui.setMonth(monthKey(new Date(new Date().getFullYear(), new Date().getMonth() + off, 1)))"
        />
      </v-list>
    </v-menu>
    <v-btn icon="mdi-chevron-right" size="small" variant="text" @click="shift(1)" />
  </div>
</template>
