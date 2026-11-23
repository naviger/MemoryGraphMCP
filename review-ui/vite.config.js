import { defineConfig } from 'vite';

export default defineConfig({
  esbuild: { jsx: 'automatic' },
  server: {
    proxy: {
      '/api/review': process.env.REVIEW_API_URL || 'http://127.0.0.1:3100',
    },
  },
});
