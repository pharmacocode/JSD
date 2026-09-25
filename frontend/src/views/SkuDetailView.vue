<script setup>
/**
 * SKU detail (spec 4.6): material requirements (qty/bottle, decimals),
 * print cost config, and cost breakup toggle per bottle / per case.
 */
import { ref, onMounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import { api, listify } from '@/api'
import { useUiStore } from '@/stores/ui'
import { money, num } from '@/utils/format'

const route = useRoute()
const ui = useUiStore()
const id = route.params.id

const sku = ref(null)
const materials = ref([])
const reqDialog = ref(false)
const reqForm = ref({ material: null, qty_per_case: 1 })
const print = ref({ paper_cost: 0, print_cost_per_paper: 0, labels_per_paper: 1, wastage_percent: 0 })
const hasPrint = ref(false)
const breakup = ref(null)
const loading = ref(true)
const saving = ref(false)
const error = ref('')

async function load() {
  loading.value = true
  try {
    sku.value = await api.get(`/skus/${id}/`)
    materials.value = listify(await api.get('/materials/'))
    hasPrint.value = !!sku.value.print_cost
    if (sku.value.print_cost) print.value = { ...sku.value.print_cost }
    await loadBreakup()
  } finally {
    loading.value = false
  }
}

async function loadBreakup() {
  // Cost breakup is per case only (bottles are never considered).
  breakup.value = await api.get(`/skus/${id}/cost_breakup/`)
}

onMounted(load)

async function addRequirement() {
  error.value = ''
  if (!reqForm.value.material || !reqForm.value.qty_per_case) {
    error.value = 'Material and qty per case are required.'
    return
  }
  saving.value = true
  try {
    await api.post('/sku-requirements/', {
      sku: Number(id),
      material: reqForm.value.material,
      qty_per_case: String(reqForm.value.qty_per_case),
    })
    reqDialog.value = false
    reqForm.value = { material: null, qty_per_case: 1 }
    sku.value = await api.get(`/skus/${id}/`)
    await loadBreakup()
  } catch (e) {
    error.value = e.message
  } finally {
    saving.value = false
  }
}

async function removeRequirement(r) {
  if (!confirm(`Remove ${r.material_name} from this SKU?`)) return
  await api.del(`/sku-requirements/${r.id}/`)
  sku.value = await api.get(`/skus/${id}/`)
  await loadBreakup()
}

async function savePrint() {
  saving.value = true
  try {
    await api.put(`/skus/${id}/print-cost/`, {
      sku: Number(id),
      paper_cost: String(print.value.paper_cost),
      print_cost_per_paper: String(print.value.print_cost_per_paper),
      labels_per_paper: String(print.value.labels_per_paper),
      wastage_percent: String(print.value.wastage_percent),
    })
    hasPrint.value = true
    ui.notify('Print cost config saved')
    await loadBreakup()
  } catch (e) {
    error.value = e.message
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <div v-if="sku">
    <v-card class="mb-3">
      <v-card-title>{{ sku.description }}</v-card-title>
      <v-card-text class="text-body-2">
        {{ num(sku.qty_per_case) }} per case · {{ sku.volume_ml }} mL
      </v-card-text>
    </v-card>

    <!-- Material requirements -->
    <v-card class="mb-3">
      <v-card-title class="d-flex align-center text-subtitle-1">
        Raw material requirements (per case)
        <v-spacer />
        <v-btn size="small" variant="tonal" prepend-icon="mdi-plus" @click="reqDialog = true">
          Add
        </v-btn>
      </v-card-title>
      <v-divider />
      <v-table density="compact">
        <tbody>
          <tr v-for="r in sku.requirements" :key="r.id">
            <td>{{ r.material_name }}</td>
            <td class="text-right">
              {{ r.qty_per_case }} {{ r.material_unit }} per case
            </td>
            <td class="text-right" style="width: 40px">
              <v-btn
                icon="mdi-delete-outline"
                size="x-small"
                variant="text"
                color="error"
                @click="removeRequirement(r)"
              />
            </td>
          </tr>
          <tr v-if="!sku.requirements.length">
            <td colspan="3" class="text-medium-emphasis">
              No materials linked yet — quantities are per case of this SKU
              (decimals allowed, e.g. 1.08 label cases).
            </td>
          </tr>
        </tbody>
      </v-table>
    </v-card>

    <!-- Print cost config (spec 3.6) -->
    <v-card class="mb-3">
      <v-card-title class="text-subtitle-1">
        Print / label cost config
        <div class="text-caption text-medium-emphasis font-weight-regular">
          Labels needed per case = quantity per case ({{ sku.qty_per_case }}).
          Per-case cost = (paper + print) ÷ labels per paper × qty per case,
          wastage-adjusted.
        </div>
      </v-card-title>
      <v-divider />
      <v-card-text>
        <v-alert v-if="error" type="error" density="compact" class="mb-2">
          {{ error }}
        </v-alert>
        <v-row dense>
          <v-col cols="6" md="3">
            <v-text-field
              v-model.number="print.paper_cost"
              type="number"
              prefix="₹"
              label="Paper cost"
              density="compact"
            />
          </v-col>
          <v-col cols="6" md="3">
            <v-text-field
              v-model.number="print.print_cost_per_paper"
              type="number"
              prefix="₹"
              label="Print ₹/paper"
              density="compact"
            />
          </v-col>
          <v-col cols="6" md="3">
            <v-text-field
              v-model.number="print.labels_per_paper"
              type="number"
              label="Labels / paper"
              density="compact"
            />
          </v-col>
          <v-col cols="6" md="3">
            <v-text-field
              v-model.number="print.wastage_percent"
              type="number"
              label="Wastage %"
              density="compact"
            />
          </v-col>
        </v-row>
        <v-btn color="primary" :loading="saving" @click="savePrint">
          Save print cost
        </v-btn>
      </v-card-text>
    </v-card>

    <!-- Cost breakup view (spec 4.6) -->
    <v-card class="mb-3">
      <v-card-title class="d-flex align-center text-subtitle-1">
        Cost breakup — per case
      </v-card-title>
      <v-divider />
      <v-card-text v-if="breakup">
        <div class="text-h5 font-weight-bold mb-2">
          {{ money(breakup.per_case) }}
          <span class="text-body-2 text-medium-emphasis">/ case</span>
        </div>
        <v-table density="compact">
          <tbody>
            <tr>
              <td>Raw materials (incl. wastage)</td>
              <td class="text-right">
                {{ money(breakup.details.materials.reduce((s, m) => s + Number(m.line_cost_per_case), 0)) }}
                / case
              </td>
            </tr>
            <tr>
              <td>Print / label (incl. wastage)</td>
              <td class="text-right">
                {{ money(breakup.details.print?.cost_per_case || 0) }} / case
              </td>
            </tr>
            <tr>
              <td>
                Overhead allocation
                <div class="text-caption text-medium-emphasis">
                  {{ breakup.details.overhead.note }}
                </div>
              </td>
              <td class="text-right">
                {{ money(breakup.details.overhead.overhead_per_case) }} / case
                <div class="text-caption">
                  pool {{ money(breakup.details.overhead.total_monthly_overhead) }}
                  ÷ {{ breakup.details.overhead.cases_sold_in_month }} cases
                </div>
              </td>
            </tr>
            <tr class="font-weight-bold">
              <td>Total COGS</td>
              <td class="text-right">{{ money(breakup.per_case) }} / case</td>
            </tr>
          </tbody>
        </v-table>

        <div class="text-subtitle-2 mt-4">Material-wise detail (per case)</div>
        <v-table density="compact">
          <thead>
            <tr>
              <th>Material</th>
              <th class="text-right">Qty / case</th>
              <th class="text-right">FIFO ₹ / case</th>
              <th class="text-right">Line ₹ / case</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="m in breakup.details.materials" :key="m.material_id">
              <td>{{ m.material }}</td>
              <td class="text-right">{{ m.qty_per_case }} {{ m.unit }}</td>
              <td class="text-right">{{ money(m.fifo_unit_cost) }}</td>
              <td class="text-right">{{ money(m.line_cost_per_case) }}</td>
            </tr>
          </tbody>
        </v-table>
        <p class="text-caption text-medium-emphasis mt-2">{{ breakup.note }}</p>
      </v-card-text>
    </v-card>

    <!-- Add requirement dialog -->
    <v-dialog v-model="reqDialog" max-width="420">
      <v-card>
        <v-card-title>Add material requirement</v-card-title>
        <v-card-text>
          <v-select
            v-model="reqForm.material"
            :items="materials"
            item-title="name"
            item-value="id"
            label="Material"
          />
          <v-text-field
            v-model.number="reqForm.qty_per_case"
            type="number"
            step="0.0001"
            label="Qty per case of SKU (decimals allowed)"
            hint="e.g. 1 case of bottles, 1.08 cases of labels per case"
            persistent-hint
          />
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="reqDialog = false">Cancel</v-btn>
          <v-btn color="primary" :loading="saving" @click="addRequirement">Add</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>
  </div>

  <v-progress-circular v-else-if="loading" indeterminate class="d-block mx-auto mt-8" />
</template>

