/** @type {import('tailwindcss').Config} */
export default {
  darkMode: 'class',
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  theme: {
    extend: {
      colors: {
        theme: {
          base: 'rgb(var(--theme-base) / <alpha-value>)',
          surface: 'rgb(var(--theme-surface) / <alpha-value>)',
          border: 'rgb(var(--theme-border) / <alpha-value>)',
          text: 'rgb(var(--theme-text) / <alpha-value>)',
          muted: 'rgb(var(--theme-muted) / <alpha-value>)',
          accent: 'rgb(var(--theme-accent) / <alpha-value>)',
          'accent-foreground': 'rgb(var(--theme-accent-foreground) / <alpha-value>)',
          'accent-bg': 'rgb(var(--theme-accent-bg) / <alpha-value>)',
        },
      },
      animation: {
        'pulse-glow': 'pulse-glow 2s ease-in-out infinite',
        'token-flash': 'token-flash 0.55s ease-out',
        'status-pulse': 'status-pulse 1.8s ease-in-out infinite',
      },
      keyframes: {
        'pulse-glow': {
          '0%, 100%': { boxShadow: '0 0 8px rgb(16 185 129 / 0.35)' },
          '50%': { boxShadow: '0 0 22px rgb(16 185 129 / 0.75)' },
        },
        'token-flash': {
          '0%': { color: '#10b981', textShadow: '0 0 12px rgb(16 185 129 / 0.9)' },
          '100%': { color: '#10b981', textShadow: '0 0 4px rgb(16 185 129 / 0.4)' },
        },
        'status-pulse': {
          '0%, 100%': { opacity: '1' },
          '50%': { opacity: '0.65' },
        },
      },
      fontFamily: {
        sans: ['Inter', 'Arial', 'sans-serif'],
        mono: ['"JetBrains Mono"', 'ui-monospace', 'SFMono-Regular', 'monospace'],
      },
    },
  },
  plugins: [],
}
