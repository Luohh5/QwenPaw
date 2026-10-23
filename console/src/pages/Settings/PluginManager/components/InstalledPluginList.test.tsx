// @vitest-environment jsdom
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { PluginInfo } from "@/api/modules/plugin";
import { InstalledPluginList } from "./InstalledPluginList";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string) =>
      (
        ({
          "pluginManager.statusLoaded": "Running",
          "pluginManager.statusUnloaded": "Not loaded",
          "pluginManager.statusError": "Error",
        }) as Record<string, string>
      )[key] ?? key,
  }),
}));

vi.mock("@/hooks/useIsMobile", () => ({ useIsMobile: () => false }));

const props = {
  loading: false,
  uninstallingId: null,
  onRefresh: vi.fn(),
  onUninstall: vi.fn(),
  updates: new Map(),
  updatesLoading: false,
  updatingId: null,
  updatingAll: false,
  onUpdate: vi.fn(),
  onUpdateAll: vi.fn(),
  enablingId: null,
  onEnable: vi.fn(),
  onRepair: vi.fn(),
  onDisable: vi.fn(),
};

function plugin(loaded: boolean, status?: string): PluginInfo {
  return {
    id: "demo",
    name: "Demo",
    version: "1.0.0",
    description: "",
    author: "",
    loaded,
    plugin_type: "general",
    enabled: true,
    ...(status === undefined ? {} : { status }),
  };
}

function expectTags(text: string, color: string, count: number) {
  const labels = screen.getAllByText(text);
  expect(labels).toHaveLength(count);
  for (const label of labels) {
    expect(label.closest(".ant-tag")).toHaveClass(`ant-tag-${color}`);
  }
}

describe("installed plugin status", () => {
  it.each([
    [true, "failed", "Error", "error"],
    [false, "failed", "Error", "error"],
    [true, "active", "Running", "success"],
    [true, undefined, "Running", "success"],
    [false, undefined, "Not loaded", "default"],
  ] as const)(
    "renders loaded=%s status=%s consistently in all views",
    (loaded, status, text, color) => {
      render(
        <InstalledPluginList {...props} plugins={[plugin(loaded, status)]} />,
      );
      expectTags(text, color, 1);
      if (status === "failed") {
        expect(screen.queryByText("Running")).not.toBeInTheDocument();
        expect(screen.queryByText("Not loaded")).not.toBeInTheDocument();
      }
      fireEvent.click(screen.getByLabelText("skills.listView"));
      // The desktop table and its responsive row both render real status tags.
      expectTags(text, color, 2);
    },
  );

  it("clears the red error tag when refreshed data reports active", () => {
    const { rerender } = render(
      <InstalledPluginList {...props} plugins={[plugin(true, "failed")]} />,
    );
    expectTags("Error", "error", 1);
    fireEvent.click(screen.getByLabelText("pluginManager.catalogRefresh"));
    expect(props.onRefresh).toHaveBeenCalledOnce();
    rerender(
      <InstalledPluginList {...props} plugins={[plugin(true, "active")]} />,
    );
    expectTags("Running", "success", 1);
    expect(screen.queryByText("Error")).not.toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("skills.listView"));
    expectTags("Running", "success", 2);
    expect(screen.queryByText("Error")).not.toBeInTheDocument();
  });

  it.each([
    [false, false, "inactive", "common.enable"],
    [true, true, "failed", "pluginManager.repair"],
  ] as const)(
    "offers enabled=%s loaded=%s status=%s action %s in both views",
    (enabled, loaded, status, label) => {
      props.onEnable.mockClear();
      props.onRepair.mockClear();
      const action = status === "failed" ? props.onRepair : props.onEnable;
      const other = status === "failed" ? props.onEnable : props.onRepair;
      const target = { ...plugin(loaded, status), enabled };
      render(<InstalledPluginList {...props} plugins={[target]} />);
      fireEvent.click(screen.getByRole("button", { name: label }));
      expect(action).toHaveBeenCalledWith(target);
      expect(other).not.toHaveBeenCalled();

      fireEvent.click(screen.getByLabelText("skills.listView"));
      const actions = screen.getAllByRole("button", {
        name: label,
        hidden: true,
      });
      expect(actions).toHaveLength(2);
      fireEvent.click(actions[0]);
      expect(action).toHaveBeenCalledTimes(2);
    },
  );

  it("offers Disable for an active Harness in both installed views", () => {
    props.onDisable.mockClear();
    const target = plugin(true, "active");
    render(<InstalledPluginList {...props} plugins={[target]} />);
    fireEvent.click(screen.getByRole("button", { name: "common.disable" }));
    expect(props.onDisable).toHaveBeenCalledWith(target);

    fireEvent.click(screen.getByLabelText("skills.listView"));
    const actions = screen.getAllByRole("button", {
      name: "common.disable",
      hidden: true,
    });
    expect(actions).toHaveLength(2);
    fireEvent.click(actions[0]);
    expect(props.onDisable).toHaveBeenCalledTimes(2);
  });
});
