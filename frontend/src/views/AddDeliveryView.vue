<script setup>
/**
 * Add Delivery (spec 4.2) — single screen, multi-SKU.
 *
 * Everything for one delivery run lives on one page: search and pick the
 * client, tap any number of that client's preferred SKUs to add a line, set
 * each line's qty / price, enter date / note, watch the live aggregate COGS
 * + shortfall preview, then submit the whole run in one atomic request. A
 * stock shortfall (HTTP 409) opens the Proceed anyway / Cancel banner inline
 * instead of a separate step, and a successful submit swaps the form for the
 * confirmation card.
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
// One row per selected SKU: { entry, qty, price } (spec 4.2 multi-SKU).
const lines = ref([])
const date = ref(today())
const note = ref('')
// Optional payment handed over at the delivery (user request): off by default
// and never required — left alone, the delivery is saved on credit exactly as
// before. When filled in it is captured as a payment transaction for the client.
const payNow = ref(false)
const payAmount = ref('')
const payDate = ref(today())
const payNote = ref('')
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
  lines.value = []
  // Only SKUs the client has a ClientSKUPrice for (preferred).
  skus.value = c.sku_prices || []
}

function changeClient() {
  client.value = null
  lines.value = []
  skus.value = []
  preview.value = null
  search.value = ''
  loadClients('')
}

// SKU chips act as toggles: tap to add a line, tap again to remove it.
// The price is pre-filled from the client's SKU price but stays editable.
function toggleSku(entry) {
  const i = lines.value.findIndex((l) => l.entry.sku === entry.sku)
  if (i >= 0) {
    lines.value.splice(i, 1)
  } else {
    lines.value.push({
      entry,
      qty: 1,
      price: String(entry.selling_price_per_case),
    })
  }
}

function removeLine(i) {
  lines.value.splice(i, 1)
}

const isPicked = (entry) => lines.value.some((l) => l.entry.sku === entry.sku)

// Live aggregate COGS preview (materials + print + this month's overhead
// allocation) for the whole run — one request whenever the lines, client or
// date change. Prices do not affect COGS, so a price edit only recomputes
// the locally-derived delivery value below.
watch(
  [() => client.value?.id, () => date.value, lines],
  async () => {
    const active = lines.value.filter((l) => Number(l.qty) > 0)
    if (!client.value || !active.length) {
      preview.value = null
      return
    }
    try {
      preview.value = await api.post('/deliveries/preview/', {
        client: client.value.id,
        date: date.value,
        lines: active.map((l) => ({
          sku: l.entry.sku,
          qty_cases: String(l.qty),
        })),
      })
    } catch {
      preview.value = null
    }
  },
  { deep: true }
)

// Delivery value = sum(line qty × line price) — client-specific prices.
const deliveryValue = computed(() =>
  lines.value.reduce((sum, l) => sum + Number(l.qty) * Number(l.price || 0), 0)
)

// The money normally changes hands on the delivery date itself, so the payment
// date follows the delivery date until the user picks one deliberately.
watch(date, (value, previous) => {
  if (payDate.value === previous) payDate.value = value
})

// Switching the payment on pre-fills today's delivery date and the full
// delivery value — both stay editable, and nothing is sent until submit.
watch(payNow, (on) => {
  if (!on) return
  payDate.value = date.value
  if (!payAmount.value) payAmount.value = String(deliveryValue.value)
})

// Client pending balance after this delivery and the optional payment:
// live pending + this run's value − whatever is handed over now.
const pendingAfter = computed(
  () =>
    Number(client.value?.pending_amount || 0) +
    deliveryValue.value -
    (payNow.value ? Number(payAmount.value || 0) : 0)
)

const canSubmit = computed(() => {
  if (loading.value || !client.value || !lines.value.length) return false
  // A payment is optional, but once the switch is on it has to be a positive
  // amount — otherwise the money would look recorded when it was not.
  if (payNow.value && !(Number(payAmount.value) > 0)) return false
  return lines.value.every(
    (l) =>
      Number(l.qty) > 0 &&
      l.price !== null &&
      l.price !== '' &&
      Number(l.price) >= 0
  )
})

async function submit(force = false) {
  error.value = ''
  shortfall.value = null
  loading.value = true
  try {
    // One atomic request for every line — never a half-saved delivery. The
    // optional payment rides along in the same request, so the delivery and the
    // client transaction are saved together (or not at all).
    const payload = {
      client: client.value.id,
      date: date.value,
      note: note.value,
      force,
      lines: lines.value.map((l) => ({
        sku: l.entry.sku,
        qty_cases: String(l.qty),
        selling_price_per_case: String(l.price),
      })),
    }
    if (payNow.value && Number(payAmount.value) > 0) {
      payload.payment = {
        amount: String(payAmount.value),
        date: payDate.value,
        note: payNote.value,
      }
    }
    result.value = await api.post('/deliveries/bulk/', payload)
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
  lines.value = []
  skus.value = []
  date.value = today()
  note.value = ''
  payNow.value = false
  payAmount.value = ''
  payDate.value = today()
  payNote.value = ''
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
          Stock negative — fix via Stock Adjustment or backdated arrival
        </v-chip>
        <!-- A SKU with no linked materials consumes no stock at all, so the
             delivery is correct but stock stays where it was — flag it here
             too so the confirmation is not silently misleading. -->
        <v-chip
          v-if="result.skus_without_requirements?.length"
          color="info"
          size="small"
          class="ml-2"
        >
          No raw materials linked — stock untouched
          ({{ result.skus_without_requirements.join(', ') }})
        </v-chip>
      </v-alert>
      <v-card class="mb-3">
        <v-list density="compact">
          <v-list-item
            :title="`${client?.name} · ${num(result.total_cases)} cases across ${
              result.lines.length
            } SKU${result.lines.length > 1 ? 's' : ''}`"
            :subtitle="`${money(result.total_amount)} total`"
          />
          <template v-for="row in result.lines" :key="row.delivery_id">
            <v-divider />
            <v-list-item
              :title="row.sku_description"
              :subtitle="`${num(row.qty_cases)} cases × ${money(
                row.selling_price_per_case
              )} / case`"
            >
              <template #append>
                <div class="text-right">
                  <div class="text-body-2 font-weight-bold">
                    {{ money(row.amount) }}
                  </div>
                  <div class="text-caption" style="opacity: 0.8">
                    COGS {{ money(row.cogs_per_case) }}/case
                  </div>
                </div>
              </template>
            </v-list-item>
          </template>
          <v-divider />
          <v-list-item title="COGS (current)">
            <template #append>
              <strong>{{ money(result.cogs.per_case) }} / case</strong>
            </template>
          </v-list-item>
          <v-list-item title="Delivery value">
            <template #append>
              <strong>{{ money(result.total_amount) }}</strong>
            </template>
          </v-list-item>
          <!-- Payment noted on this screen (optional): shown so the user sees
               the money landed as a transaction under the client. -->
          <v-list-item
            v-if="result.payment"
            title="Payment recorded with delivery"
            :subtitle="`${result.payment.date} · ${result.payment.note}`"
          >
            <template #append>
              <strong class="text-success">
                {{ money(result.payment.amount) }}
              </strong>
            </template>
          </v-list-item>
          <v-list-item title="Total COGS (current)">
            <template #append>
              <strong>{{ money(result.total_cogs_current) }}</strong>
            </template>
          </v-list-item>
          <v-list-item
            :title="
              result.payment
                ? 'Client pending balance (after payment)'
                : 'Client pending balance'
            "
          >
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
      <!-- Page heading: same 16px bold scale as "Enter Arrived Stock" so the two
           entry screens read as one family. -->
      <div class="mb-4">
        <div class="text-subtitle-1 font-weight-bold">New delivery</div>
        <div class="text-caption text-medium-emphasis">
          Client → SKUs → quantity &amp; price per line. COGS updates as you
          type.
        </div>
      </div>

      <v-alert v-if="error" type="error" class="mb-3">{{ error }}</v-alert>

      <!-- 1. Client -->
      <v-card class="mb-3">
        <v-card-title
          class="d-flex align-center text-subtitle-1 font-weight-bold"
        >
          <v-avatar
            size="24"
            color="primary"
            variant="tonal"
            class="mr-2 text-caption font-weight-bold"
          >
            1
          </v-avatar>
          Client
        </v-card-title>
        <v-divider />
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

      <!-- 2. SKUs (client's preferred SKUs) — tap to add / remove a line -->
      <v-card class="mb-3" :disabled="!client">
        <v-card-title
          class="d-flex align-center text-subtitle-1 font-weight-bold"
        >
          <v-avatar
            size="24"
            color="primary"
            variant="tonal"
            class="mr-2 text-caption font-weight-bold"
          >
            2
          </v-avatar>
          SKUs
        </v-card-title>
        <v-divider />
        <v-card-text>
          <div class="d-flex flex-wrap ga-2">
            <v-btn
              v-for="entry in skus"
              :key="entry.id"
              :color="isPicked(entry) ? 'primary' : undefined"
              :variant="isPicked(entry) ? 'elevated' : 'outlined'"
              @click="toggleSku(entry)"
            >
              {{ entry.sku_description }}
              <span class="text-caption ml-1">
                ₹{{ entry.selling_price_per_case }}/case
              </span>
            </v-btn>
          </div>
          <p v-if="lines.length" class="text-caption mb-0">
            Tap a selected SKU again to remove its line.
          </p>
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
          <p v-if="!client" class="text-caption text-medium-emphasis mb-0">
            Pick a client first.
          </p>
        </v-card-text>
      </v-card>
      <!-- 3. Lines: quantity & price per selected SKU, plus date / note -->
      <v-card class="mb-3" :disabled="!lines.length">
        <v-card-title
          class="d-flex align-center text-subtitle-1 font-weight-bold"
        >
          <v-avatar
            size="24"
            color="primary"
            variant="tonal"
            class="mr-2 text-caption font-weight-bold"
          >
            3
          </v-avatar>
          Quantity &amp; price per line
        </v-card-title>
        <v-divider />
        <v-card-text>
          <v-text-field v-model="date" type="date" label="Delivery date" />
          <div
            v-for="(line, i) in lines"
            :key="line.entry.sku"
            :class="{ 'mb-4 pb-4 border-b': i < lines.length - 1 }"
          >
            <div class="d-flex align-center mb-1">
              <span class="text-subtitle-2 font-weight-bold">
                {{ line.entry.sku_description }}
              </span>
              <v-spacer />
              <v-btn
                icon="mdi-close"
                size="small"
                variant="text"
                :aria-label="`Remove ${line.entry.sku_description}`"
                @click="removeLine(i)"
              />
            </div>
            <v-text-field
              v-model.number="line.qty"
              type="number"
              min="1"
              label="Quantity (cases)"
              density="comfortable"
            />
            <!-- Short label; the "auto-filled, editable" note moved into the
                 hint instead of being crammed into the label. -->
            <v-text-field
              v-model="line.price"
              type="number"
              min="0"
              label="Selling price per case"
              prefix="₹"
              hint="Pre-filled from the client's SKU price — editable"
              persistent-hint
              density="comfortable"
            />
          </div>
          <v-text-field v-model="note" label="Note (optional)" class="mt-2" />
        </v-card-text>
      </v-card>

      <!-- 4. Payment (optional) — user request: the money handed over at the
           delivery can be noted right here instead of a separate trip to the
           client's ledger. Left off, the delivery is simply saved on credit. -->
      <v-card class="mb-3" :disabled="!lines.length">
        <v-card-title
          class="d-flex align-center text-subtitle-1 font-weight-bold"
        >
          <v-avatar
            size="24"
            color="primary"
            variant="tonal"
            class="mr-2 text-caption font-weight-bold"
          >
            4
          </v-avatar>
          Payment (optional)
        </v-card-title>
        <v-divider />
        <v-card-text>
          <v-switch
            v-model="payNow"
            color="success"
            density="compact"
            hide-details
            label="Payment received with this delivery"
          />
          <p class="text-caption text-medium-emphasis mt-1 mb-0">
            Optional — leave this off and the delivery is saved on credit as
            usual. An amount entered here is captured as a payment transaction
            under {{ client?.name }} immediately.
          </p>
          <template v-if="payNow">
            <v-text-field
              v-model="payAmount"
              type="number"
              min="0"
              prefix="₹"
              label="Amount received"
              density="comfortable"
              class="mt-3"
            />
            <div class="d-flex flex-wrap ga-2">
              <v-chip
                size="small"
                variant="tonal"
                @click="payAmount = String(deliveryValue)"
              >
                Full delivery value · {{ money(deliveryValue) }}
              </v-chip>
              <v-chip
                v-if="payAmount"
                size="small"
                variant="text"
                @click="payAmount = ''"
              >
                Clear
              </v-chip>
            </div>
            <v-text-field
              v-model="payDate"
              type="date"
              label="Payment date"
              density="comfortable"
              class="mt-2"
            />
            <v-text-field
              v-model="payNote"
              label="Payment note (optional)"
              hint="Stored on the payment entry in the client's ledger"
              persistent-hint
              density="comfortable"
            />
            <p class="text-caption mt-2 mb-0">
              Delivery value {{ money(deliveryValue) }} ·
              <template v-if="Number(payAmount) > 0">
                paying {{ money(payAmount) }} now ·
              </template>
              pending after this: <strong>{{ money(pendingAfter) }}</strong>
            </p>
          </template>
        </v-card-text>
      </v-card>

      <!-- Live COGS breakdown for the whole run: per-line rows on top, then
           the aggregate split stated once per case (spec 4.2). -->
      <v-card v-if="preview" variant="tonal" color="primary" class="mb-3">
        <v-card-text>
          <div class="text-caption font-weight-bold text-uppercase">
            Estimated COGS (per case, whole run)
          </div>
          <div class="text-h6 font-weight-bold">
            {{ money(preview.cogs.per_case) }}
          </div>
          <v-table density="compact" class="bg-transparent mt-2">
            <thead>
              <tr>
                <th class="px-0 text-left">SKU</th>
                <th class="px-0 text-right">Cases</th>
                <th class="px-0 text-right">COGS / case</th>
                <th class="px-0 text-right">Line COGS</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="line in preview.lines" :key="line.sku">
                <td class="px-0 text-body-2">{{ line.sku_description }}</td>
                <td class="text-right px-0">{{ num(line.qty_cases) }}</td>
                <td class="text-right px-0">{{ money(line.per_case) }}</td>
                <td class="text-right px-0">{{ money(line.total_cogs) }}</td>
              </tr>
            </tbody>
          </v-table>
          <v-divider class="my-2" />
          <v-table density="compact" class="bg-transparent">
            <tbody>
              <tr>
                <td class="px-0 text-body-2">Raw materials (incl. wastage)</td>
                <td class="text-right px-0">
                  {{ money(preview.cogs.materials_per_case) }}
                </td>
              </tr>
              <tr>
                <td class="px-0 text-body-2">Print / label (incl. wastage)</td>
                <td class="text-right px-0">
                  {{ money(preview.cogs.print_per_case) }}
                </td>
              </tr>
              <tr>
                <td class="px-0 text-body-2">
                  Overhead allocation
                  <div class="text-caption" style="opacity: 0.8">
                    pool {{ money(preview.cogs.details.overhead.total_monthly_overhead) }}
                    ÷ {{ preview.cogs.details.overhead.cases_sold_in_month }} cases
                  </div>
                </td>
                <td class="text-right px-0">
                  {{ money(preview.cogs.overhead_per_case) }}
                </td>
              </tr>
              <tr class="font-weight-bold">
                <td class="px-0">Total COGS</td>
                <td class="text-right px-0">{{ money(preview.cogs.per_case) }}</td>
              </tr>
            </tbody>
          </v-table>
          <div class="text-caption mt-2" style="opacity: 0.9">
            Delivery value
            <strong>{{ money(deliveryValue) }}</strong>
            · Total COGS (run)
            <strong>{{ money(preview.totals.cogs) }}</strong>
          </div>
        </v-card-text>
      </v-card>

      <!-- Preview shortfall: warn about stock before submitting (4.2 step 4).
           Hidden once the submit-time banner below takes over. -->
      <v-alert
        v-if="preview?.shortfall?.length && !shortfall"
        type="warning"
        variant="tonal"
        density="compact"
        class="mb-3"
      >
        <div class="font-weight-bold mb-1">Stock shortfall (preview)</div>
        <div
          v-for="s in preview.shortfall"
          :key="s.material_id"
          class="text-body-2"
        >
          <strong>{{ s.material }}</strong> — short by
          {{ num(s.short_by) }} {{ s.unit }} (need {{ num(s.required) }}, have
          {{ num(s.available) }})
        </div>
      </v-alert>

      <!-- No raw-material requirements: such a SKU consumes NO stock, so it
           can never report a shortfall and its COGS has no material part. Say
           so up front instead of leaving the user wondering why stock never
           moves (user report). -->
      <v-alert
        v-if="preview?.skus_without_requirements?.length"
        type="info"
        variant="tonal"
        density="compact"
        class="mb-3"
      >
        <div class="font-weight-bold mb-1">No raw-material requirements</div>
        <div class="text-body-2">
          <strong>{{ preview.skus_without_requirements.join(', ') }}</strong> —
          no materials are linked to this SKU, so this delivery will not move
          any stock (bottles, labels…) and its COGS has no material cost. Link
          the materials in <strong>Masters → SKU Master</strong>.
        </div>
      </v-alert>

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



