// @vitest-environment jsdom
import React from "react";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { Form } from "antd";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  harnessRegistry,
  type HarnessBackendDescriptor,
  type HarnessSettingsProps,
} from "@/plugins/harnesses";
import { renderWithProviders } from "@/test/common_setup";
import { AgentBackendFields } from "./AgentBackendFields";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

const owners = ["first-owner", "second-owner"];
let rows: HarnessBackendDescriptor[] = [];
let currentForm: ReturnType<typeof Form.useForm>[0];

function descriptor(
  id: string,
  pluginId: string,
  status: HarnessBackendDescriptor["status"] = "available",
): HarnessBackendDescriptor {
  return {
    id,
    label: id,
    plugin_id: pluginId,
    status,
    capabilities: {},
    config_fields: [],
  };
}

function BackendForm({
  backend = "qwenpaw",
  settings = {},
}: {
  backend?: string;
  settings?: Record<string, unknown>;
}) {
  const [form] = Form.useForm();
  currentForm = form;
  return (
    <Form form={form} initialValues={{ backend, backend_settings: settings }}>
      <AgentBackendFields form={form} open />
    </Form>
  );
}

describe("AgentBackendFields", () => {
  beforeEach(() => {
    rows = [];
    harnessRegistry.syncCatalog([]);
    owners.forEach((owner) => harnessRegistry.disposeAll(owner));
    vi.spyOn(globalThis, "fetch").mockImplementation(
      async () =>
        new Response(JSON.stringify({ backends: rows }), {
          headers: { "Content-Type": "application/json" },
        }),
    );
  });

  afterEach(() => {
    owners.forEach((owner) => harnessRegistry.disposeAll(owner));
    harnessRegistry.syncCatalog([]);
    vi.restoreAllMocks();
  });

  it("offers native creation and installation without probing absent vendors", async () => {
    renderWithProviders(<BackendForm />);
    fireEvent.click(screen.getByText("agent.backend.thirdPartyTitle"));

    expect(screen.getByText("agent.backend.nativeTitle")).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "agent.backend.installPlugin" }),
    ).toHaveAttribute("href", expect.stringContaining("/market"));
    expect(screen.queryByText("Codex")).not.toBeInTheDocument();
    expect(screen.queryByText("Qoder")).not.toBeInTheDocument();
    await waitFor(() => expect(globalThis.fetch).toHaveBeenCalled());
    expect(
      vi
        .mocked(globalThis.fetch)
        .mock.calls.every(([url]) => !String(url).includes("/harnesses/")),
    ).toBe(true);
    expect(currentForm.getFieldValue("backend")).toBe("qwenpaw");
  });

  it.each(["missing", "disabled", "failed"] as const)(
    "keeps the identity and settings of a saved %s backend",
    async (state) => {
      if (state !== "missing") {
        rows = [descriptor("retired", "first-owner", state)];
        harnessRegistry.syncCatalog(rows);
      }
      const settings = { model: "saved-model", binary: "/saved/path" };
      renderWithProviders(
        <BackendForm backend="retired" settings={settings} />,
      );

      expect(await screen.findByRole("status")).toHaveTextContent("retired");
      expect(screen.getByRole("status")).toHaveTextContent(
        `agent.backend.states.${state}`,
      );
      expect(currentForm.getFieldValue("backend")).toBe("retired");
      expect(currentForm.getFieldValue("backend_settings")).toEqual(settings);
    },
  );

  it("renders and writes settings only for the selected active owner", async () => {
    rows = [
      descriptor("first", "first-owner"),
      descriptor("second", "second-owner"),
    ];
    harnessRegistry.syncCatalog(rows);
    const first = vi.fn(() => React.createElement("p", {}, "first settings"));
    const second = vi.fn((props: HarnessSettingsProps) =>
      React.createElement(
        "button",
        {
          onClick: () =>
            props.onChange({ ...props.value, model: "second-model" }),
        },
        "second settings",
      ),
    );
    harnessRegistry.register("first-owner", {
      backendId: "first",
      SettingsComponent: first,
    });
    harnessRegistry.register("second-owner", {
      backendId: "second",
      SettingsComponent: second,
    });

    renderWithProviders(
      <BackendForm backend="second" settings={{ preserved: true }} />,
    );
    fireEvent.click(await screen.findByText("second settings"));
    expect(screen.queryByText("first settings")).not.toBeInTheDocument();
    expect(first).not.toHaveBeenCalled();
    expect(currentForm.getFieldValue("backend_settings")).toEqual({
      preserved: true,
      model: "second-model",
    });
  });
});
