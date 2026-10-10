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

    /**
     * Pending -> Ready to Deliver. `items` = [{item, qty_cases}] moves only
     * those cases (user request: move part of a pending order); omitting it
     * moves the whole order. The backend splits the order when only part of it
     * moves, and answers with `{ order, split, partial }`.
     */
    async complete(id, { force = false, items = null } = {}) {
      const order = await api.post(`/orders/${id}/complete/`, {
        force,
        items: items || undefined,
      })
      await this.refresh(true)
      return order
    },

    /** Ready to Deliver -> Pending, with the same optional partial selection. */
    async reopen(id, { items = null } = {}) {
      const order = await api.post(`/orders/${id}/reopen/`, {
        items: items || undefined,
      })
      await this.refresh(true)
      return order
    },

    /**
     * Deliver a ready order. `payment` ({amount, date?, note?}) is optional and
     * rides in the same request, so the delivery and the money are saved
     * together or not at all (user request). Both totals and the client's
     * pending balance come back from the server.
     *
     * `items` = [{item, qty_cases}] sends only those cases (partial delivery):
     * the rest stays on the order in Ready to Deliver and `partial` comes back
     * true. Omitted = the whole order goes out at once, as before.
     */
    async deliver(
      id,
      { date = null, force = false, note = '', payment = null, items = null } = {}
    ) {
      const result = await api.post(`/orders/${id}/deliver/`, {
        date: date || undefined,
        force,
        note: note || undefined,
        payment: payment || undefined,
        items: items || undefined,
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
