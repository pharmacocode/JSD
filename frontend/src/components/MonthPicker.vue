<script setup>
import { computed } from 'vue'
import { useUiStore } from '@/stores/ui'
import { monthLabel, monthKey, today } from '@/utils/format'

const props = defineProps({
  /**
   * Offer a Month ⇄ Dates toggle (user request: the dashboard can be driven by
   * an explicit from/to range instead of the month). Opt-in so screens that are
   * genuinely month-keyed — Overheads — keep the plain month picker.
   */
  allowRange: { type: Boolean, default: false },
})

const ui = useUiStore()

const label = computed(() => monthLabel(ui.month))
const isRange = computed(() => props.allowRange && ui.periodMode === 'range')

// The range only drives the dashboard once BOTH dates are set and ordered.
// Until then the dashboard stays on the month, so the toggle is never a dead
// end — and the hint below tells the user what is still missing.
const rangeIncomplete = computed(
  () => isRange.value && (!ui.rangeFrom || !ui.rangeTo)
)
const rangeInverted = computed(
  () => isRange.value && !rangeIncomplete.value && ui.rangeFrom > ui.rangeTo
)

function shift(delta) {
  const [y, m] = ui.month.split('-').map(Number)
  const d = new Date(y, m - 1 + delta, 1)
  ui.setMonth(monthKey(d))
}

function pickMonth() {
  ui.setPeriodMode('month')
}

// Default the range to the current month so switching modes immediately shows
// meaningful figures rather than two empty boxes.
function pickRange() {
  ui.setPeriodMode('range')
  if (!ui.rangeFrom || !ui.rangeTo) {
    const now = new Date()
    const first = new Date(now.getFullYear(), now.getMonth(), 1)
    ui.setRange(monthKey(first).concat('-01'), today())
  }
}
</script>

<template>
  <div class="my-2">
    <div v-if="allowRange" class="d-flex justify-center mb-1">
      <v-btn-group density="compact" variant="tonal" color="primary">
        <v-btn size="small" :active="!isRange" @click="pickMonth">Month</v-btn>
        <v-btn size="small" :active="isRange" @click="pickRange">Dates</v-btn>
      </v-btn-group>
    </div>

    <!-- Month mode — unchanged chevron + menu row. -->
    <div v-if="!isRange" class="d-flex align-center justify-center ga-1">
      <v-btn icon="mdi-chevron-left" size="small" variant="text" @click="shift(-1)" />
      <v-menu>
        <template #activator="{ props: p }">
          <v-btn
            v-bind="p"
            variant="tonal"
            color="primary"
            prepend-icon="mdi-calendar"
          >
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

    <!-- Date-range mode — the month is ignored entirely while both dates hold. -->
    <div v-else class="d-flex flex-wrap justify-center ga-2">
      <v-text-field
        v-model="ui.rangeFrom"
        type="date"
        label="From"
        density="compact"
        hide-details
        style="max-width: 170px"
      />
      <v-text-field
        v-model="ui.rangeTo"
        type="date"
        label="To"
        density="compact"
        hide-details
        style="max-width: 170px"
      />
    </div>
    <div
      v-if="rangeIncomplete || rangeInverted"
      class="text-caption text-medium-emphasis text-center mt-1"
    >
      {{ rangeIncomplete ? 'Pick both dates to use a range — the month is used until then.' : 'The "from" date must not be after the "to" date.' }}
    </div>
  </div>
</template>
