<script setup>
import { ref, computed, watch, onUnmounted, onErrorCaptured } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { apiBaseUrlMissing } from '@/api'
import { useUiStore } from '@/stores/ui'
import { useRefAuthStore } from '@/stores/auth'

const route = useRoute()
const router = useRouter()
const ui = useUiStore()
const auth = useRefAuthStore()
const drawer = ref(false)
const password = ref('')

const nav = [
  { to: '/', label: 'Home', icon: 'mdi-home' },
  { to: '/clients', label: 'Clients', icon: 'mdi-account-group' },
  { to: '/masters', label: 'Masters', icon: 'mdi-database' },
  { to: '/reports', label: 'Reports', icon: 'mdi-chart-line' },
]

// Views worth keeping mounted between navigations (perf plan 2.10): read-mostly
// lists and the dashboard, whose state costs something to rebuild. Data-entry
// screens (delivery, arrival, adjustment) and form/detail pages are deliberately
// left out so they mount fresh with clean inputs. Names are matched against the
// SFC filename through Vue's `__name` inference.
const cachedViews = [
  'HomeView',
  'ClientsView',
  'MastersView',
  'MaterialsView',
  'SkusView',
  'VendorsView',
  'ReportsView',
  'OverheadsView',
]

const isRoot = computed(() => route.path === '/')
// Screens the user asked to be plain — no header, no drawer, no bottom nav, so
// /statusupdate in particular offers no way back to Home (user request).
const isBare = computed(() => !!route.meta.bare)
const snack = computed(() => ui.toast)
const renderError = ref(null)
function reloadApp() {
  window.location.reload()
}

// A view that throws during render used to leave a blank page — invisible on
// meta.bare routes such as /statusupdate, which hide the app bar, drawer and
// bottom nav. Surface it instead, with a way back.
onErrorCaptured((err, instance, info) => {
  renderError.value = { message: err?.message || String(err), info }
  // eslint-disable-next-line no-console
  console.error('[jsd] view crashed', err, info)
  return false
})

// A later navigation gets a clean slate.
watch(
  () => route.fullPath,
  () => {
    renderError.value = null
  }
)

async function submitPassword() {
  if (auth.lockedOut || auth.checking || !password.value) return
  // The attempted path is recorded server-side so the audit log shows which
  // screen the visitor was trying to reach.
  const ok = await auth.unlock(password.value, route.path)
  if (ok) password.value = ''
}

// Drive the lockout countdown one second at a time while it is running.
let lockTimer = null
watch(
  () => auth.lockedOut,
  (locked) => {
    if (lockTimer) {
      clearInterval(lockTimer)
      lockTimer = null
    }
    if (locked) lockTimer = setInterval(() => auth.tick(), 1000)
  },
  { immediate: true }
)
onUnmounted(() => lockTimer && clearInterval(lockTimer))

function goBack() {
  // A view may register its own back behaviour; otherwise use browser history.
  if (ui.backAction) {
    ui.backAction()
    return
  }
  if (window.history.length > 1) router.back()
  else router.push('/')
}

function goHome() {
  if (route.path !== '/') router.push('/')
}
</script>

<template>
  <v-app>
    <!-- Header: JSD Group branding (spec 7). Hidden while the app is locked
         and on the deliberately plain screens (user request). -->
    <v-app-bar
      v-if="auth.unlocked && !isBare"
      flat
      color="primary"
      density="comfortable"
    >
      <v-btn
        v-if="!isRoot"
        icon="mdi-arrow-left"
        variant="text"
        color="white"
        @click="goBack"
      />
      <v-app-bar-title
        class="font-weight-bold"
        style="cursor: pointer"
        @click="goHome"
      >
        <v-icon start color="white">mdi-bottle-soda-classic-outline</v-icon>
        JSD Group
      </v-app-bar-title>
      <v-spacer />
      <!-- Vendor branding, top-right of the banner (user request). -->
      <span
        class="text-body-2 text-white mr-2 text-no-wrap"
        style="opacity: 0.9"
      >
        Pharmaco &copy;
      </span>
      <v-btn
        v-if="$vuetify.display.mdAndUp"
        icon="mdi-menu"
        variant="text"
        color="white"
        @click="drawer = !drawer"
      />
    </v-app-bar>

    <!-- Desktop: left sidebar (same routes as bottom nav) -->
    <v-navigation-drawer
      v-if="auth.unlocked && !isBare"
      v-model="drawer"
      :location="$vuetify.display.mdAndUp ? 'right' : undefined"
      :permanent="$vuetify.display.lgAndUp"
    >
      <v-list nav>
        <v-list-item
          v-for="item in nav"
          :key="item.to"
          :to="item.to"
          :prepend-icon="item.icon"
          :title="item.label"
          rounded="lg"
        />
        <v-divider class="my-2" />
        <v-list-item
          to="/stock/arrived"
          prepend-icon="mdi-truck-plus"
          title="Enter Arrived Stock"
          rounded="lg"
        />
        <v-list-item
          to="/stock/adjust"
          prepend-icon="mdi-scale-balance"
          title="Stock Adjustment"
          rounded="lg"
        />
      </v-list>
    </v-navigation-drawer>

    <v-main>
      <v-container
        fluid
        :class="auth.unlocked ? 'pa-3 pa-md-5' : 'fill-height align-center'"
        :style="auth.unlocked ? 'max-width: 1100px' : 'max-width: 420px'"
      >
        <!-- Loud, visible warning when the production build has no backend URL
             (see api/index.js) — otherwise every screen silently talks to
             localhost. Shown even while locked, so a misconfigured deploy is
             still diagnosable from the password screen. -->
        <v-alert
          v-if="apiBaseUrlMissing"
          type="error"
          variant="tonal"
          density="compact"
          class="mb-3"
          title="Backend URL not configured"
          text="Set VITE_API_BASE_URL to the Render backend URL in GitHub → Settings → Secrets and variables → Actions (repository variable or secret), then re-run the deploy workflow."
        />

        <!-- A view that threw during render (caught above) leaves an error
             instead of a blank page — invisible on meta.bare routes such as
             /statusupdate, which hide the app bar, drawer and bottom nav. -->
        <v-alert
          v-if="renderError"
          type="error"
          variant="tonal"
          density="compact"
          class="mb-3"
          title="This screen failed to render"
        >
          {{ renderError.message }}
          <v-btn
            size="small"
            variant="text"
            color="error"
            class="ml-2"
            @click="reloadApp"
          >
            Reload
          </v-btn>
        </v-alert>

        <!-- App gate (user request): ask for the password before anything is
             shown. The check, the 300s restriction and the audit trail are
             server-side; this only collects and submits the password. -->
        <v-card v-if="!auth.unlocked" class="pa-4" elevation="2">
          <div class="text-center mb-3">
            <v-icon size="42" color="primary">mdi-lock-outline</v-icon>
            <div class="text-h6 font-weight-bold mt-1">JSD Group</div>
            <div class="text-caption text-medium-emphasis">
              Enter the password to continue
            </div>
          </div>
          <v-alert
            v-if="auth.error"
            :type="auth.lockedOut ? 'warning' : 'error'"
            density="compact"
            class="mb-3"
            :text="auth.error"
          />
          <v-text-field
            v-model="password"
            type="password"
            label="Password"
            prepend-inner-icon="mdi-key-variant"
            autofocus
            hide-details="auto"
            :disabled="auth.lockedOut || auth.checking"
            @keyup.enter="submitPassword"
          />
          <v-btn
            color="primary"
            block
            size="large"
            class="mt-4"
            :loading="auth.checking"
            :disabled="auth.lockedOut"
            @click="submitPassword"
          >
            {{
              auth.lockedOut
                ? `Restricted — retry in ${auth.retryAfter}s`
                : 'Unlock'
            }}
          </v-btn>
          <div class="text-caption text-medium-emphasis text-center mt-4">
            Pharmaco &copy;
          </div>
        </v-card>

        <!-- Keep the tab-style views alive so returning to them is instant and
             their filters / scroll survive; everything else mounts fresh
             (perf plan 2.10). -->
        <router-view v-else v-slot="{ Component }">
          <keep-alive :include="cachedViews">
            <component :is="Component" />
          </keep-alive>
        </router-view>
      </v-container>
    </v-main>

    <!-- Mobile: bottom navigation bar (spec 4) -->
    <v-bottom-navigation
      v-if="auth.unlocked && !isBare && $vuetify.display.smAndDown"
      grow
      color="primary"
      active-color="primary"
      :model-value="route.path"
    >
      <v-btn
        v-for="item in nav"
        :key="item.to"
        :value="item.to"
        :to="item.to"
        :exact="item.to === '/'"
      >
        <v-icon>{{ item.icon }}</v-icon>
        <span class="text-caption">{{ item.label }}</span>
      </v-btn>
    </v-bottom-navigation>

    <!-- Green/red feedback toast (spec 7) -->
    <v-snackbar
      :model-value="!!snack"
      :color="snack?.color"
      :timeout="3000"
      location="top"
      @update:model-value="(v) => !v && (ui.toast = null)"
    >
      {{ snack?.text }}
    </v-snackbar>
  </v-app>
</template>
