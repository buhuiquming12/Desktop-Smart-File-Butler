/// <reference types="vite/client" />

export {};

declare global {
  interface Window {
    desktop?: {
      platform: string;
      versions: Readonly<{
        electron: string;
        chrome: string;
        node: string;
      }>;
    };
  }
}
