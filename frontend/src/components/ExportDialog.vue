<script setup>
/**
 * Export dialog (user request): start date + end date, a live preview of the
 * matching rows (both dates inclusive), then Export PDF on confirm.
 *
 * Data-agnostic on purpose: the caller owns what "rows in the range" means
 * (client deliveries, the Home register...) and what the PDF looks like, so
 * both export buttons share the shell and only differ in props:
 *   title        — dialog heading
 *   rangeLabel   — subtitle under the heading
 *   dateLabel    — the date key inside each row picked up by the filter
 *   rows         — ALL candidate rows (the dialog filters to [from, to])
 *   loader       — OPTIONAL async row source ({ from, to }) => Promise<rows>
 *                  for arbitrary ranges (Home): rows are fetched per range
 *                  instead of filtered from `rows`, and the preview shows
 *                  exactly what the server returned for the chosen range.
 *   defaultFrom/defaultTo — range the dialog opens with (today when unset)
 *   emptyHint    — what the preview says when nothing matches
 *   columns      — [{ key, label, align?, format? }] for the preview table
 *   confirmLabel — the Export button label
 * Emits `confirm({ from, to, rows })` with the rows in range.
 */
import { ref, computed, watch } from 'vue'
import { today, fmtDate } from '@/utils/format'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  title: { type: String, default: 'Export' },
  rangeLabel: { type: String, default: '' },
  dateLabel: { type: String, default: 'Delivery date range' },
  dateKey: { type: String, default: 'date' },
  rows: { type: Array, default: () => [] },
  /** Async row source for arbitrary ranges: ({ from, to }) => Promise<rows>. */
  loader: { type: Function, default: null },
  /** Range the dialog opens with (falls back to today when empty). */
  defaultFrom: { type: String, default: '' },
  defaultTo: { type: String, default: '' },
  emptyHint: { type: String, default: 'No rows in this date range.' },
  columns: { type: Array, default: () => [] },
  confirmLabel: { type: String, default: 'Export PDF' },
})

const emit = defineEmits(['update:modelValue', 'confirm'])

const open = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v),
})

const from = ref(today())
const to = ref(today())
const busy = ref(false)
const error = ref('')
// Loader mode (Home): rows for an arbitrary range come from the server, so
// they are fetched for [from, to] rather than filtered from a static list.
const loadedRows = ref([])
const loadingRows = ref(false)

/** Clamp to ISO date strings so comparisons are plain string compares. */
function key(row) {
  return fmtDate(row?.[props.dateKey])
}

const inRange = computed(() => {
  // Loader rows arrive already scoped to [from, to] by the server.
  if (props.loader) return loadedRows.value
  if (!from.value || !to.value || from.value > to.value) return []
  return (props.rows || []).filter((r) => {
    const d = key(r)
    return d && d >= from.value && d <= to.value
  })
})

// ---------------------------------------------------------------------------
// Loader mode: fetch the rows for the chosen range (debounced, race-guarded).
// ---------------------------------------------------------------------------
let fetchSeq = 0
let fetchTimer = null

async function fetchLoadedRows() {
  if (!props.loader || !from.value || !to.value || from.value > to.value) {
    loadedRows.value = []
    return
  }
  const seq = ++fetchSeq
  loadingRows.value = true
  try {
    const rows = await props.loader({ from: from.value, to: to.value })
    // A later range change must not be overwritten by a slower earlier reply.
    if (seq === fetchSeq) loadedRows.value = rows || []
  } catch (e) {
    if (seq === fetchSeq) {
      loadedRows.value = []
      error.value = e?.message || 'Could not load rows for this range.'
    }
  } finally {
    if (seq === fetchSeq) loadingRows.value = false
  }
}

function scheduleFetch() {
  if (!props.loader) return
  clearTimeout(fetchTimer)
  fetchTimer = setTimeout(fetchLoadedRows, 250)
}

/** When the dialog is (re)opened, default the range and (re)load the rows. */
watch(open, (v) => {
  if (!v) return
  error.value = ''
  const t = today()
  from.value = props.defaultFrom || t
  to.value = props.defaultTo || t
  if (props.loader) fetchLoadedRows()
})
watch([from, to], () => {
  if (open.value) scheduleFetch()
})

function cell(row, col) {
  const v = row?.[col.key]
  return typeof col.format === 'function' ? col.format(v, row) : v ?? ''
}

async function confirm() {
  if (!inRange.value.length || busy.value || loadingRows.value) return
  busy.value = true
  error.value = ''
  try {
    await emit('confirm', { from: from.value, to: to.value, rows: inRange.value })
    open.value = false
  } catch (e) {
    error.value = e?.message || 'Export failed'
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <v-dialog v-model="open" max-width="640" scrollable>
    <v-card>
      <v-card-title class="d-flex align-center text-subtitle-1 font-weight-bold">
        <v-icon start color="primary">mdi-file-export-outline</v-icon>
        {{ title }}
      </v-card-title>
      <div v-if="rangeLabel" class="text-body-2 text-medium-emphasis px-4 pb-2">
        {{ rangeLabel }}
      </div>
      <v-divider />
      <v-card-text>
        <v-alert v-if="error" type="error" density="compact" class="mb-3">
          {{ error }}
        </v-alert>
        <div class="text-uppercase text-caption text-medium-emphasis mb-1">
          {{ dateLabel }} (both dates inclusive)
        </div>
        <v-row dense class="mb-2">
          <v-col cols="6">
            <v-text-field
              v-model="from"
              type="date"
              label="Start date"
              density="compact"
              variant="outlined"
            />
          </v-col>
          <v-col cols="6">
            <v-text-field
              v-model="to"
              type="date"
              label="End date"
              density="compact"
              variant="outlined"
            />
          </v-col>
        </v-row>
        <v-alert
          v-if="from && to && from > to"
          type="warning"
          density="compact"
          class="mb-3"
        >
          Start date is after the end date — pick a valid range.
        </v-alert>
        <v-progress-linear v-if="loadingRows" indeterminate density="compact" class="mb-2" />
        <div class="text-body-2 font-weight-medium mb-1">
          {{ inRange.length }} row{{ inRange.length === 1 ? '' : 's' }} in range
        </div>
        <v-table v-if="inRange.length" density="compact" class="mb-1">
          <thead>
            <tr>
              <th v-for="c in columns" :key="c.key" :class="c.align === 'right' ? 'text-right' : 'text-left'">
                {{ c.label }}
              </th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(r, i) in inRange" :key="r.id ?? i">
              <td
                v-for="c in columns"
                :key="c.key"
                :class="c.align === 'right' ? 'text-right' : ''"
              >
                {{ cell(r, c) }}
              </td>
            </tr>
          </tbody>
        </v-table>
        <div v-else class="text-body-2 text-medium-emphasis">
          {{ emptyHint }}
        </div>
      </v-card-text>
      <v-divider />
      <v-card-actions>
        <v-spacer />
        <v-btn variant="text" :disabled="busy" @click="open = false">Cancel</v-btn>
        <v-btn
          color="primary"
          variant="flat"
          :loading="busy || loadingRows"
          :disabled="!inRange.length || loadingRows"
          @click="confirm"
        >
          {{ confirmLabel }}
        </v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>
</template>
