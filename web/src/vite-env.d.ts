/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Base URL of the local paper/research API. Defaults to http://127.0.0.1:8177 */
  readonly VITE_API_BASE?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
