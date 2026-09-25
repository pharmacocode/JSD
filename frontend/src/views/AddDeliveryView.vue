<script setup>
/**
 * Add Delivery (spec 4.2) — single screen.
 *
 * Everything for one delivery lives on one page: search and pick the client,
 * tap one of that client's preferred SKUs, enter qty / price / date / note,
 * watch the live COGS breakdown, then submit. A stock shortfall (HTTP 409)
 * opens the Proceed anyway / Cancel banner inline instead of a separate step,
 * and a successful submit swaps the form for the confirmation card.
 */
import { ref, computed, watch, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { api, listify } from '@/api'
import { money, num, today } from '@/utils/format'

const router = useRouter()

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

// Client search-as-you-type. A token guards against a slow response
// overwriting a newer one.
let searchToken = 0
async function loadClients(q = '') {
  const token = ++searchToken
  try {
    const data = await api.get('/clients/', { search: q || '' })
    if (token !== searchToken) return // stale response
    clients.value = listify(data).slice(0, 8)
  } catch {
    if (token === searchToken) clients.value = []
  }
}

watch(search, (q) => {
  if (client.value) return
  loadClients(q)
})

// Show the picker list immediately instead of waiting for a keystroke.
onMounted(() => loadClients(''))

function pickClient(c) {
  client.value = c
  sku.value = null
  price.value = null
  // Only SKUs the client has a ClientSKUPrice for (preferred).
  skus.value = c.sku_prices || []
}

function changeClient() {
  client.value = null
  sku.value = null
  skus.value = []
  price.value = null
  preview.value = null
  search.value = ''
  loadClients('')
}

function pickSku(entry) {
  sku.value = entry
  price.value = String(entry.selling_price_per_case) // auto-fill, editable
}

// Live COGS preview (materials + print + this month's overhead allocation).
watch([() => client.value?.id, () => sku.value?.sku, qty, date], async () => {
  if (!client.value || !sku.value || !qty.value) {
    preview.value = null
    return
  }
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

// Raw-material subtotal shown in the COGS card.
const materialsCost = computed(() =>
  (preview.value?.cogs?.details?.materials || []).reduce(
    (sum, m) => sum + Number(m.line_cost_per_case),
    0
  )
)

const canSubmit = computed(
  () =>
    !!client.value &&
    !!sku.value &&
    Number(qty.value) > 0 &&
    price.value !== null &&
    price.value !== '' &&
    !loading.value
)

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
  } catch (e) {
    if (e.status === 409) {
      shortfall.value = e.data // {detail, shortfall:[...]} -> inline banner
    } else {
      error.value = e.message
    }
  } finally {
    loading.value = false
  }
}

// Start the next delivery without leaving the page.
function reset() {
  client.value = null
  sku.value = null
  skus.value = []
  qty.value = 1
  price.value = null
  date.value = today()
  note.value = ''
  preview.value = null
  shortfall.value = null
  result.value = null
  error.value = ''
  search.value = ''
  loadClients('')
}
</script>

<template>
  <div>
    <!-- Confirmation card — shown after a successful submit -->
    <template v-if="result">
      <v-alert type="success" variant="tonal" class="mb-3">
        Delivery recorded!
        <v-chip
          v-if="result.stock_shortfall_flag"
          color="warning"
          size="small"
          class="ml-2"
        >
          Stock shortfall — reconcile via Stock Adjustment
        </v-chip>
      </v-alert>
      <v-card class="mb-3">
        <v-list density="compact">
          <v-list-item
            :title="`${client?.name} · ${sku?.sku_description}`"
            :subtitle="`${num(result.delivery?.qty_cases)} cases · ${money(
              result.delivery?.selling_price_per_case
            )} / case`"
          />
          <v-divider />
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
          <v-list-item title="Total COGS (current)">
            <template #append>
              <strong>{{ money(result.total_cogs_current) }}</strong>
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

    <!-- Single-screen delivery form -->
    <template v-else>
      <v-alert v-if="error" type="error" class="mb-3">{{ error }}</v-alert>
      <!-- 1. Client -->
      <v-card class="mb-3">
        <v-card-title class="text-subtitle-1">1. Client</v-card-title>
        <v-card-text>
          <div v-if="client" class="d-flex flex-wrap align-center ga-2">
            <v-chip color="primary" size="large" prepend-icon="mdi-account">
              {{ client.name }}
            </v-chip>
            <span v-if="client.contact_number" class="text-body-2">
              {{ client.contact_number }}
            </span>
            <v-btn size="small" variant="text" @click="changeClient">Change</v-btn>
          </div>
          <template v-else>
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
          </template>
        </v-card-text>
      </v-card>

      <!-- 2. SKU (client's preferred SKUs) -->
      <v-card class="mb-3" :disabled="!client">
        <v-card-title class="text-subtitle-1">2. SKU</v-card-title>
        <v-card-text>
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
            v-if="client && !skus.length"
            type="info"
            variant="tonal"
            class="mt-3"
            density="compact"
          >
            No preferred SKUs configured for this client — set them on the
            client's detail page.
          </v-alert>
          <p v-if="!client" class="text-body-2 text-medium-contrast mb-0">
            Pick a client first.
          </p>
        </v-card-text>
      </v-card>
      <!-- 3. Quantity, price, date -->
      <v-card class="mb-3" :disabled="!sku">
        <v-card-title class="text-subtitle-1">3. Quantity &amp; price</v-card-title>
        <v-card-text>
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
          <p v-if="!sku" class="text-body-2 text-medium-contrast mb-0">
            Pick a SKU to enter quantity and price.
          </p>
        </v-card-text>
      </v-card>

      <!-- Live COGS breakdown -->
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
                <td class="text-right px-0">{{ money(materialsCost) }} / case</td>
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

      <!-- Shortfall banner: Proceed anyway / Cancel (spec 4.2) -->
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
        size="large"
        :disabled="!canSubmit"
        :loading="loading"
        @click="submit(false)"
      >
        Submit delivery
      </v-btn>
    </template>
  </div>
</template>



