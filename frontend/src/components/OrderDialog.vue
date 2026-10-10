<script setup>
/**
 * Enter Order / edit order (user request).
 *
 * Pick a client -> the SKUs already priced for that client appear as chips ->
 * tap one to add a line with a PRE-FILLED price that stays editable -> enter
 * the quantity -> Confirm.
 *
 * The COGS preview comes from the same /deliveries/preview/ endpoint the Add
 * Delivery screen uses, so the user sees the cost of the order before it is
 * logged. Pass `order` to edit an existing one instead of creating.
 */
import { ref, watch, computed } from 'vue'
import { api, listify } from '@/api'
import { useRefDataStore } from '@/stores/refdata'
import { useOrdersStore } from '@/stores/orders'
import { useUiStore } from '@/stores/ui'
import { money, num, today } from '@/utils/format'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  /** Existing order to edit; null/undefined = create. */
  order: { type: Object, default: null },
})

const emit = defineEmits(['update:modelValue', 'saved'])

const refdata = useRefDataStore()
const orders = useOrdersStore()
const ui = useUiStore()

const open = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v),
})

const busy = ref(false)
const error = ref('')
const shortage = ref(null) // 409 payload from complete/preview, shown as an alert

const clientId = ref(null)
const orderDate = ref(today())
const notes = ref('')
const lines = ref([]) // [{sku_id, description, qty_cases, price, cogs, memo}]

const editing = computed(() => Boolean(props.order))

const client = computed(
  () => refdata.clients.find((c) => c.id === clientId.value) || null
)

// SKUs already priced for this client (refdata caches sku_prices per client).
const clientSkus = computed(() => {
  const rows = client.value?.sku_prices || []
  return rows.map((r) => ({
    id: r.sku,
    description: r.sku_description || `SKU ${r.sku}`,
    price: Number(r.selling_price_per_case),
  }))
})

const lineOf = (skuId) => lines.value.find((l) => l.sku_id === skuId)

function toggleSku(sku) {
  const existing = lineOf(sku.id)
  if (existing) {
    lines.value = lines.value.filter((l) => l.sku_id !== sku.id)
    return
  }
  lines.value = [
    ...lines.value,
    {
      sku_id: sku.id,
      description: sku.description,
      qty_cases: 1,
      price: sku.price,
      cogs: null,
    },
  ]
}

function dropLine(skuId) {
  lines.value = lines.value.filter((l) => l.sku_id !== skuId)
}

const totalAmount = computed(() =>
  lines.value.reduce((s, l) => s + Number(l.qty_cases || 0) * Number(l.price || 0), 0)
)

const totalCogs = computed(() =>
  lines.value.reduce((s, l) => {
    if (l.cogs == null) return s
    return s + Number(l.cogs) * Number(l.qty_cases || 0)
  }, 0)
)

const canSave = computed(
  () => clientId.value && lines.value.length > 0 && !busy.value
)

// Fill the dialog from the order being edited, or reset it for a new one.
watch(open, async (v) => {
  if (!v) return
  error.value = ''
  shortage.value = null
  if (!refdata.clients.length) await refdata.ensureLoaded?.()
  if (props.order) {
    clientId.value = props.order.client
    orderDate.value = props.order.order_date || today()
    notes.value = props.order.notes || ''
    lines.value = (props.order.items || []).map((i) => ({
      sku_id: i.sku,
      description: i.sku_description,
      qty_cases: Number(i.qty_cases),
      price: Number(i.selling_price_per_case),
      cogs: null,
    }))
  } else {
    clientId.value = null
    orderDate.value = today()
    notes.value = ''
    lines.value = []
  }
})

// Cost preview, same source as Add Delivery. Debounced by watcher granularity:
// one request per change is fine here (the dialog is small and the endpoint is
// cheap), and it keeps the number the user sees exactly the one that will be
// booked.
watch(
  [clientId, lines],
  async () => {
    if (!clientId.value || !lines.value.length) return
    for (const l of lines.value) {
      if (!(Number(l.qty_cases) > 0)) {
        l.cogs = null
        continue
      }
      try {
        const p = await api.get('/deliveries/preview/', {
          client: clientId.value,
          sku: l.sku_id,
          qty_cases: String(l.qty_cases),
          selling_price_per_case: String(l.price),
          date: orderDate.value,
        })
        l.cogs = p.cogs_per_case != null ? Number(p.cogs_per_case) : null
      } catch {
        l.cogs = null // a failed preview must not block saving
      }
    }
  },
  { deep: true }
)

async function save() {
  if (!canSave.value) return
  busy.value = true
  error.value = ''
  shortage.value = null
  try {
    await orders.save(
      {
        client: clientId.value,
        order_date: orderDate.value,
        notes: notes.value,
        items: lines.value.map((l) => ({
          sku: l.sku_id,
          qty_cases: String(l.qty_cases),
          // Blank price means "use the client's configured one" server-side.
          selling_price_per_case: String(l.price),
        })),
      },
      props.order?.id || null
    )
    ui.notify(editing.value ? 'Order updated' : 'Order logged')
    open.value = false
    emit('saved')
  } catch (e) {
    // 409 from the pipeline carries the per-material gaps in the body; the
    // banner below lists them so the user can decide to proceed anyway.
    if (e.data?.shortages) {
      shortage.value = e.data.shortages
    } else {
      error.value = e.message
    }
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <v-dialog v-model="open" max-width="720" scrollable>
    <v-card>
      <v-card-title class="d-flex align-center text-subtitle-1 font-weight-bold">
        <v-icon start color="primary">mdi-notebook-plus</v-icon>
        {{ editing ? 'Edit Order' : 'Enter Order' }}
      </v-card-title>
      <v-divider />
      <v-card-text>
        <v-alert v-if="error" type="error" density="compact" class="mb-4">
          {{ error }}
        </v-alert>

        <v-select
          v-model="clientId"
          :items="refdata.clients"
          item-title="name"
          item-value="id"
          label="Client"
          variant="outlined"
          density="compact"
          :disabled="editing"
          class="mb-2"
        />

        <v-row dense class="mb-2">
          <v-col cols="6">
            <v-text-field
              v-model="orderDate"
              type="date"
              label="Order date"
              variant="outlined"
              density="compact"
            />
          </v-col>
          <v-col cols="6" class="d-flex align-center">
            <span class="text-body-2">
              Total:
              <strong>{{ money(totalAmount) }}</strong>
            </span>
          </v-col>
        </v-row>

        <!-- SKUs already priced for this client (the "pre-determined SKUs"). -->
        <div v-if="client" class="mb-1 text-caption text-medium-emphasis">
          Tap a SKU to add it — the price is pre-filled and editable.
        </div>
        <div v-if="client && !clientSkus.length" class="text-caption text-warning mb-2">
          No priced SKUs for this client yet — add them on the client's screen.
        </div>
        <div v-if="client" class="d-flex flex-wrap ga-1 mb-3">
          <v-chip
            v-for="s in clientSkus"
            :key="s.id"
            size="small"
            :color="lineOf(s.id) ? 'primary' : 'default'"
            :variant="lineOf(s.id) ? 'flat' : 'outlined'"
            @click="toggleSku(s)"
          >
            {{ s.description }} · {{ money(s.price) }}
          </v-chip>
        </div>

        <!-- One row per chosen line: quantity and editable price. -->
        <v-table v-if="lines.length" density="compact">
          <thead>
            <tr>
              <th>SKU</th>
              <th style="width: 110px">Qty (cases)</th>
              <th style="width: 130px">Price / case</th>
              <th class="text-right">Amount</th>
              <th style="width: 48px" />
            </tr>
          </thead>
          <tbody>
            <tr v-for="l in lines" :key="l.sku_id">
              <td class="text-body-2">{{ l.description }}</td>
              <td>
                <v-text-field
                  v-model.number="l.qty_cases"
                  type="number"
                  min="0"
                  density="compact"
                  hide-details
                  variant="underlined"
                />
              </td>
              <td>
                <v-text-field
                  v-model.number="l.price"
                  type="number"
                  min="0"
                  density="compact"
                  hide-details
                  variant="underlined"
                />
              </td>
              <td class="text-right text-body-2">
                {{ money(l.qty_cases * l.price) }}
                <div v-if="l.cogs != null" class="text-caption text-medium-emphasis">
                  cost {{ money(l.cogs) }}/case
                </div>
              </td>
              <td>
                <v-btn
                  icon="mdi-close"
                  size="x-small"
                  variant="text"
                  @click="dropLine(l.sku_id)"
                />
              </td>
            </tr>
          </tbody>
        </v-table>

        <v-text-field
          v-model="notes"
          label="Notes"
          variant="outlined"
          density="compact"
          class="mt-2"
        />

        <v-alert
          v-if="shortage"
          type="warning"
          density="compact"
          class="mt-2"
        >
          Stock is short for this order:
          <span v-for="s in shortage" :key="s.material_id" class="d-block">
            {{ s.material }} — short by {{ num(s.short_by) }} {{ s.unit }}
          </span>
        </v-alert>
      </v-card-text>
      <v-divider />
      <v-card-actions>
        <v-spacer />
        <v-btn variant="text" @click="open = false">Cancel</v-btn>
        <v-btn
          color="primary"
          variant="flat"
          :disabled="!canSave"
          :loading="busy"
          @click="save"
        >
          Confirm
        </v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>
</template>

