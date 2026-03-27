/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    './src/pages/**/*.{js,ts,jsx,tsx,mdx}',
    './src/components/**/*.{js,ts,jsx,tsx,mdx}',
    './src/app/**/*.{js,ts,jsx,tsx,mdx}',
  ],
  theme: {
    extend: {
      fontFamily: {
        display: ['var(--font-display)', 'serif'],
        body: ['var(--font-body)', 'sans-serif'],
        mono: ['var(--font-mono)', 'monospace'],
      },
      colors: {
        ink: '#0A0A0F',
        paper: '#F5F3EE',
        accent: '#C8A951',
        'accent-dim': '#8B7235',
        dim: '#3A3A4A',
        muted: '#8888A0',
        positive: '#2ECC71',
        negative: '#E74C3C',
        neutral: '#95A5A6',
      },
    },
  },
  plugins: [],
};