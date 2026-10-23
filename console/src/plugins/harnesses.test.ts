// @vitest-environment jsdom
import React from "react";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@agentscope-ai/design", async () => vi.importActual("antd"));
import {
  HarnessSettings,
  harnessNamespace,
  harnessRegistry,
  refreshHarnessCatalog,
  type HarnessSettingsProps,
} from "./harnesses";
import { installHostSdk } from "./hostSdk/install";
import { removePluginRuntime } from "./pluginRuntimeCleanup";

const owners = ["first-owner", "second-owner", "forged-owner"];
const originalHost = Object.getOwnPropertyDescriptor(window, "QwenPaw");
function descriptor(
  id = "third-one",
  pluginId = "first-owner",
  status: "available" | "disabled" | "failed" = "available",
) {
  return {
    id,
    label: id,
    plugin_id: pluginId,
    status,
    capabilities: {},
    config_fields: [],
  };
}
beforeEach(() => {
  owners.forEach((owner) => harnessRegistry.disposeAll(owner));
  harnessRegistry.syncCatalog([]);
});
afterEach(() => {
  cleanup();
  owners.forEach(removePluginRuntime);
  vi.restoreAllMocks();
  if (originalHost) Object.defineProperty(window, "QwenPaw", originalHost);
  else Reflect.deleteProperty(window, "QwenPaw");
});

describe("harness frontend ownership", () => {
  it("keeps early registrations inert and uses authoritative ownership", () => {
    harnessRegistry.register("forged-owner", {
      backendId: "third-one",
      SettingsComponent: () => null,
    });
    expect(harnessRegistry.getSettings("third-one")).toBeUndefined();
    harnessRegistry.syncCatalog([descriptor()]);
    expect(harnessRegistry.getSettings("third-one")).toBeUndefined();
    const component = () => null;
    harnessNamespace.register("first-owner", {
      backendId: "third-one",
      SettingsComponent: component,
    });
    expect(harnessRegistry.getSettings("third-one")?.SettingsComponent).toBe(
      component,
    );
    expect(() =>
      harnessRegistry.register("forged-owner", { backendId: "third-one" }),
    ).toThrow();
    harnessRegistry.syncCatalog([
      descriptor("third-one", "first-owner", "disabled"),
    ]);
    expect(harnessRegistry.getSettings("third-one")).toBeUndefined();
  });

  it("rejects reserved and duplicate IDs and ignores stale disposal", () => {
    harnessRegistry.syncCatalog([descriptor()]);
    expect(() =>
      harnessRegistry.register("first-owner", { backendId: "qwenpaw" }),
    ).toThrow();
    const old = harnessNamespace.register("first-owner", {
      backendId: "third-one",
    });
    expect(() =>
      harnessRegistry.register("first-owner", { backendId: "third-one" }),
    ).toThrow();
    harnessRegistry.disposeAll("first-owner");
    const replacement = () => null;
    harnessRegistry.register("first-owner", {
      backendId: "third-one",
      SettingsComponent: replacement,
    });
    old.dispose();
    expect(harnessRegistry.getSettings("third-one")?.SettingsComponent).toBe(
      replacement,
    );
  });

  it("installs the public namespace and cleans up through existing runtime removal", () => {
    harnessRegistry.syncCatalog([descriptor()]);
    installHostSdk();
    expect(window.QwenPaw?.harnesses).toBe(harnessNamespace);
    harnessNamespace.register("first-owner", { backendId: "third-one" });
    expect(harnessRegistry.getSettings("third-one")).toBeDefined();
    removePluginRuntime("first-owner");
    expect(harnessRegistry.getSettings("third-one")).toBeUndefined();
  });

  it("hides stale settings after a failed catalog refresh and restores them on recovery", async () => {
    harnessRegistry.syncCatalog([descriptor()]);
    harnessRegistry.register("first-owner", {
      backendId: "third-one",
      SettingsComponent: () => React.createElement("div", {}, "live settings"),
    });
    render(
      React.createElement(HarnessSettings, {
        backendId: "third-one",
        value: {},
        onChange: vi.fn(),
        open: true,
      }),
    );
    expect(screen.getByText("live settings")).toBeInTheDocument();

    vi.spyOn(globalThis, "fetch")
      .mockRejectedValueOnce(new Error("catalog offline"))
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ backends: [descriptor()] }), {
          headers: { "Content-Type": "application/json" },
        }),
      );
    await act(async () => {
      await expect(refreshHarnessCatalog()).rejects.toThrow("catalog offline");
    });
    expect(screen.queryByText("live settings")).toBeNull();

    await act(refreshHarnessCatalog);
    expect(screen.getByText("live settings")).toBeInTheDocument();
  });
});

describe("selected harness settings", () => {
  it("renders only the selected owner's component with exact public props", () => {
    harnessRegistry.syncCatalog([
      descriptor(),
      descriptor("third-two", "second-owner"),
    ]);
    const first = vi.fn(() => React.createElement("div", {}, "first settings"));
    const second = vi.fn((props: HarnessSettingsProps) =>
      React.createElement(
        "button",
        { onClick: () => props.onChange({ ...props.value, model: "changed" }) },
        "second settings",
      ),
    );
    harnessRegistry.register("first-owner", {
      backendId: "third-one",
      SettingsComponent: first,
    });
    harnessRegistry.register("second-owner", {
      backendId: "third-two",
      SettingsComponent: second,
    });
    const onChange = vi.fn();
    const value = { model: "original", preserved: true };
    render(
      React.createElement(HarnessSettings, {
        backendId: "third-two",
        value,
        onChange,
        open: true,
      }),
    );
    expect(screen.queryByText("first settings")).toBeNull();
    expect(screen.getByText("second settings")).toBeInTheDocument();
    expect(first).not.toHaveBeenCalled();
    expect(second.mock.calls[second.mock.calls.length - 1]?.[0]).toEqual({
      backendId: "third-two",
      value,
      onChange,
      open: true,
    });
    fireEvent.click(screen.getByText("second settings"));
    expect(onChange).toHaveBeenCalledWith({
      model: "changed",
      preserved: true,
    });
    act(() =>
      harnessRegistry.syncCatalog([
        descriptor("third-two", "second-owner", "failed"),
      ]),
    );
    expect(screen.queryByText("second settings")).toBeNull();
  });

  it("uses the shared config schema when a frontend component is absent", () => {
    const fields = [
      {
        name: "model",
        label: "Model name",
        type: "text" as const,
        required: false,
      },
    ];
    const nextFields = [
      {
        name: "endpoint",
        label: "Endpoint",
        type: "text" as const,
        required: false,
      },
    ];
    harnessRegistry.syncCatalog([
      { ...descriptor(), config_fields: fields },
      { ...descriptor("third-two", "second-owner"), config_fields: nextFields },
    ]);
    const onChange = vi.fn();
    const { rerender } = render(
      React.createElement(HarnessSettings, {
        backendId: "third-one",
        value: { model: "original", preserved: true },
        onChange,
        open: true,
      }),
    );
    expect(screen.getByText("Model name")).toBeInTheDocument();
    fireEvent.change(screen.getByDisplayValue("original"), {
      target: { value: "changed" },
    });
    expect(onChange).toHaveBeenCalledWith({
      model: "changed",
      preserved: true,
    });

    onChange.mockClear();
    rerender(
      React.createElement(HarnessSettings, {
        backendId: "third-two",
        value: { endpoint: "initial", preserved: true },
        onChange,
        open: true,
      }),
    );
    fireEvent.change(screen.getByDisplayValue("initial"), {
      target: { value: "updated" },
    });
    expect(onChange).toHaveBeenCalledWith({
      endpoint: "updated",
      preserved: true,
    });
  });
});
