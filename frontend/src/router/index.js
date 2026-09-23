import { createRouter, createWebHistory } from 'vue-router'

const routes = [
  { path: '/', name: 'home', component: () => import('@/views/HomeView.vue') },
  {
    path: '/delivery/new',
    name: 'add-delivery',
    component: () => import('@/views/AddDeliveryView.vue'),
    meta: { title: 'Add Delivery' },
  },
  {
    path: '/stock/arrived',
    name: 'arrived-stock',
    component: () => import('@/views/ArrivedStockView.vue'),
    meta: { title: 'Enter Arrived Stock' },
  },
  {
    path: '/stock/adjust',
    name: 'stock-adjust',
    component: () => import('@/views/StockAdjustmentView.vue'),
    meta: { title: 'Stock Adjustment' },
  },
  {
    path: '/clients',
    name: 'clients',
    component: () => import('@/views/ClientsView.vue'),
  },
  {
    path: '/clients/new',
    name: 'client-new',
    component: () => import('@/views/ClientFormView.vue'),
    meta: { title: 'New Client' },
  },
  {
    path: '/clients/:id',
    name: 'client-detail',
    component: () => import('@/views/ClientDetailView.vue'),
  },
  {
    path: '/clients/:id/edit',
    name: 'client-edit',
    component: () => import('@/views/ClientFormView.vue'),
    meta: { title: 'Edit Client' },
  },
  {
    path: '/masters',
    name: 'masters',
    component: () => import('@/views/MastersView.vue'),
  },
  {
    path: '/masters/materials',
    name: 'materials',
    component: () => import('@/views/MaterialsView.vue'),
    meta: { title: 'MVP Master' },
  },
  {
    path: '/masters/vendors',
    name: 'vendors',
    component: () => import('@/views/VendorsView.vue'),
    meta: { title: 'Vendors' },
  },
  {
    path: '/masters/materials/:id',
    name: 'material-detail',
    component: () => import('@/views/MaterialDetailView.vue'),
  },
  {
    path: '/masters/skus',
    name: 'skus',
    component: () => import('@/views/SkusView.vue'),
    meta: { title: 'SKU Master' },
  },
  {
    path: '/masters/skus/:id',
    name: 'sku-detail',
    component: () => import('@/views/SkuDetailView.vue'),
  },
  {
    path: '/masters/overheads',
    name: 'overheads',
    component: () => import('@/views/OverheadsView.vue'),
    meta: { title: 'Overheads' },
  },
  {
    path: '/reports',
    name: 'reports',
    component: () => import('@/views/ReportsView.vue'),
  },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
  scrollBehavior() {
    return { top: 0 }
  },
})

router.afterEach((to) => {
  document.title = to.meta.title
    ? `${to.meta.title} · JSD Group`
    : 'JSD Group — COGS & Inventory'
})

export default router
