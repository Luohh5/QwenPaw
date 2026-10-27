// @vitest-environment jsdom
// Cross-plugin frontend integration: render the actual public-SDK components
// with one host fixture so vendor account behavior stays outside core UI.
import React from "react";
import { Button, Input, Tooltip } from "antd";
import * as antdIcons from "@ant-design/icons";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import {
  afterAll,
  afterEach,
  beforeAll,
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from "vitest";

const mock = vi.hoisted(() => ({
  fetch: vi.fn(),
  success: vi.fn(),
  error: vi.fn(),
}));

type SettingsComponent = React.ComponentType<{
  backendId: string;
  value: Record<string, unknown>;
  onChange: (value: Record<string, unknown>) => void;
  open: boolean;
}>;
let CodexSettings: SettingsComponent;
let QoderSettings: SettingsComponent;

beforeAll(async () => {
  Object.defineProperty(window, "QwenPaw", {
    configurable: true,
    value: {
      host: {
        React,
        antdIcons,
        antd: {
          Button,
          Input,
          Tooltip,
          App: { useApp: () => ({ message: mock }) },
        },
        fetch: mock.fetch,
        useLocale: () => "en",
      },
    },
  });
  ({ Settings: CodexSettings } = await import(
    "../../../plugins/harness/codex/frontend/src/Settings"
  ));
  ({ Settings: QoderSettings } = await import(
    "../../../plugins/harness/qoder/frontend/src/Settings"
  ));
});

afterAll(() => {
  Reflect.deleteProperty(window, "QwenPaw");
});

function response(value: unknown) {
  return new Response(JSON.stringify(value), {
    headers: { "Content-Type": "application/json" },
  });
}

function Mounted({ Component }: { Component: SettingsComponent }) {
  const [value, setValue] = React.useState<Record<string, unknown>>({});
  return <Component backendId="test" value={value} onChange={setValue} open />;
}

describe("vendor-owned Harness Settings", () => {
  beforeEach(() => {
    mock.fetch.mockReset();
    mock.success.mockReset();
    mock.error.mockReset();
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it("keeps Codex API-key identity and custom executable checks inside its plugin", async () => {
    mock.fetch.mockResolvedValue(
      response({
        installed: true,
        authenticated: true,
        account: { type: "apiKey" },
        runtime_path: "/opt/codex",
        capabilities: {
          qwenpaw_skills_projection: true,
          mcp_tool_allowlist: true,
        },
      }),
    );
    render(<Mounted Component={CodexSettings} />);

    expect(
      await screen.findByText(/Authenticated with API key/),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Connect with ChatGPT" }),
    ).toBeNull();
    expect(screen.getByText("/opt/codex")).toHaveProperty("tagName", "CODE");
    fireEvent.change(
      screen.getByRole("textbox", { name: /Codex executable/ }),
      {
        target: { value: "/custom/bin/codex" },
      },
    );
    fireEvent.click(screen.getByText("Detect"));
    await waitFor(() =>
      expect(mock.fetch).toHaveBeenLastCalledWith(
        "/harnesses/codex/status",
        expect.objectContaining({
          body: expect.stringContaining("/custom/bin/codex"),
        }),
      ),
    );
    const copy = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: copy },
    });
    fireEvent.click(screen.getByRole("button", { name: "Copy" }));
    await waitFor(() => expect(copy).toHaveBeenCalledWith("/opt/codex"));
  });

  it("shows the missing Codex runtime without offering login", async () => {
    mock.fetch.mockResolvedValue(
      response({
        installed: false,
        authenticated: false,
        account: null,
        runtime_path: null,
      }),
    );
    render(<Mounted Component={CodexSettings} />);
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Codex runtime not found",
    );
    expect(
      screen.getByRole("button", { name: "Connect with ChatGPT" }),
    ).toBeDisabled();
  });

  it("keeps Qoder CLI login and account identity inside its plugin", async () => {
    mock.fetch.mockImplementation(async (path: string) => {
      if (path.endsWith("/login"))
        return response({ type: "external", loginId: "local" });
      return response({ installed: true, authenticated: false, account: null });
    });
    render(<Mounted Component={QoderSettings} />);
    fireEvent.change(
      screen.getByRole("textbox", { name: /Qoder executable/ }),
      {
        target: { value: "/custom/qodercli" },
      },
    );
    fireEvent.click(
      await screen.findByRole("button", { name: "Connect to Qoder" }),
    );
    await waitFor(() =>
      expect(mock.fetch).toHaveBeenCalledWith(
        "/harnesses/qoder/login",
        expect.objectContaining({
          body: expect.stringContaining("/custom/qodercli"),
        }),
      ),
    );
  });

  it("shows an authenticated Qoder CLI account without a login action", async () => {
    mock.fetch.mockResolvedValue(
      response({
        installed: true,
        authenticated: true,
        account: { type: "qodercli", username: "qoder-user" },
        runtime_path: "/opt/qoder/qodercli",
      }),
    );
    render(<Mounted Component={QoderSettings} />);
    expect(
      await screen.findByText(/Qoder CLI authenticated · qoder-user/),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Connect to Qoder" }),
    ).toBeNull();
  });

  it("waits for a Qoder login status response before scheduling another poll", async () => {
    let statusCalls = 0;
    mock.fetch.mockImplementation(async (path: string) => {
      if (path.endsWith("/login"))
        return response({ type: "external", loginId: "local" });
      statusCalls += 1;
      if (statusCalls > 1) return new Promise<Response>(() => undefined);
      return response({ installed: true, authenticated: false, account: null });
    });
    const view = render(<Mounted Component={QoderSettings} />);
    const connect = await screen.findByRole("button", {
      name: "Connect to Qoder",
    });
    vi.useFakeTimers();
    await act(async () => {
      fireEvent.click(connect);
      await Promise.resolve();
    });
    expect(mock.fetch).toHaveBeenCalledWith(
      "/harnesses/qoder/login",
      expect.anything(),
    );
    await act(async () => {
      vi.advanceTimersByTime(2_000);
    });
    expect(statusCalls).toBe(2);
    await act(async () => {
      vi.advanceTimersByTime(10_000);
    });
    expect(statusCalls).toBe(2);
    view.unmount();
  });

  it("does not resume Qoder polling when a pending login finishes after unmount", async () => {
    let releaseLogin!: (value: Response) => void;
    const login = new Promise<Response>((resolve) => {
      releaseLogin = resolve;
    });
    let statusCalls = 0;
    mock.fetch.mockImplementation(async (path: string) => {
      if (path.endsWith("/login")) return login;
      statusCalls += 1;
      return response({ installed: true, authenticated: false, account: null });
    });
    const view = render(<Mounted Component={QoderSettings} />);
    const connect = await screen.findByRole("button", {
      name: "Connect to Qoder",
    });
    vi.useFakeTimers();
    fireEvent.click(connect);
    expect(mock.fetch).toHaveBeenCalledWith(
      "/harnesses/qoder/login",
      expect.anything(),
    );
    view.unmount();
    await act(async () => {
      releaseLogin(response({ type: "external", loginId: "late" }));
    });
    await act(async () => {
      vi.advanceTimersByTime(10_000);
    });
    expect(statusCalls).toBe(1);
  });
});
