import "@testing-library/jest-dom/vitest";

/**
 * `localStorage` polyfill for the jsdom test environment.
 *
 * On newer Node runtimes (Node 22+, which ships its own experimental global
 * `localStorage` gated behind `--localstorage-file`), that built-in global
 * shadows jsdom's own storage implementation, leaving `window.localStorage`
 * `undefined` under Vitest even though real browsers provide it fine. COL-33
 * introduces `frontend/src/api/client.ts`'s `getStoredApiKey`/`setStoredApiKey`,
 * the first code in this app to touch `localStorage`, so this in-memory
 * stand-in keeps their tests (and any future ones) working regardless of the
 * Node/jsdom combination running them.
 */
if (typeof globalThis.localStorage === "undefined" || typeof globalThis.localStorage?.setItem !== "function") {
  class MemoryStorage implements Storage {
    #store = new Map<string, string>();

    get length(): number {
      return this.#store.size;
    }

    clear(): void {
      this.#store.clear();
    }

    getItem(key: string): string | null {
      return this.#store.has(key) ? this.#store.get(key)! : null;
    }

    key(index: number): string | null {
      return Array.from(this.#store.keys())[index] ?? null;
    }

    removeItem(key: string): void {
      this.#store.delete(key);
    }

    setItem(key: string, value: string): void {
      this.#store.set(key, String(value));
    }
  }

  Object.defineProperty(globalThis, "localStorage", {
    value: new MemoryStorage(),
    configurable: true,
    writable: true,
  });
}
