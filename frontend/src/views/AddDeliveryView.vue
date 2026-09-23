<script setup>
/**
 * Add Delivery flow (spec 4.2) — stepped on mobile:
 * 1) client search-as-you-type, 2) SKU buttons (client's preferred SKUs),
 * 3) qty + auto-filled editable selling price, 4) shortfall Proceed/Cancel
 * banner, 5) confirmation with COGS snapshot + new pending balance.
 */
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import { useRouter } from 'vue-router'
import { api, listify } from '@/api'
import { money, num, today } from '@/utils/format'
import { useUiStore } from '@/stores/ui'

const router = useRouter()
const ui = useUiStore()

const step = ref(1)
const loading = ref(false)
const search = ref('')
const clients = ref([])
const client = ref(null)
const skus = ref([])
const sku = ref(null)
const qty = ref(1)
const price = ref(null)
const date = ref(today())
const note = ref('')
const preview = ref(null)
const shortfall = ref(null)
const result = ref(null)
const error = ref('')

// Step 1: search-as-you-type client picker (spec 4.2 step 1)
let searchToken = 0
watch(search, async (q) => {
  if (client.value) return
  const token = ++searchToken
  try {
    const data = await api.get('/clients/', { search: q || '' })
    if (token !== searchToken) return // stale response
    clients.value = listify(data).slice(0, 8)
  } catch {
    if (token === searchToken) clients.value = []
  }
})

async function pickClient(c) {
  client.value = c
  sku.value = null
  // Step 2: only SKUs the client has a ClientSKUPrice for (preferred).
  skus.value = c.sku_prices || []
  step.value = 2
}

function pickSku(entry) {
  sku.value = entry
  price.value = String(entry.selling_price_per_case) // auto-fill, editable
  step.value = 3
}

// Live COGS preview for the confirmation card (spec 4.2 step 5)
watch([() => client.value?.id, () => sku.value?.sku, qty, date], async () => {
  if (!client.value || !sku.value || !qty.value) return
  try {
    preview.value = await api.post('/deliveries/preview/', {
      client: client.value.id,
      sku: sku.value.sku,
      qty_cases: String(qty.value),
      date: date.value,
    })
  } catch {
    preview.value = null
  }
})

async function submit(force = false) {
  error.value = ''
  shortfall.value = null
  loading.value = true
  try {
    result.value = await api.post('/deliveries/', {
      client: client.value.id,
      sku: sku.value.sku,
      qty_cases: String(qty.value),
      selling_price_per_case: String(price.value),
      date: date.value,
      note: note.value,
      force,
    })
    step.value = 4
  } catch (e) {
    if (e.status === 409) {
      shortfall.value = e.data // {detail, shortfall:[...]} -> banner
      step.value = 3
    } else {
      error.value = e.message
    }
  } finally {
    loading.value = false
  }
}

function reset() {
  step.value = 1
  client.value = null
  sku.value = null
  shortfall.value = null
  result.value = null
  error.value = ''
  search.value = ''
  qty.value = 1
  preview.value = null
}

const stepTitle = computed(
  () =>
    ({ 1: 'Select client', 2: 'Select SKU', 3: 'Quantity & price', 4: 'Confirmation' })[
      step.value
    ]
)

// Header back arrow walks back through the steps (user-reported fix):
// step 4 (done) and step 1 leave the page, steps 2-3 go one step back.
onMounted(() => {
  ui.setBackAction(() => {
    if (step.value > 1 && step.value < 4) step.value -= 1
    else router.push('/')
  })
})
onUnmounted(() => ui.clearBackAction())
</script>

<template>
  <div>
    <v-stepper
      v-model="step"
      :items="['Client', 'SKU', 'Qty', 'Confirm']"
      hide-actions
      flat
    >
      <!-- Step 1: client picker -->
      <template #[`item.1`]>
        <v-text-field
          v-model="search"
          prepend-inner-icon="mdi-magnify"
          label="Search client…"
          clearable
        />
        <v-list>
          <v-list-item
            v-for="c in clients"
            :key="c.id"
            :title="c.name"
            :subtitle="c.contact_number"
            prepend-icon="mdi-account"
            @click="pickClient(c)"
          />
          <v-list-item
            v-if="search && !clients.length"
            title="No clients found"
            subtitle="Add them under Clients → New"
          />
        </v-list>
        <v-btn color="primary" block :disabled="!client" @click="step = 2">
          Continue
        </v-btn>
      </template>

      <!-- Step 2: SKU buttons -->
      <template #[`item.2`]>
        <p v-if="client" class="text-body-2 mb-2">
          Delivering to <strong>{{ client.name }}</strong>
        </p>
        <div class="d-flex flex-wrap ga-2">
          <v-btn
            v-for="entry in skus"
            :key="entry.id"
            :color="sku?.id === entry.id ? 'primary' : undefined"
            :variant="sku?.id === entry.id ? 'elevated' : 'outlined'"
            @click="pickSku(entry)"
          >
            {{ entry.sku_description }}
            <span class="text-caption ml-1">
              ₹{{ entry.selling_price_per_case }}/case
            </span>
          </v-btn>
        </div>
        <v-alert
          v-if="!skus.length"
          type="info"
          variant="tonal"
          class="mt-3"
          density="compact"
        >
          No preferred SKUs configured for this client — set them on the
          client's detail page.
        </v-alert>
        <v-btn class="mt-4" color="primary" block :disabled="!sku" @click="step = 3">
          Continue
        </v-btn>
      </template>

      <!-- Step 3: qty + price + preview + shortfall banner -->
      <template #[`item.3`]>
        <v-alert v-if="error" type="error" class="mb-3">{{ error }}</v-alert>

        <v-text-field v-model="date" type="date" label="Delivery date" />
        <v-text-field
          v-model.number="qty"
          type="number"
          min="1"
          label="Quantity (cases)"
        />
        <v-text-field
          v-model="price"
          type="number"
          min="0"
          label="Selling price per case (₹) — auto-filled, editable"
          prefix="₹"
        />
        <v-text-field v-model="note" label="Note (optional)" />

        <v-card v-if="preview" variant="tonal" color="primary" class="mb-3">
          <v-card-text>
            <div class="text-subtitle-2">Estimated COGS — per case</div>
            <div class="text-h6 font-weight-bold">
              {{ money(preview.cogs.per_case) }}
              <span class="text-body-2">/ case</span>
            </div>
            <v-table density="compact" class="bg-transparent">
              <tbody>
                <tr>
                  <td class="px-0">Raw materials (incl. wastage)</td>
                  <td class="text-right px-0">
                    {{ money(preview.cogs.details.materials.reduce((s, m) => s + Number(m.line_cost_per_case), 0)) }}
                    / case
                  </td>
                </tr>
                <tr>
                  <td class="px-0">Print / label (incl. wastage)</td>
                  <td class="text-right px-0">
                    {{ money(preview.cogs.details.print?.cost_per_case || 0) }} / case
                  </td>
                </tr>
                <tr>
                  <td class="px-0">
                    Overhead allocation
                    <div class="text-caption text-medium-contrast">
                      pool {{ money(preview.cogs.details.overhead.total_monthly_overhead) }}
                      ÷ {{ preview.cogs.details.overhead.cases_sold_in_month }} cases
                    </div>
                  </td>
                  <td class="text-right px-0">
                    {{ money(preview.cogs.details.overhead.overhead_per_case) }} / case
                  </td>
                </tr>
                <tr class="font-weight-bold">
                  <td class="px-0">Total COGS</td>
                  <td class="text-right px-0">
                    {{ money(preview.cogs.per_case) }} / case
                  </td>
                </tr>
              </tbody>
            </v-table>
            <div class="text-caption mt-1">
              Delivery value:
              <strong>{{ money(Number(qty) * Number(price || 0)) }}</strong>
              · Total COGS:
              <strong>{{ money(Number(qty) * Number(preview.cogs.per_case)) }}</strong>
            </div>
          </v-card-text>
        </v-card>

        <!-- Shortfall banner: Proceed anyway / Cancel (spec 4.2 step 4) -->
        <v-alert v-if="shortfall" type="warning" variant="elevated" class="mb-3">
          <div class="font-weight-bold mb-1">{{ shortfall.detail }}</div>
          <div
            v-for="s in shortfall.shortfall"
            :key="s.material_id"
            class="text-body-2"
          >
            <strong>{{ s.material }}</strong> — short by
            {{ num(s.short_by) }} {{ s.unit }} (need {{ num(s.required) }}, have
            {{ num(s.available) }})
          </div>
          <div class="d-flex ga-2 mt-3">
            <v-btn color="warning" variant="flat" :loading="loading" @click="submit(true)">
              Proceed anyway
            </v-btn>
            <v-btn variant="outlined" @click="shortfall = null">Cancel</v-btn>
          </div>
        </v-alert>

        <v-btn
          v-else
          color="primary"
          block
          :loading="loading"
          @click="submit(false)"
        >
          Review & Submit
        </v-btn>
      </template>

      <!-- Step 4: confirmation (spec 4.2 step 5) -->
      <template #[`item.4`]>
        <v-alert type="success" variant="tonal" class="mb-3">
          Delivery recorded!
          <v-chip
            v-if="result?.stock_shortfall_flag"
            color="warning"
            size="small"
            class="ml-2"
          >
            Stock shortfall — reconcile via Stock Adjustment
          </v-chip>
        </v-alert>
        <v-card v-if="result" class="mb-3">
          <v-list density="compact">
            <v-list-item title="COGS (frozen snapshot)">
              <template #append>
                <strong>{{ money(result.cogs.per_case) }} / case</strong>
              </template>
            </v-list-item>
            <v-list-item title="Delivery value">
              <template #append>
                <strong>{{ money(result.total_amount) }}</strong>
              </template>
            </v-list-item>
            <v-list-item title="Client pending balance">
              <template #append>
                <strong class="text-error">
                  {{ money(result.client_pending_amount) }}
                </strong>
              </template>
            </v-list-item>
          </v-list>
        </v-card>
        <div class="d-flex flex-wrap ga-2">
          <v-btn color="primary" @click="reset">Add another</v-btn>
          <v-btn variant="outlined" @click="router.push(`/clients/${client?.id}`)">
            Open client ledger
          </v-btn>
          <v-btn variant="text" @click="router.push('/')">Home</v-btn>
        </div>
      </template>
    </v-stepper>

    <div class="text-caption text-center mt-4 text-medium-contrast">
      {{ stepTitle }}
    </div>
  </div>
</template>

