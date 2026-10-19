<script setup>
/**
 * Add / edit an asset (user request). The FIRST thing an asset carries is
 * its label — DEPRECIATING (auto, printing machine…) or NON_DEPRECIATING
 * (savings, deposits…). Only a depreciating asset collects the life details
 * (initial value, purchase month & year, expected lifetime, end-of-life
 * value) and the dialog previews the estimated value live before confirm.
 */
import { computed, ref, watch } from 'vue'
import { api } from '@/api'
import { useUiStore } from '@/stores/ui'
import { money, monthKey } from '@/utils/format'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  asset: { type: Object, default: null },
})
const emit = defineEmits(['update:modelValue', 'saved'])

const ui = useUiStore()
const saving = ref(false)
const error = ref('')
const form = ref(blank())

function blank() {
  return {
    id: null,
    name: '',
    kind: 'DEPRECIATING',
    initial_value: null,
    purchase_month: monthKey(),
    lifetime_years: null,
    end_of_life_value: null,
  }
}

watch(
  () => props.modelValue,
  (open) => {
    if (!open) return
    error.value = ''
    form.value = props.asset
      ? {
          id: props.asset.id,
          name: props.asset.name,
          kind: props.asset.kind,
          initial_value: Number(props.asset.initial_value),
          purchase_month: props.asset.purchase_month || monthKey(),
          lifetime_years:
            props.asset.lifetime_years == null
              ? null
              : Number(props.asset.lifetime_years),
          end_of_life_value:
            props.asset.end_of_life_value == null
              ? null
              : Number(props.asset.end_of_life_value),
        }
      : blank()
  },
)

// Straight-line estimate — the same maths as Asset.current_value on the
// server, so the preview and the saved figure always agree.
const estimated = computed(() => {
  const f = form.value
  const initial = Number(f.initial_value || 0)
  if (f.kind !== 'DEPRECIATING') return initial
  const lifetimeMonths = Math.round(Number(f.lifetime_years || 0) * 12)
  if (!f.purchase_month || lifetimeMonths <= 0) return initial
  const [py, pm] = f.purchase_month.split('-').map(Number)
  const now = new Date()
  const elapsed = (now.getFullYear() - py) * 12 + (now.getMonth() + 1 - pm)
  if (elapsed <= 0) return initial
  const salvage = Number(f.end_of_life_value || 0)
  const fraction = Math.min(elapsed / lifetimeMonths, 1)
  return Math.max(initial - (initial - salvage) * fraction, salvage)
})

const canSave = computed(() => {
  const f = form.value
  if (!f.name || !Number(f.initial_value)) return false
  if (f.kind === 'DEPRECIATING')
    return !!f.purchase_month && Number(f.lifetime_years) > 0
  return true
})

async function save() {
  error.value = ''
  saving.value = true
  try {
    const f = form.value
    const payload = {
      name: f.name,
      kind: f.kind,
      initial_value: String(f.initial_value),
      // The life details belong to a depreciating asset only.
      purchase_month: f.kind === 'DEPRECIATING' ? f.purchase_month : '',
      lifetime_years:
        f.kind === 'DEPRECIATING' && f.lifetime_years != null
          ? String(f.lifetime_years)
          : null,
      end_of_life_value:
        f.kind === 'DEPRECIATING' && f.end_of_life_value != null
          ? String(f.end_of_life_value)
          : null,
    }
    if (f.id) await api.put(`/assets/${f.id}/`, payload)
    else await api.post('/assets/', payload)
    ui.notify(`Asset "${f.name}" saved`)
    emit('saved')
    emit('update:modelValue', false)
  } catch (e) {
    error.value = e.message
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <v-dialog
    :model-value="modelValue"
    max-width="480"
    @update:model-value="(v) => emit('update:modelValue', v)"
  >
    <v-card>
      <v-card-title>{{ form.id ? 'Edit asset' : 'Add asset' }}</v-card-title>
      <v-card-text>
        <v-text-field
          v-model="form.name"
          label="Asset name (e.g. Auto, Printing machine)"
        />

        <!-- The label comes FIRST (user request): everything below depends on it. -->
        <div
          class="text-caption font-weight-bold text-uppercase text-medium-emphasis mb-1"
        >
          Type
        </div>
        <v-btn-toggle
          v-model="form.kind"
          mandatory
          density="comfortable"
          divided
          class="mb-1"
        >
          <v-btn value="DEPRECIATING" prepend-icon="mdi-car-cash">
            Depreciating
          </v-btn>
          <v-btn value="NON_DEPRECIATING" prepend-icon="mdi-piggy-bank-outline">
            Non-depreciating
          </v-btn>
        </v-btn-toggle>
        <div class="text-caption text-medium-emphasis mb-4">
          <template v-if="form.kind === 'DEPRECIATING'">
            Vehicles, machines… — the value is estimated down to its
            end-of-life value.
          </template>
          <template v-else>
            Savings, deposits… — the value never changes.
          </template>
        </div>

        <v-text-field
          v-model.number="form.initial_value"
          type="number"
          prefix="₹"
          label="Initial value"
        />

        <template v-if="form.kind === 'DEPRECIATING'">
          <v-text-field
            v-model="form.purchase_month"
            type="month"
            label="Purchase month & year"
          />
          <v-text-field
            v-model.number="form.lifetime_years"
            type="number"
            suffix="years"
            label="Expected asset life time"
          />
          <v-text-field
            v-model.number="form.end_of_life_value"
            type="number"
            prefix="₹"
            label="End of life value (scrap / resale)"
          />
        </template>

        <v-sheet color="success" variant="tonal" rounded class="pa-3 mt-2 text-center">
          <div class="text-caption text-medium-emphasis">
            {{ form.kind === 'DEPRECIATING' ? 'Estimated value today' : 'Value' }}
          </div>
          <div class="text-h6 font-weight-bold">{{ money(estimated) }}</div>
        </v-sheet>

        <v-alert v-if="error" type="error" density="compact" class="mt-3">
          {{ error }}
        </v-alert>
      </v-card-text>
      <v-card-actions>
        <v-spacer />
        <v-btn variant="text" @click="emit('update:modelValue', false)">Cancel</v-btn>
        <v-btn color="primary" :loading="saving" :disabled="!canSave" @click="save">
          Save
        </v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>
</template>
