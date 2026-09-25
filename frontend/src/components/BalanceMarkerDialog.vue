<script setup>
/**
 * Shared "balance as of a date" dialog (user request) — used by the client
 * pending amount and the vendor payable.
 *
 * The figure typed in is what was owed ON the marked date. Transactions dated
 * strictly after that day are added on top automatically; everything on or
 * before it is already inside the figure. The split is computed server-side
 * (GET <endpoint>?as_of=&amount=) so the rule lives in one place — the same
 * values the save uses — and the API returns exactly what was applied.
 */
import { ref, watch, computed } from 'vue'
import { api } from '@/api'
import { money, today } from '@/utils/format'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  title: { type: String, default: 'Balance as of date' },
  amountLabel: { type: String, default: 'Balance on that date' },
  totalLabel: { type: String, default: 'Balance now' },
  endpoint: { type: String, required: true },
  amount: { type: [Number, String], default: 0 },
  date: { type: String, default: '' },
  active: { type: Boolean, default: false },
})
const emit = defineEmits(['update:modelValue', 'saved'])

const form = ref({ amount: 0, date: today() })
const preview = ref(null)
const loading = ref(false)
const saving = ref(false)
const error = ref('')

watch(
  () => props.modelValue,
  (open) => {
    if (!open) return
    error.value = ''
    preview.value = null
    form.value = { amount: Number(props.amount || 0), date: props.date || today() }
    load()
  }
)

// Debounced live preview while typing (amount + date).
let timer = null
watch(
  () => [form.value.amount, form.value.date],
  () => {
    if (!props.modelValue) return
    clearTimeout(timer)
    timer = setTimeout(load, 300)
  }
)

async function load() {
  if (!form.value.date) return
  loading.value = true
  try {
    preview.value = await api.get(props.endpoint, {
      as_of: form.value.date,
      amount: form.value.amount,
    })
    error.value = ''
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}

async function save() {
  error.value = ''
  if (!form.value.date) {
    error.value = 'Pick the date this amount refers to.'
    return
  }
  saving.value = true
  try {
    await api.post(props.endpoint, {
      amount: String(form.value.amount ?? 0),
      date: form.value.date,
    })
    emit('saved')
    emit('update:modelValue', false)
  } catch (e) {
    error.value = e.message
  } finally {
    saving.value = false
  }
}

async function clearMarker() {
  if (
    !confirm(
      'Remove the "as of" marker? The balance returns to the plain sum of all transactions.'
    )
  )
    return
  saving.value = true
  try {
    await api.del(props.endpoint)
    emit('saved')
    emit('update:modelValue', false)
  } catch (e) {
    error.value = e.message
  } finally {
    saving.value = false
  }
}

const after = computed(
  () => preview.value?.after ?? { count: 0, total: '0', entries: [] }
)
const before = computed(
  () => preview.value?.before ?? { count: 0, total: '0', entries: [] }
)
</script>

<template>
  <v-dialog
    :model-value="modelValue"
    max-width="560"
    @update:model-value="(v) => emit('update:modelValue', v)"
  >
    <v-card>
      <v-card-title class="text-subtitle-1">{{ title }}</v-card-title>
      <v-card-text>
        <v-alert v-if="error" type="error" density="compact" class="mb-3">
          {{ error }}
        </v-alert>

        <v-text-field
          v-model.number="form.amount"
          type="number"
          prefix="₹"
          :label="amountLabel"
          hint="Enter what was owed on the date below — older transactions are already inside it."
          persistent-hint
          class="mb-6"
        />
        <v-text-field
          v-model="form.date"
          type="date"
          label="As of date"
          hint="Transactions after this day are added automatically."
          persistent-hint
        />

        <v-sheet
          v-if="preview"
          color="grey-lighten-4"
          rounded="lg"
          class="pa-3 mt-5"
          :elevation="0"
        >
          <div class="d-flex justify-space-between text-body-2">
            <span>{{ amountLabel }}</span>
            <strong>{{ money(preview.amount) }}</strong>
          </div>
          <div class="d-flex justify-space-between text-body-2 mt-1">
            <span>{{ after.count }} transaction(s) after {{ form.date }}</span>
            <span :class="Number(after.total) >= 0 ? 'text-error' : 'text-success'">
              {{ Number(after.total) >= 0 ? '+' : '' }}{{ money(after.total) }}
            </span>
          </div>
          <v-divider class="my-2" />
          <div class="d-flex justify-space-between align-center">
            <span class="text-subtitle-2">{{ totalLabel }}</span>
            <strong class="text-h6">{{ money(preview.total) }}</strong>
          </div>
        </v-sheet>

        <v-progress-linear v-else-if="loading" indeterminate class="mt-4" />

        <v-alert
          v-if="before.count"
          type="info"
          variant="tonal"
          density="compact"
          class="mt-3"
        >
          {{ before.count }} older transaction(s) are already inside the entered
          figure, so they are not added again.
        </v-alert>

        <v-expansion-panels v-if="after.entries.length" variant="accordion" class="mt-3">
          <v-expansion-panel :title="`Transactions added on top (${after.count})`">
            <v-expansion-panel-text>
              <v-list density="compact">
                <v-list-item
                  v-for="e in after.entries"
                  :key="e.id"
                  :title="e.label"
                  :subtitle="`${e.date}${e.detail ? ' · ' + e.detail : ''}`"
                >
                  <template #append>
                    <span
                      :class="Number(e.amount) >= 0 ? 'text-error' : 'text-success'"
                      class="font-weight-bold"
                    >
                      {{ Number(e.amount) >= 0 ? '+' : '' }}{{ money(e.amount) }}
                    </span>
                  </template>
                </v-list-item>
              </v-list>
            </v-expansion-panel-text>
          </v-expansion-panel>
        </v-expansion-panels>
      </v-card-text>

      <v-card-actions>
        <v-btn
          v-if="active"
          variant="text"
          color="error"
          :disabled="saving"
          @click="clearMarker"
        >
          Remove marker
        </v-btn>
        <v-spacer />
        <v-btn variant="text" @click="emit('update:modelValue', false)">Cancel</v-btn>
        <v-btn color="primary" :loading="saving" @click="save">Save</v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>
</template>
