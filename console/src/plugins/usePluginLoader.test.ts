// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import React from "react";

import { routeRegistry } from "./registry/store";
import { harnessRegistry } from "./harnesses";
import { PluginProvider, usePlugins } from "./PluginContext";
import { setAuthToken } from "../api/config";
import { notifyPluginChange } from "../utils/pluginChangeEvents";
import { removePluginRuntime } from "./pluginRuntimeCleanup";
import {
  loadAllPlugins,
  loadPawApp,
  reloadPawApp,
  resetPawAppLoaderForTests,
} from "./usePluginLoader";

const originalCreateObjectUrl = URL.createObjectURL;
const originalRevokeObjectUrl = URL.revokeObjectURL;
const originalIdleCallback = Object.getOwnPropertyDescriptor(
  window,
  "requestIdleCallback",
);

function jsonResponse(value: unknown): Response {
  return new Response(JSON.stringify(value), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function plugin(id: string, type: string) {
  return {
    id,
    name: id,
    plugin_type: type,
    frontend_entry: "dist/index.js",
  };
}

function renderPluginProvider() {
  Object.defineProperty(window, "requestIdleCallback", {
    configurable: true,
    value: (callback: () => void) => {
      queueMicrotask(callback);
      return 1;
    },
  });
  function LoadState() {
    return React.createElement(
      "div",
      {},
      usePlugins().loading ? "loading" : "ready",
    );
  }
  render(
    React.createElement(PluginProvider, {
      children: React.createElement(LoadState),
    }),
  );
}

describe("frontend plugin loader", () => {
  beforeEach(() => {
    resetPawAppLoaderForTests();
    routeRegistry.__resetForTests();
    harnessRegistry.syncCatalog([]);
    localStorage.removeItem("qwenpaw_auth_token");
    vi.restoreAllMocks();
    URL.createObjectURL = vi.fn(
      () => `data:text/javascript,${encodeURIComponent("export default true")}`,
    );
    URL.revokeObjectURL = vi.fn();
  });

  afterEach(() => {
    cleanup();
    URL.createObjectURL = originalCreateObjectUrl;
    URL.revokeObjectURL = originalRevokeObjectUrl;
    if (originalIdleCallback)
      Object.defineProperty(
        window,
        "requestIdleCallback",
        originalIdleCallback,
      );
    else Reflect.deleteProperty(window, "requestIdleCallback");
    delete (globalThis as typeof globalThis & { __registerNotes?: () => void })
      .__registerNotes;
    delete (
      globalThis as typeof globalThis & {
        __registerCustomLogin?: () => void;
      }
    ).__registerCustomLogin;
    delete (
      globalThis as typeof globalThis & {
        __registerAvailableHarness?: () => void;
      }
    ).__registerAvailableHarness;
    delete (
      globalThis as typeof globalThis & {
        __registerOrdinary?: () => void;
      }
    ).__registerOrdinary;
  });

  it("loads every installed frontend plugin during startup", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch");
    fetchMock
      .mockResolvedValueOnce(
        jsonResponse([
          plugin("global-tools", "frontend"),
          plugin("notes", "app"),
        ]),
      )
      .mockResolvedValueOnce(jsonResponse({ backends: [] }))
      .mockImplementation(async () => new Response("export default true"));

    await expect(loadAllPlugins()).resolves.toEqual({
      loaded: 2,
      failed: [],
    });
    expect(fetchMock).toHaveBeenCalledTimes(4);
    expect(fetchMock.mock.calls[1][0]).toContain("/agents/backends");
  });

  it("never fetches disabled or failed Harness scripts from disk fallback", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(async (url) => {
        const path = String(url);
        if (path.endsWith("/frontend_plugin")) {
          return jsonResponse([
            {
              ...plugin("disabled-harness", "general"),
              enabled: false,
              status: "disabled",
            },
            {
              ...plugin("failed-harness", "general"),
              enabled: true,
              status: "failed",
            },
            { ...plugin("notes", "app"), enabled: true, status: "active" },
          ]);
        }
        if (path.includes("/agents/backends")) {
          return jsonResponse({
            backends: [
              {
                id: "disabled",
                plugin_id: "disabled-harness",
                status: "disabled",
              },
              { id: "failed", plugin_id: "failed-harness", status: "failed" },
            ],
          });
        }
        return new Response("export default true");
      });

    await expect(loadAllPlugins()).resolves.toEqual({ loaded: 1, failed: [] });
    const requested = fetchMock.mock.calls.map(([url]) => String(url));
    expect(
      requested.some((url) => url.includes("disabled-harness/files")),
    ).toBe(false);
    expect(requested.some((url) => url.includes("failed-harness/files"))).toBe(
      false,
    );
    expect(requested.some((url) => url.includes("notes/files"))).toBe(true);
  });

  it("loads the skipped Harness after login without reloading an existing frontend-only plugin", async () => {
    const registerLogin = vi.fn(() =>
      routeRegistry.add("custom-login", {
        id: "custom-login.page",
        path: "/apps/login",
        component: () => null,
      }),
    );
    const registerHarness = vi.fn();
    const runtimeGlobal = globalThis as typeof globalThis & {
      __registerCustomLogin?: () => void;
      __registerAvailableHarness?: () => void;
    };
    runtimeGlobal.__registerCustomLogin = registerLogin;
    runtimeGlobal.__registerAvailableHarness = registerHarness;
    URL.createObjectURL = vi
      .fn()
      .mockReturnValueOnce(
        `data:text/javascript,${encodeURIComponent(
          "globalThis.__registerCustomLogin()",
        )}`,
      )
      .mockReturnValueOnce(
        `data:text/javascript,${encodeURIComponent(
          "globalThis.__registerAvailableHarness()",
        )}`,
      );
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(async (url, options) => {
        const path = String(url);
        if (path.endsWith("/frontend_plugin"))
          return jsonResponse([
            { ...plugin("custom-login", "frontend"), status: "active" },
            {
              ...plugin("harness-owned", "general"),
              enabled: true,
              status: "active",
              harnesses: ["third"],
            },
          ]);
        if (path.includes("/agents/backends"))
          return new Headers(options?.headers).get("Authorization") ===
            "Bearer login-token"
            ? jsonResponse({
                backends: [
                  {
                    id: "third",
                    plugin_id: "harness-owned",
                    status: "available",
                  },
                ],
              })
            : new Response("unauthorized", { status: 401 });
        if (path.includes("/files/")) return new Response("plugin script");
        return new Response(null, { status: 204 });
      });

    renderPluginProvider();
    await waitFor(() => expect(screen.getByText("ready")).toBeTruthy(), {
      timeout: 5000,
    });
    expect(registerLogin).toHaveBeenCalledOnce();
    expect(registerHarness).not.toHaveBeenCalled();
    expect(routeRegistry.snapshot()).toMatchObject([
      { id: "custom-login.page", source: "custom-login" },
    ]);

    setAuthToken("login-token");
    await waitFor(() => expect(registerHarness).toHaveBeenCalledOnce());
    expect(registerLogin).toHaveBeenCalledOnce();
    const scripts = fetchMock.mock.calls
      .map(([url]) => String(url))
      .filter((url) => url.includes("/files/"));
    expect(scripts.filter((url) => url.includes("custom-login"))).toHaveLength(
      1,
    );
    expect(scripts.filter((url) => url.includes("harness-owned"))).toHaveLength(
      1,
    );
  });

  it("finishes an ordinary frontend-only startup load when login occurs during its script fetch", async () => {
    let releaseScript!: (response: Response) => void;
    const heldScript = new Promise<Response>((resolve) => {
      releaseScript = resolve;
    });
    const register = vi.fn(() =>
      routeRegistry.add("custom-login", {
        id: "custom-login.page",
        path: "/apps/login",
        component: () => null,
      }),
    );
    (
      globalThis as typeof globalThis & { __registerOrdinary?: () => void }
    ).__registerOrdinary = register;
    URL.createObjectURL = vi.fn(
      () =>
        `data:text/javascript,${encodeURIComponent(
          "globalThis.__registerOrdinary()",
        )}`,
    );
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(async (url, options) => {
        const path = String(url);
        if (path.endsWith("/frontend_plugin"))
          return jsonResponse([
            { ...plugin("custom-login", "frontend"), status: "active" },
          ]);
        if (path.includes("/agents/backends"))
          return new Headers(options?.headers).get("Authorization") ===
            "Bearer login-token"
            ? jsonResponse({ backends: [] })
            : new Response("unauthorized", { status: 401 });
        if (path.includes("custom-login/files")) return heldScript;
        return new Response(null, { status: 204 });
      });

    renderPluginProvider();
    await waitFor(
      () =>
        expect(
          fetchMock.mock.calls.filter(([url]) =>
            String(url).includes("custom-login/files"),
          ),
        ).toHaveLength(1),
      { timeout: 5000 },
    );
    setAuthToken("login-token");
    releaseScript(new Response("plugin script"));
    await waitFor(() => expect(screen.getByText("ready")).toBeTruthy(), {
      timeout: 5000,
    });
    expect(register).toHaveBeenCalledOnce();
    expect(routeRegistry.snapshot()).toMatchObject([
      { id: "custom-login.page", source: "custom-login" },
    ]);
    expect(
      fetchMock.mock.calls.filter(([url]) =>
        String(url).includes("custom-login/files"),
      ),
    ).toHaveLength(1);
  });

  it("does not execute a pending Harness script after its plugin is disabled", async () => {
    let releaseScript!: (response: Response) => void;
    const script = new Promise<Response>((resolve) => {
      releaseScript = resolve;
    });
    const register = vi.fn();
    const runtimeGlobal = globalThis as typeof globalThis & {
      __registerStale?: () => void;
    };
    runtimeGlobal.__registerStale = register;
    URL.createObjectURL = vi.fn(
      () =>
        `data:text/javascript,${encodeURIComponent(
          "globalThis.__registerStale()",
        )}`,
    );
    let enabled = true;
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(async (url) => {
        const path = String(url);
        if (path.endsWith("/frontend_plugin")) {
          return jsonResponse([
            {
              ...plugin("owned", "general"),
              enabled,
              status: enabled ? "active" : "disabled",
              harnesses: ["third"],
            },
          ]);
        }
        if (path.includes("/agents/backends")) {
          return jsonResponse({
            backends: [
              {
                id: "third",
                plugin_id: "owned",
                status: enabled ? "available" : "disabled",
              },
            ],
          });
        }
        return script;
      });

    const loading = loadAllPlugins();
    await vi.waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([url]) =>
          String(url).includes("owned/files"),
        ),
      ).toBe(true),
    );
    enabled = false;
    notifyPluginChange();
    removePluginRuntime("owned");
    releaseScript(new Response("globalThis.__registerStale()"));
    await loading;
    expect(register).not.toHaveBeenCalled();
    delete runtimeGlobal.__registerStale;
  });

  it("loads a newly installed PawApp and exposes its route immediately", async () => {
    const runtimeGlobal = globalThis as typeof globalThis & {
      __registerNotes?: () => void;
    };
    runtimeGlobal.__registerNotes = () => {
      routeRegistry.add("notes", {
        id: "notes.page",
        path: "/apps/notes",
        component: () => null,
      });
    };
    URL.createObjectURL = vi.fn(
      () =>
        `data:text/javascript,${encodeURIComponent(
          "globalThis.__registerNotes()",
        )}`,
    );
    const fetchMock = vi.spyOn(globalThis, "fetch");
    fetchMock
      .mockResolvedValueOnce(jsonResponse([plugin("notes", "app")]))
      .mockResolvedValueOnce(jsonResponse({ backends: [] }))
      .mockResolvedValueOnce(new Response("globalThis.__registerNotes()"));

    await expect(loadPawApp("notes")).resolves.toBeUndefined();
    expect(routeRegistry.snapshot()).toMatchObject([
      { id: "notes.page", path: "/apps/notes", source: "notes" },
    ]);
  });

  it("deduplicates concurrent loads and allows retry after failure", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch");
    fetchMock
      .mockResolvedValueOnce(jsonResponse([plugin("notes", "app")]))
      .mockResolvedValueOnce(jsonResponse({ backends: [] }))
      .mockResolvedValueOnce(new Response("missing", { status: 503 }));

    const first = loadPawApp("notes");
    expect(loadPawApp("notes")).toBe(first);
    await expect(first).rejects.toThrow("HTTP 503");

    fetchMock
      .mockResolvedValueOnce(jsonResponse([plugin("notes", "app")]))
      .mockResolvedValueOnce(jsonResponse({ backends: [] }))
      .mockImplementationOnce(async () => {
        routeRegistry.add("notes", {
          id: "notes.page",
          path: "/apps/notes",
          component: () => null,
        });
        return new Response("export default true");
      });

    await expect(loadPawApp("notes")).resolves.toBeUndefined();
  });

  it("force reloads an already registered PawApp route", async () => {
    const oldComponent = () => null;
    const newComponent = () => null;
    routeRegistry.add("notes", {
      id: "notes.page",
      path: "/apps/notes",
      component: oldComponent,
    });
    const runtimeGlobal = globalThis as typeof globalThis & {
      __registerUpdatedNotes?: () => void;
    };
    runtimeGlobal.__registerUpdatedNotes = () => {
      routeRegistry.add("notes", {
        id: "notes.page",
        path: "/apps/notes",
        component: newComponent,
      });
    };
    URL.createObjectURL = vi.fn(
      () =>
        `data:text/javascript,${encodeURIComponent(
          "globalThis.__registerUpdatedNotes()",
        )}`,
    );
    const fetchMock = vi.spyOn(globalThis, "fetch");
    fetchMock
      .mockResolvedValueOnce(jsonResponse([plugin("notes", "app")]))
      .mockResolvedValueOnce(jsonResponse({ backends: [] }))
      .mockResolvedValueOnce(new Response("updated"));

    await expect(reloadPawApp("notes")).resolves.toBeUndefined();
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(routeRegistry.snapshot()[0].Component).toBe(newComponent);
    expect(routeRegistry.snapshot()[0].Component).not.toBe(oldComponent);
  });
});
