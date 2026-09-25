<script setup>
/**
 * Overheads (spec 3.10 / 4.6): month picker, editable category amounts
 * (Rent/Diesel/Electricity/Labour pre-seeded + custom), Labour expands to
 * employee list + payments which auto-roll-up into the month's Labour total.
 */
import { ref, onMounted, watch, computed } from 'vue'
import { api, listify } from '@/api'
import { useUiStore } from '@/stores/ui'
import { money, monthLabel } from '@/utils/format'
import MonthPicker from '@/components/MonthPicker.vue'

const ui = useUiStore()

const categories = ref([])
const overheads = ref([]) // rows for selected month
const employees = ref([])
const payments = ref([])
const labourOpen = ref(false)
const loading = ref(true)
const saving = ref(false)
const error = ref('')

const catDialog = ref(false)
const catName = ref('')
const empDialog = ref(false)
// One dialog for both add and edit — `empForm.id` decides which (user request:
// employee details used to be create-only and could not be corrected).
const empForm = ref(blankEmployee())
const payDialog = ref(false)
const payForm = ref({ employee: null, amount_paid: null, date: '', note: '' })

const labourCategory = computed(() => categories.value.find((c) => c.name === 'Labour'))
const labourRow = computed(() =>
  overheads.value.find((o) => o.category_name === 'Labour')
)

async function load() {
  loading.value = true
  try {
    const [cats, oh, emps, pays] = await Promise.all([
      api.get('/overhead-categories/'),
      api.get('/overheads/', { month: ui.month }),
      api.get('/employees/'),
      api.get('/employee-payments/', { month: ui.month }),
    ])
    categories.value = listify(cats)
    overheads.value = listify(oh)
    employees.value = listify(emps)
    payments.value = listify(pays)
    // Ensure every category has an editable row for this month
    // (Labour excluded — it is always the auto-summed payment total).
    for (const c of categories.value) {
      if (c.name === 'Labour') continue
      if (!overheads.value.find((o) => o.category === c.id)) {
        overheads.value.push({
          id: null,
          category: c.id,
          category_name: c.name,
          month: ui.month,
          amount: '0',
        })
      }
    }
  } finally {
    loading.value = false
  }
}

onMounted(load)
watch(() => ui.month, load)

async function saveAmount(row) {
  saving.value = true
  error.value = ''
  try {
    const payload = {
      category: row.category,
      month: ui.month,
      amount: String(row.amount),
    }
    if (row.id) await api.put(`/overheads/${row.id}/`, payload)
    else {
      const created = await api.post('/overheads/', payload)
      row.id = created.id
    }
    ui.notify(`${row.category_name} updated`)
  } catch (e) {
    error.value = e.message
  } finally {
    saving.value = false
  }
}

async function addCategory() {
  if (!catName.value.trim()) return
  await api.post('/overhead-categories/', { name: catName.value.trim() })
  catDialog.value = false
  catName.value = ''
  await load()
}

async function removeCategory(c) {
  if (c.is_default) {
    error.value = 'Default categories cannot be deleted.'
    return
  }
  if (!confirm(`Delete category "${c.name}"?`)) return
  await api.del(`/overhead-categories/${c.id}/`)
  await load()
}

function blankEmployee() {
  return { id: null, name: '', role: '', monthly_pay: 0, active: true }
}

/** Open the dialog in add mode (no argument) or edit mode (employee row). */
function openEmployee(employee = null) {
  empForm.value = employee
    ? {
        id: employee.id,
        name: employee.name,
        role: employee.role || '',
        monthly_pay: Number(employee.monthly_pay || 0),
        active: employee.active !== false,
      }
    : blankEmployee()
  empDialog.value = true
}

async function saveEmployee() {
  error.value = ''
  if (!empForm.value.name?.trim()) {
    error.value = 'Employee name is required.'
    return
  }
  saving.value = true
  try {
    const payload = {
      name: empForm.value.name.trim(),
      role: empForm.value.role || '',
      monthly_pay: String(empForm.value.monthly_pay || 0),
      active: empForm.value.active !== false,
    }
    if (empForm.value.id) {
      await api.put(`/employees/${empForm.value.id}/`, payload)
      ui.notify(`${payload.name} updated`)
    } else {
      await api.post('/employees/', payload)
      ui.notify(`${payload.name} added`)
    }
    empDialog.value = false
    empForm.value = blankEmployee()
    employees.value = listify(await api.get('/employees/'))
  } catch (e) {
    error.value = e.message
  } finally {
    saving.value = false
  }
}

async function savePayment() {
  if (!payForm.value.employee || !payForm.value.amount_paid) {
    error.value = 'Employee and amount are required.'
    return
  }
  await api.post('/employee-payments/', {
    employee: payForm.value.employee,
    month: ui.month,
    amount_paid: String(payForm.value.amount_paid),
    date: payForm.value.date || new Date().toISOString().slice(0, 10),
    note: payForm.value.note,
  })
  payDialog.value = false
  payForm.value = { employee: null, amount_paid: null, date: '', note: '' }
  await load() // Labour row is now re-rolled-up server-side
}

async function deletePayment(p) {
  if (!confirm('Delete this payment? The Labour total will re-sum.')) return
  await api.del(`/employee-payments/${p.id}/`)
  await load()
}
</script>

<template>
  <div>
    <MonthPicker />
    <v-alert v-if="error" type="error" density="compact" class="mb-3">
      {{ error }}
    </v-alert>

    <!-- Category amounts for selected month -->
    <v-card class="mb-3" :loading="loading">
      <v-card-title class="d-flex align-center text-subtitle-1">
        Overheads — {{ monthLabel(ui.month) }}
        <v-spacer />
        <v-btn size="small" variant="tonal" prepend-icon="mdi-plus" @click="catDialog = true">
          Category
        </v-btn>
      </v-card-title>
      <v-divider />
      <v-list density="compact">
        <v-list-group v-if="labourCategory" v-model="labourOpen" prepend-icon="mdi-account-hard-hat">
          <template #activator="{ props }">
            <v-list-item
              v-bind="props"
              title="Labour"
              subtitle="auto-summed from employee payments — no manual entry"
            >
              <template #append>
                <span class="font-weight-bold mr-3">
                  {{ money(labourRow?.amount || 0) }}
                </span>
              </template>
            </v-list-item>
          </template>

          <!-- Labour expands: employees + payments (spec 4.6) -->
          <v-card flat class="ml-8 mb-2">
            <v-card-text class="py-2">
              <div class="d-flex align-center mb-2">
                <span class="text-subtitle-2">Employees</span>
                <v-spacer />
                <v-btn size="x-small" variant="tonal" @click="openEmployee()">
                  Add employee
                </v-btn>
                <v-btn size="x-small" variant="tonal" class="ml-1" @click="payDialog = true">
                  Log payment
                </v-btn>
              </div>

              <v-table density="compact">
                <tbody>
                  <tr v-for="e in employees" :key="e.id">
                    <td>
                      {{ e.name }}
                      <span class="text-caption text-medium-contrast">
                        {{ e.role }}
                      </span>
                      <v-chip
                        v-if="!e.active"
                        size="x-small"
                        variant="tonal"
                        class="ml-1"
                      >
                        inactive
                      </v-chip>
                    </td>
                    <td class="text-right">{{ money(e.monthly_pay) }}/mo</td>
                    <td style="width: 40px">
                      <v-btn
                        icon="mdi-pencil-outline"
                        size="x-small"
                        variant="text"
                        title="Edit employee"
                        @click="openEmployee(e)"
                      />
                    </td>
                  </tr>
                  <tr v-if="!employees.length">
                    <td colspan="3" class="text-medium-contrast">
                      No employees yet — use Add employee.
                    </td>
                  </tr>
                </tbody>
              </v-table>

              <div class="text-subtitle-2 mt-3">Payments this month</div>
              <v-table density="compact">
                <tbody>
                  <tr v-for="p in payments" :key="p.id">
                    <td>{{ p.employee_name }}</td>
                    <td>{{ p.date }}</td>
                    <td class="text-right">{{ money(p.amount_paid) }}</td>
                    <td style="width: 36px">
                      <v-btn
                        icon="mdi-delete-outline"
                        size="x-small"
                        variant="text"
                        color="error"
                        @click="deletePayment(p)"
                      />
                    </td>
                  </tr>
                  <tr v-if="!payments.length">
                    <td colspan="4" class="text-medium-contrast">
                      No payments logged for this month.
                    </td>
                  </tr>
                </tbody>
              </v-table>

              <div class="text-caption text-medium-contrast mt-2">
                Labour total = sum of the payments above. It updates
                automatically — there is no manual entry.
              </div>
            </v-card-text>
          </v-card>
        </v-list-group>

        <v-list-item
          v-for="row in overheads.filter((o) => o.category_name !== 'Labour')"
          :key="row.category"
          :title="row.category_name"
        >
          <template #append>
            <v-text-field
              v-model.number="row.amount"
              type="number"
              prefix="₹"
              density="compact"
              hide-details
              style="width: 130px"
            />
            <v-btn
              icon="mdi-content-save"
              size="x-small"
              variant="text"
              class="ml-1"
              @click="saveAmount(row)"
            />
            <v-btn
              v-if="!categories.find((c) => c.id === row.category)?.is_default"
              icon="mdi-delete-outline"
              size="x-small"
              variant="text"
              color="error"
              @click="removeCategory(categories.find((c) => c.id === row.category))"
            />
          </template>
        </v-list-item>
      </v-list>
    </v-card>

    <!-- Add custom category -->
    <v-dialog v-model="catDialog" max-width="360">
      <v-card>
        <v-card-title>New overhead category</v-card-title>
        <v-card-text>
          <v-text-field v-model="catName" label="Name (e.g. Packaging)" />
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="catDialog = false">Cancel</v-btn>
          <v-btn color="primary" @click="addCategory">Add</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <!-- Add / edit employee (details stay editable — user request) -->
    <v-dialog v-model="empDialog" max-width="400">
      <v-card>
        <v-card-title>
          {{ empForm.id ? 'Edit employee' : 'Add employee' }}
        </v-card-title>
        <v-card-text>
          <v-text-field v-model="empForm.name" label="Name" />
          <v-text-field v-model="empForm.role" label="Role" />
          <v-text-field
            v-model.number="empForm.monthly_pay"
            type="number"
            prefix="₹"
            label="Monthly pay"
          />
          <v-switch
            v-model="empForm.active"
            color="primary"
            label="Active"
            hide-details
            density="compact"
          />
          <div class="text-caption text-medium-contrast mt-1">
            Inactive employees stay in the list — their logged payments keep
            their month's Labour total intact.
          </div>
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="empDialog = false">Cancel</v-btn>
          <v-btn color="primary" :loading="saving" @click="saveEmployee">
            Save
          </v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <!-- Log employee payment (rolls into Labour) -->
    <v-dialog v-model="payDialog" max-width="400">
      <v-card>
        <v-card-title>Log employee payment</v-card-title>
        <v-card-text>
          <v-select
            v-model="payForm.employee"
            :items="employees"
            item-title="name"
            item-value="id"
            label="Employee"
          />
          <v-text-field
            v-model.number="payForm.amount_paid"
            type="number"
            prefix="₹"
            label="Amount paid"
          />
          <v-text-field v-model="payForm.date" type="date" label="Date" />
          <v-text-field v-model="payForm.note" label="Note" />
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="payDialog = false">Cancel</v-btn>
          <v-btn color="success" @click="savePayment">Save</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>
  </div>
</template>

