/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      fontFamily: {
        sans: ['Outfit', 'system-ui', 'sans-serif'],
        display: ['Newsreader', 'Georgia', 'serif'],
        mono: ['"JetBrains Mono"', 'Consolas', 'monospace'],
      },
      boxShadow: {
        glow: '0 0 24px rgba(245, 158, 11, 0.18)',
        'glow-lg': '0 0 48px rgba(245, 158, 11, 0.25)',
      },
    },
  },
  plugins: [],
}
