import { defineStore } from 'pinia'
import { api, listify } from '@/api'

/**
 * Shared reference-data cache (perf plan 2.8).
 *
 * Clients, SKUs, materials and vendors are read on almost every screen — the
 * Add Delivery picker, Enter Arrived Stock, Stock Adjustment, Reports, Client
 * form and the Masters lists all pull the same rows. Fetching them again on
 * every navigation meant a fresh API round trip (and a Render cold-start wait)
 * for data that barely changes, so they live here instead: the first screen to
 * need a list fetches it once, and every later screen reads the cache and
 * refreshes in the background (stale-while-revalidate).
 */
const ENDPOINTS = {
  clients: '/clients/',
  skus: '/skus/',
  materials: '/materials/',
  vendors: '/vendors/',
}

// One in-flight request per list, shared by concurrent callers. Held outside
// the store so it is never made reactive or serialised by the devtools.
const inflight = {}

export const useRefDataStore = defineStore('refdata', {
  state: () => ({
    clients: [],
    skus: [],
    materials: [],
    vendors: [],
    loaded: {}, // key -> true once a fetch has resolved
  }),
  actions: {
    /**
     * Return a reference list, fetching it only when it is not cached yet (or
     * `force` is set). Concurrent calls for the same key share one request.
     */
    async fetch(key, { force = false } = {}) {
      if (this.loaded[key] && !force) return this[key]
      if (inflight[key]) return inflight[key]
      const req = (async () => {
        try {
          const data = listify(await api.get(ENDPOINTS[key]))
          this[key] = data
          this.loaded[key] = true
          return data
        } finally {
          delete inflight[key]
        }
      })()
      inflight[key] = req
      return req
    },

    /**
     * Stale-while-revalidate helper: resolve now with whatever is cached (so a
     * screen paints instantly) and refresh in the background. Callers that need
     * the newest rows can await the returned `fresh` promise.
     */
    swr(key) {
      const cached = this[key]
      return {
        cached,
        // Force a refresh only when we already hold data; a cold miss just
        // fetches normally.
        fresh: this.fetch(key, { force: !!this.loaded[key] }),
      }
    },

    /**
     * Forget one list (or all of them) so the next read refetches — call this
     * after a mutation so a screen elsewhere does not show stale rows.
     */
    invalidate(...keys) {
      for (const k of keys.length ? keys : Object.keys(ENDPOINTS)) {
        this.loaded[k] = false
      }
    },
  },
})
