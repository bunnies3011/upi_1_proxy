/// <reference types="vite/client" />

declare module '*.vue' {
  import type { DefineComponent } from 'vue'
  const component: DefineComponent<{}, {}, unknown>
  export default component
}

/**
 * Env vars có prefix `VITE_` được Vite expose vào `import.meta.env` (client
 * bundle). Khai báo ở đây để TypeScript compile-check.
 */
interface ImportMetaEnv {
  readonly [key: string]: string | boolean | undefined
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
