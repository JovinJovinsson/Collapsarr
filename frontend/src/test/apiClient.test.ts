import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { apiErrorMessage, apiFetch, getStoredApiKey, setStoredApiKey } from "../api/client";

describe("apiFetch / stored API key", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    localStorage.clear();
  });

  it("returns an empty string when no key is stored", () => {
    expect(getStoredApiKey()).toBe("");
  });

  it("persists a key via setStoredApiKey and reads it back", () => {
    setStoredApiKey("abc123");
    expect(getStoredApiKey()).toBe("abc123");
  });

  it("clears the stored key when set to an empty string", () => {
    setStoredApiKey("abc123");
    setStoredApiKey("");
    expect(getStoredApiKey()).toBe("");
  });

  it("attaches X-Api-Key to outgoing requests when a key is stored", async () => {
    setStoredApiKey("secret-key");
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 200, json: () => Promise.resolve({}) });
    vi.stubGlobal("fetch", fetchMock);

    await apiFetch("/api/wanted");

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const headers = new Headers(init.headers);
    expect(headers.get("X-Api-Key")).toBe("secret-key");
  });

  it("omits X-Api-Key when no key is stored", async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 200, json: () => Promise.resolve({}) });
    vi.stubGlobal("fetch", fetchMock);

    await apiFetch("/api/wanted");

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const headers = new Headers(init.headers);
    expect(headers.get("X-Api-Key")).toBeNull();
  });
});

describe("apiErrorMessage", () => {
  it("extracts a string `detail` field", async () => {
    const response = new Response(JSON.stringify({ detail: "No arr instance with id=1" }), { status: 404 });
    expect(await apiErrorMessage(response, "fallback")).toBe("No arr instance with id=1");
  });

  it("joins Pydantic validation-error messages", async () => {
    const response = new Response(
      JSON.stringify({ detail: [{ msg: "field required" }, { msg: "value is not a valid url" }] }),
      { status: 422 },
    );
    expect(await apiErrorMessage(response, "fallback")).toBe("field required; value is not a valid url");
  });

  it("falls back when the body has no usable detail", async () => {
    const response = new Response(JSON.stringify({}), { status: 500 });
    expect(await apiErrorMessage(response, "fallback")).toBe("fallback");
  });

  it("falls back when the body isn't JSON", async () => {
    const response = new Response("not json", { status: 500 });
    expect(await apiErrorMessage(response, "fallback")).toBe("fallback");
  });
});
