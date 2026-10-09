/**
 * Orders pipeline state (user request).
 *
 *   PENDING    -> Enter Order (reserves nothing)
 *   COMPLETED  -> "moved to Ready to Deliver" (stock leaves Stock in Hand)
 *   DELIVERED  -> every line has a real StockDelivery behind it
 *
 * The figures the panels show come from `GET /orders/summary/`, which derives
 * commitment on the server (physical stock − committed), so there is no
 * client-side arithmetic that can drift from the ledger. `refresh()` is
 * called after every transition; `invalidate()` lets other screens drop the
 * memo after they change stock (an arrival, a delivery, a Masters edit).
 */
import { defineStore } from 'pinia'
import { listify } from '@/api'
import { api } from '@/api'

const STATUS = { PENDING: 'PENDING', COMPLETED: 'COMPLETED', DELIVERED: 'DELIVERED' }

export const useOrdersStore = defineStore('orders', {
  state: () => ({
    pending: [],
    ready: [],
    commitment: [],
    pendingCount: 0,
    readyCount: 0,
    loading: false,
    error: '',
    /** Set while the Enter Order dialog is open; null when closed. */
    editingOrder: null,
    dialogOpen: false,
  }),

  getters: {
    hasPending: (s) => s.pendingCount > 0,
    hasReady: (s) => s.readyCount > 0,
    /** Materials committed beyond what is physically free (negative available). */
    shortfalls: (s) => s.commitment.filter((c) => c.short),
  },

  actions: {
    async refresh(force = false) {
      if (this.loading) return
      this.loading = true
      this.error = ''
      try {
        const data = await api.get('/orders/summary/', force ? { _t: Date.now() } : undefined)
        this.pending = data.pending || []
        this.ready = data.ready || []
        this.commitment = data.commitment || []
        this.pendingCount = data.pending_count ?? this.pending.length
        this.readyCount = data.ready_count ?? this.ready.length
      } catch (e) {
        this.error = e.message || 'Could not load orders'
      } finally {
        this.loading = false
      }
    },

    async loadOrder(id) {
      const order = await api.get(`/orders/${id}/`)
      return order
    },

    /**
     * Create or edit. `items` = [{sku, qty_cases, selling_price_per_case}];
     * a blank price means "use the client's configured one".
     */
    async save(payload, id = null) {
      const body = {
        client: payload.client,
        order_date: payload.order_date || undefined,
        notes: payload.notes || '',
        items: payload.items,
      }
      const saved = id
        ? await api.put(`/orders/${id}/`, body)
        : await api.post('/orders/', body)
      await this.refresh(true)
      return saved
    },

    async complete(id, { force = false } = {}) {
      const order = await api.post(`/orders/${id}/complete/`, { force })
      await this.refresh(true)
      return order
    },

    async reopen(id) {
      const order = await api.post(`/orders/${id}/reopen/`)
      await this.refresh(true)
      return order
    },

    async deliver(id, { date = null, force = false, note = '' } = {}) {
      const result = await api.post(`/orders/${id}/deliver/`, {
        date: date || undefined,
        force,
        note: note || undefined,
      })
      await this.refresh(true)
      return result
    },

    async remove(id) {
      await api.del(`/orders/${id}/`)
      await this.refresh(true)
    },

    /** Other screens call this when they change stock. */
    invalidate() {
      return this.refresh(true)
    },
  },
})

export { STATUS as ORDER_STATUS }
