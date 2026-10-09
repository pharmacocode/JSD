<script setup>
/**
 * Login attempts (user request): the audit trail behind the app gate. Every
 * successful and failed password attempt, with the visitor's IP, best-effort
 * location, device and the screen they were trying to reach — read-only, and
 * written by the backend only (core.views.PasswordView).
 */
import { ref, watch, onMounted } from 'vue'
import { api } from '@/api'
import { useUiStore } from '@/stores/ui'

const ui = useUiStore()
const rows = ref([])
const loading = ref(true)
const error = ref('')
const filter = ref('all') // all | failed | success

async function load() {
  loading.value = true
  error.value = ''
  try {
    const params = filter.value === 'all' ? undefined : { result: filter.value }
    rows.value = await api.get('/login-attempts/', params)
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}

watch(filter, load)
onMounted(load)

function when(iso) {
  const d = new Date(iso)
  return Number.isNaN(d.getTime())
    ? iso
    : d.toLocaleString('en-IN', { dateStyle: 'medium', timeStyle: 'short' })
}

function location(row) {
  const parts = [row.geo_city, row.geo_region, row.geo_country].filter(Boolean)
  if (parts.length) return parts.join(', ')
  // Say WHY the location is blank rather than leaving a mystery gap.
  if (row.geo_source === 'skipped') return 'Local / private address'
  if (row.geo_source === 'fallback') return row.geo_error || 'Lookup unavailable'
  return '—'
}

function device(row) {
  const ua = row.user_agent || ''
  if (!ua) return '—'
  const browser = /Edg\//.test(ua) ? 'Edge'
    : /Chrome\//.test(ua) ? 'Chrome'
    : /Safari\//.test(ua) ? 'Safari'
    : /Firefox\//.test(ua) ? 'Firefox'
    : 'Unknown'
  const os = /Windows/.test(ua) ? 'Windows'
    : /Android/.test(ua) ? 'Android'
    : /(iPhone|iPad)/.test(ua) ? 'iOS'
    : /Mac OS/.test(ua) ? 'macOS'
    : /Linux/.test(ua) ? 'Linux'
    : ''
  return os ? `${browser} on ${os}` : browser
}
</script>

<template>
  <div>
    <div class="d-flex align-center flex-wrap ga-2 mb-3">
      <v-icon color="primary">mdi-account-key</v-icon>
      <span class="text-subtitle-1 font-weight-bold">Login attempts</span>
      <v-spacer />
      <v-btn-toggle v-model="filter" density="compact" variant="tonal" mandatory>
        <v-btn value="all" size="small">All</v-btn>
        <v-btn value="failed" size="small">Failed</v-btn>
        <v-btn value="success" size="small">Successful</v-btn>
      </v-btn-toggle>
      <v-btn
        icon="mdi-refresh"
        size="small"
        variant="text"
        :loading="loading"
        @click="load"
      />
    </div>

    <v-alert v-if="error" type="error" density="compact" class="mb-3">
      {{ error }} — check the API connection.
    </v-alert>

    <v-card :loading="loading">
      <v-table v-if="rows.length" density="compact">
        <thead>
          <tr>
            <th>When</th>
            <th>Result</th>
            <th>IP</th>
            <th>Location</th>
            <th>Device</th>
            <th>Screen</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in rows" :key="r.id">
            <td class="text-no-wrap">{{ when(r.timestamp) }}</td>
            <td>
              <v-chip
                size="x-small"
                variant="flat"
                :color="r.success ? 'success' : r.locked_out ? 'warning' : 'error'"
              >
                {{ r.success ? 'Successful' : r.locked_out ? 'Blocked' : 'Wrong password' }}
              </v-chip>
            </td>
            <td class="text-no-wrap">{{ r.ip || '—' }}</td>
            <td>{{ location(r) }}</td>
            <td>{{ device(r) }}</td>
            <td class="text-no-wrap">{{ r.path || '—' }}</td>
          </tr>
        </tbody>
      </v-table>
      <div
        v-else-if="!loading"
        class="text-center text-medium-emphasis pa-6"
      >
        No attempts recorded yet.
      </div>
    </v-card>

    <div class="text-caption text-medium-emphasis mt-3">
      {{ rows.length }} attempt{{ rows.length === 1 ? '' : 's' }} shown, newest
      first. Three wrong passwords from one address restricts that visitor for
      300 seconds.
    </div>
  </div>
</template>