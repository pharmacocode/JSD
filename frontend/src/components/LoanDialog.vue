<script setup>
/**
 * Add / edit a loan (user request): amount, interest rate, start date and
 * duration produce the EXPECTED EMI, shown before confirm — and the EMI can
 * be edited (a bank's figure often differs by a few rupees) before saving.
 */
import { computed, ref, watch } from 'vue'
import { api } from '@/api'
import { useUiStore } from '@/stores/ui'
import { money, today } from '@/utils/format'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  loan: { type: Object, default: null },
})
const emit = defineEmits(['update:modelValue', 'saved'])

const ui = useUiStore()
const saving = ref(false)
const error = ref('')
const form = ref(blank())
const emiTouched = ref(false)

function blank() {
  return {
    id: null,
    name: '',
    principal: null,
    annual_interest_rate: null,
    start_date: today(),
    duration_months: null,
    emi: null,
    initial_paid: 0,
  }
}

// Reducing-balance EMI — the same annuity formula the backend uses.
function computeEmi(principal, annualRate, months) {
  principal = Number(principal || 0)
  months = Number(months || 0)
  if (principal <= 0 || months <= 0) return 0
  const r = Number(annualRate || 0) / 12 / 100
  if (r === 0) return principal / months
  const f = Math.pow(1 + r, months)
  return (principal * r * f) / (f - 1)
}

// EMIs fallen due from the first EMI date (= start date) — mirrors
// Loan.emis_due on the server: #1 on the start date, then monthly.
function emisDue(startStr, duration) {
  if (!startStr) return 0
  const start = new Date(`${String(startStr).slice(0, 10)}T00:00:00`)
  const now = new Date()
  if (Number.isNaN(start.getTime()) || start > now) return 0
  const elapsed =
    (now.getFullYear() - start.getFullYear()) * 12 +
    (now.getMonth() - start.getMonth())
  const due = elapsed + (now.getDate() >= start.getDate() ? 1 : 0)
  return Math.max(0, Math.min(due, Number(duration || 0)))
}

const expectedEmi = computed(() => {
  const v = computeEmi(
    form.value.principal,
    form.value.annual_interest_rate,
    form.value.duration_months,
  )
  return Math.round(v * 100) / 100
})

// The EMI field follows the formula until the user edits it themselves.
watch(expectedEmi, (v) => {
  if (!emiTouched.value) form.value.emi = v || null
})

function useExpectedEmi() {
  emiTouched.value = false
  form.value.emi = expectedEmi.value || null
}

watch(
  () => props.modelValue,
  (open) => {
    if (!open) return
    error.value = ''
    emiTouched.value = false
    if (props.loan) {
      const l = props.loan
      form.value = {
        id: l.id,
        name: l.name || '',
        principal: Number(l.principal),
        annual_interest_rate: Number(l.annual_interest_rate),
        start_date: l.start_date,
        duration_months: Number(l.duration_months),
        emi: Number(l.emi),
        initial_paid: Number(l.initial_paid || 0),
      }
      // An existing loan keeps its saved EMI (even an edited one) until the
      // terms change or the user asks for the expected figure again.
      emiTouched.value = Number(l.emi) !== expectedEmi.value
    } else {
      form.value = blank()
      form.value.emi = expectedEmi.value || null
    }
  },
)

const totalPayable = computed(
  () => Number(form.value.emi || 0) * Number(form.value.duration_months || 0),
)

// Live preview of the NEW outstanding: EMIs fallen due from the first EMI
// date, minus the already-paid figure the user is entering.
const dueCount = computed(() =>
  emisDue(form.value.start_date, form.value.duration_months),
)

const previewOutstanding = computed(() => {
  const due = Math.round(Number(form.value.emi || 0) * dueCount.value * 100) / 100
  return Math.max(due - Number(form.value.initial_paid || 0), 0)
})

const canSave = computed(() => {
  const f = form.value
  return (
    Number(f.principal) > 0 &&
    Number(f.duration_months) >= 1 &&
    !!f.start_date &&
    Number(f.emi) > 0 &&
    Number(f.initial_paid || 0) >= 0
  )
})

async function save() {
  error.value = ''
  saving.value = true
  try {
    const f = form.value
    const payload = {
      name: f.name,
      principal: String(f.principal),
      annual_interest_rate: String(f.annual_interest_rate || 0),
      start_date: f.start_date,
      duration_months: Number(f.duration_months),
      emi: String(f.emi),
      initial_paid: String(f.initial_paid || 0),
    }
    if (f.id) await api.put(`/loans/${f.id}/`, payload)
    else await api.post('/loans/', payload)
    ui.notify('Loan saved')
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
      <v-card-title>{{ form.id ? 'Edit loan' : 'Add loan' }}</v-card-title>
      <v-card-text>
        <v-text-field
          v-model="form.name"
          label="Loan name (optional, e.g. Auto loan)"
        />
        <v-text-field
          v-model.number="form.principal"
          type="number"
          prefix="₹"
          label="Loan amount"
        />
        <v-text-field
          v-model.number="form.annual_interest_rate"
          type="number"
          suffix="%"
          label="Interest rate (per year)"
        />
        <v-text-field
          v-model="form.start_date"
          type="date"
          label="First EMI date (start date)"
        />
        <v-text-field
          v-model.number="form.duration_months"
          type="number"
          suffix="months"
          label="Duration of loan"
        />
        <v-text-field
          v-model.number="form.initial_paid"
          type="number"
          prefix="₹"
          label="Total payment already made"
        />
        <div class="text-caption text-medium-emphasis">
          0 for a fresh loan; enter what has already been paid on one that
          has been running — it counts against the EMIs due.
        </div>

        <!-- Expected EMI before confirm (user request), editable. -->
        <v-sheet color="primary" variant="tonal" rounded class="pa-3 mt-2">
          <div class="d-flex align-center">
            <div>
              <div class="text-caption text-medium-emphasis">Expected EMI</div>
              <div class="text-h6 font-weight-bold">{{ money(expectedEmi) }}</div>
            </div>
            <v-spacer />
            <div class="text-right">
              <div class="text-caption text-medium-emphasis">Total payable</div>
              <div class="text-subtitle-2 font-weight-bold">
                {{ money(totalPayable) }}
              </div>
            </div>
          </div>
          <div class="text-caption mt-1" style="opacity: 0.85">
            {{ dueCount }} EMI{{ dueCount === 1 ? '' : 's' }} due so far ·
            outstanding <strong>{{ money(previewOutstanding) }}</strong>
          </div>
        </v-sheet>
        <v-text-field
          v-model.number="form.emi"
          type="number"
          prefix="₹"
          label="EMI you will pay"
          class="mt-2"
          @update:model-value="emiTouched = true"
        >
          <template #append-inner>
            <v-btn
              v-if="emiTouched"
              size="x-small"
              variant="text"
              color="primary"
              title="Use the expected EMI"
              @click="useExpectedEmi"
            >
              use expected
            </v-btn>
          </template>
        </v-text-field>
        <div class="text-caption text-medium-emphasis">
          Edit the EMI if your bank's figure differs — the total follows it.
        </div>

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

