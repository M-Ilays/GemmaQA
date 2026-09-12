/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_BASE_URL?: string;
  readonly VITE_WS_BASE_URL?: string;
  /** @deprecated use VITE_API_BASE_URL */
  readonly VITE_API_BASE?: string;
  /** @deprecated use VITE_WS_BASE_URL */
  readonly VITE_WS_HOST?: string;
  readonly VITE_DEMO_MODE?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
