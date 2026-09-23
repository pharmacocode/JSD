<script setup>
/** Masters submenu (spec 4.6): MVP Master, SKU Master, Overheads. */
import { useRouter } from 'vue-router'

const router = useRouter()

const sections = [
  {
    to: '/masters/materials',
    title: 'Material, Vendor & Price Master',
    subtitle: 'Bottles, caps, client labels · stock, prices, alerts',
    icon: 'mdi-package-variant-closed',
    color: 'primary',
  },
  {
    to: '/masters/vendors',
    title: 'Vendor Master',
    subtitle: 'Vendor list with contact number and amount owed',
    icon: 'mdi-store',
    color: 'success',
  },
  {
    to: '/masters/skus',
    title: 'SKU Master',
    subtitle: 'Products, per-case material requirements, print cost, cost breakup',
    icon: 'mdi-barcode',
    color: 'secondary',
  },
  {
    to: '/masters/overheads',
    title: 'Overheads & Labour',
    subtitle: 'Rent, Diesel, Electricity, Labour by month · employees',
    icon: 'mdi-cash-multiple',
    color: 'warning',
  },
]
</script>

<template>
  <div>
    <v-card
      v-for="s in sections"
      :key="s.to"
      class="mb-3"
      :to="s.to"
      hover
    >
      <v-card-text class="d-flex align-center">
        <v-avatar :color="s.color" size="44" variant="tonal">
          <v-icon>{{ s.icon }}</v-icon>
        </v-avatar>
        <div class="ml-4">
          <div class="font-weight-bold">{{ s.title }}</div>
          <div class="text-caption text-medium-contrast">{{ s.subtitle }}</div>
        </div>
        <v-spacer />
        <v-icon>mdi-chevron-right</v-icon>
      </v-card-text>
    </v-card>

    <v-btn
      variant="tonal"
      color="error"
      block
      prepend-icon="mdi-scale-balance"
      :to="{ path: '/stock/adjust' }"
    >
      Stock Adjustment
    </v-btn>
  </div>
</template>
