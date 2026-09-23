<script setup>
import { ref, computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useUiStore } from '@/stores/ui'

const route = useRoute()
const router = useRouter()
const ui = useUiStore()
const drawer = ref(false)

const nav = [
  { to: '/', label: 'Home', icon: 'mdi-home' },
  { to: '/clients', label: 'Clients', icon: 'mdi-account-group' },
  { to: '/masters', label: 'Masters', icon: 'mdi-database' },
  { to: '/reports', label: 'Reports', icon: 'mdi-chart-line' },
]

const isRoot = computed(() => route.path === '/')
const snack = computed(() => ui.toast)

function goBack() {
  // Multi-step views register their own back behaviour (e.g. Add Delivery
  // steps back through its steps); otherwise use browser history.
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
    <!-- Header: JSD Group branding (spec 7) -->
    <v-app-bar flat color="primary" density="comfortable">
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
      <v-container fluid class="pa-3 pa-md-5" style="max-width: 1100px">
        <router-view />
      </v-container>
    </v-main>

    <!-- Mobile: bottom navigation bar (spec 4) -->
    <v-bottom-navigation
      v-if="$vuetify.display.smAndDown"
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
