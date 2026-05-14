const envBase = import.meta.env.VITE_API_BASE_URL;
const currentHost = typeof window === "undefined" ? "" : window.location.origin;
const localBackendFallback = "http://localhost:4000";
const apiOrigin = envBase || (import.meta.env.DEV ? localBackendFallback : currentHost || localBackendFallback);

export const APP_URL = apiOrigin;

export const API_URL = apiOrigin.replace(/\/$/, "") + "/trpc";

export const REST_API_BASE = apiOrigin.replace(/\/$/, "");

export const AUTH_URL = apiOrigin;

// Environment helpers
export const ENV = {
    isDevelopment: import.meta.env.MODE === "development",
    isStaging: import.meta.env.MODE === "staging",
    isProduction: import.meta.env.MODE === "production",
    current: import.meta.env.MODE || "development",
};
