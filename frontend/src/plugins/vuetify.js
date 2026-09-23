import 'vuetify/styles'
import { createVuetify } from 'vuetify'

// High-contrast palette for outdoor/field phone use (spec Section 7):
// deep blue brand, green success, amber/red warnings.
export default createVuetify({
  theme: {
    defaultTheme: 'jsd',
    themes: {
      jsd: {
        dark: false,
        colors: {
          primary: '#0d47a1',
          secondary: '#00838f',
          success: '#1b8a3a',
          warning: '#e65100',
          error: '#c62828',
          background: '#f4f6f8',
        },
      },
    },
  },
  defaults: {
    VBtn: { rounded: 'lg' },
    VCard: { rounded: 'lg', elevation: 1 },
    VTextField: { variant: 'outlined', density: 'comfortable' },
    VSelect: { variant: 'outlined', density: 'comfortable' },
  },
})
