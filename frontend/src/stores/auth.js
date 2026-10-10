import { defineStore } from 'pinia'
import { api, ApiError } from '@/api'

/**
 * App gate (user request): the app asks for a password when it opens, lets a
 * visitor in, and records every attempt server-side.
 *
 * `unlocked` lives in sessionStorage so a reload inside the SAME tab stays
 * unlocked, while a fresh tab or a closed browser asks again. It is purely a
 * convenience — the real check, the 300-second restriction and the audit
 * trail all live in the backend (core.views.PasswordView), which is the only
 * place that knows the password.
 */
const STORAGE_KEY = 'jsd-unlocked'

export const useRefAuthStore = defineStore('auth', {
  state: () => ({
    unlocked: sessionStorage.getItem(STORAGE_KEY) === '1',
    checking: false,
    error: '',
    // Set when the server refuses because of the lockout; drives the countdown.
    lockedOut: false,
    retryAfter: 0, // seconds left, ticked down by `tick()`
    attemptsRemaining: null, // wrong tries left before the lockout
  }),
  actions: {
    /**
     * Send the password to the gate. Returns true only when the app opens.
     * A 401 shows the wrong-password message (and the tries left); a 429
     * starts the 300s countdown.
     */
    async unlock(password, path = '/') {
      this.checking = true
      this.error = ''
      try {
        await api.post('/auth/password/', { password, path })
        this.unlocked = true
        this.lockedOut = false
        this.retryAfter = 0
        this.attemptsRemaining = null
        sessionStorage.setItem(STORAGE_KEY, '1')
        return true
      } catch (e) {
        const data = e instanceof ApiError ? e.data || {} : {}
        if (e.status === 429) {
          this.lockedOut = true
          this.retryAfter = Number(data.retry_after || 300)
          this.error =
            data.detail || 'Too many wrong attempts — access is restricted.'
        } else {
          this.attemptsRemaining =
            data.attempts_remaining === undefined
              ? null
              : Number(data.attempts_remaining)
          this.error = data.detail || e.message || 'Wrong password.'
        }
        return false
      } finally {
        this.checking = false
      }
    },

    /** One countdown step — called every second while lockedOut is true. */
    tick() {
      if (this.retryAfter > 0) this.retryAfter -= 1
      if (this.retryAfter <= 0) {
        this.lockedOut = false
        this.retryAfter = 0
        this.error = ''
        this.attemptsRemaining = null
      }
    },

    /** Lock the app again (there is deliberately no "change password" UI). */
    lock() {
      this.unlocked = false
      this.error = ''
      this.attemptsRemaining = null
      sessionStorage.removeItem(STORAGE_KEY)
    },
  },
})