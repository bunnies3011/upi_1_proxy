/// <reference types="vitest" />
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  build: {
    // Split vendor thành các chunk riêng để cache lâu (deps ít đổi hơn app
    // code) + giảm size mỗi file dưới ngưỡng warning 500 KB. `naive-ui`
    // chiếm phần lớn bundle (~250-300KB) — tách hẳn ra để không ép browser
    // parse lại khi chỉ có `src/*` thay đổi.
    //
    // Chunk phân bổ:
    //   - `vendor-naive`: naive-ui (component library, chiếm > 50% bundle).
    //   - `vendor-icons`: @vicons/tabler (icon set — tree-shake nhưng vẫn
    //     đủ nặng do naive dùng nhiều).
    //   - `vendor-vue`: vue + pinia (core framework).
    //   - `vendor` (fallback): các dep npm còn lại.
    //   - `index` (mặc định): source code của app (src/*).
    rollupOptions: {
      output: {
        manualChunks: (id: string) => {
          if (!id.includes('node_modules')) {
            return undefined
          }
          if (id.includes('naive-ui')) {
            return 'vendor-naive'
          }
          if (id.includes('@vicons/')) {
            return 'vendor-icons'
          }
          if (
            id.includes('/vue/') ||
            id.includes('/@vue/') ||
            id.includes('/pinia/')
          ) {
            return 'vendor-vue'
          }
          return 'vendor'
        },
      },
    },
    // Nâng ngưỡng cảnh báo lên 600 KB — sau khi split, chunk lớn nhất
    // (`vendor-naive`) vẫn quanh 400-500 KB do component library, chấp
    // nhận được cho SPA local (không phải web mobile). Warning cũ 500 KB
    // là default cho web app public.
    chunkSizeWarningLimit: 600,
  },
  server: {
    // Proxy tất cả `/api/*` sang FastAPI backend (:8989) trong dev.
    // Không có proxy → gọi `/api/events/stream` sẽ hit Vite dev server và
    // trả 404, khiến login gate không thoát và SSE reconnect loop.
    // Trong production, frontend sẽ được backend serve cùng origin nên
    // không cần proxy này.
    proxy: {
      '/api': {
        // Backend port có thể override qua env VITE_BACKEND_PORT khi dev
        // song song với app khác đang chiếm port 8989 (mặc định). Trong
        // production, frontend được backend serve cùng origin nên proxy
        // này không có tác dụng.
        target: `http://127.0.0.1:${process.env.VITE_BACKEND_PORT || 8989}`,
        changeOrigin: true,
        // SSE stream cần giữ connection alive — vite proxy hỗ trợ mặc định
        // qua http-proxy, không cần `ws: true` (SSE là HTTP long-poll, không
        // phải WebSocket). Chỉ tắt buffering để event flush ngay.
      },
    },
  },
  test: {
    // Snapshot/component tests cần DOM (mount, localStorage, window.matchMedia).
    // Property tests thuần pure function không dùng DOM cũng chạy được trên
    // jsdom mà không phải mất tiền chi phí đáng kể — dùng chung env cho gọn.
    environment: 'jsdom',
    setupFiles: ['./src/__tests__/setup.ts'],
    // Timeout tiêu chuẩn — dài đủ để property tests chạy, ngắn đủ để CI
    // không treo khi có bug logic tạo vòng lặp vô hạn.
    testTimeout: 15000,
    hookTimeout: 5000,
    teardownTimeout: 3000,
    // `forks` với singleFork + isolate=false thoát process nhanh hơn
    // `threads` khi test sinh AbortController/fetch stream (Node 22 undici
    // giữ keep-alive agent khiến `threads` không giải phóng worker).
    pool: 'forks',
    poolOptions: {
      forks: {
        singleFork: true,
      },
    },
  },
})
