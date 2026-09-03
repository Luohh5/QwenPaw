import { act, renderHook, waitFor } from "@testing-library/react";
import { createElement, StrictMode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { portabilityImportApi } from "../../api/modules/import";
import { useImportJob } from "./useImportJob";

let selectedAgent = "agent-a";

vi.mock("../../stores/agentStore", () => ({
  useAgentStore: () => ({ selectedAgent }),
}));
vi.mock("../../api/modules/import", () => ({
  portabilityImportApi: {
    sources: vi.fn(),
    create: vi.fn(),
    snapshot: vi.fn(),
    start: vi.fn(),
    retry: vi.fn(),
    cancel: vi.fn(),
    current: vi.fn(),
    streamEvents: vi.fn(),
  },
}));

const job = (agentId: string, jobId = `import-${agentId}`) => ({
  job_id: jobId,
  agent_id: agentId,
  state: "awaiting_selection" as const,
  seq: 1,
  providers: [],
  logs: [],
});

describe("useImportJob", () => {
  beforeEach(() => {
    selectedAgent = "agent-a";
    vi.clearAllMocks();
    sessionStorage.clear();
    localStorage.clear();
    vi.mocked(portabilityImportApi.create).mockImplementation(async (agentId) =>
      job(agentId),
    );
    vi.mocked(portabilityImportApi.snapshot).mockImplementation(
      async (agentId, jobId) => job(agentId, jobId),
    );
    vi.mocked(portabilityImportApi.start).mockImplementation(
      async (agentId, jobId) => ({ ...job(agentId, jobId), state: "running" }),
    );
    vi.mocked(portabilityImportApi.cancel).mockImplementation(
      async (agentId, jobId) => ({
        ...job(agentId, jobId),
        seq: 2,
        state: "interrupted",
      }),
    );
    vi.mocked(portabilityImportApi.current).mockResolvedValue(null);
    vi.mocked(portabilityImportApi.streamEvents).mockReturnValue(
      new Promise(() => undefined),
    );
  });

  it("keeps one recoverable import per agent while switching", async () => {
    const { result, rerender } = renderHook(() => useImportJob());
    await act(() => result.current.scan(["codex"]));

    selectedAgent = "agent-b";
    rerender();
    await waitFor(() => expect(result.current.job).toBeNull());
    await act(() => result.current.scan(["qoder"]));

    expect(portabilityImportApi.create).toHaveBeenNthCalledWith(1, "agent-a", [
      "codex",
    ]);
    expect(portabilityImportApi.create).toHaveBeenNthCalledWith(2, "agent-b", [
      "qoder",
    ]);

    selectedAgent = "agent-a";
    rerender();
    await waitFor(() => expect(result.current.job?.agent_id).toBe("agent-a"));

    selectedAgent = "agent-b";
    rerender();
    await waitFor(() => expect(result.current.job?.agent_id).toBe("agent-b"));
  });

  it("ignores a stale snapshot after switching agents", async () => {
    sessionStorage.setItem(
      "qwenpaw.portability.activeImports",
      JSON.stringify({ "agent-a": "import-a", "agent-b": "import-b" }),
    );
    let resolveA: (value: ReturnType<typeof job>) => void;
    vi.mocked(portabilityImportApi.snapshot).mockImplementation(
      (agentId, jobId) =>
        agentId === "agent-a"
          ? new Promise((resolve) => {
              resolveA = resolve;
            })
          : Promise.resolve(job(agentId, jobId)),
    );
    const { result, rerender } = renderHook(() => useImportJob());

    selectedAgent = "agent-b";
    rerender();
    await waitFor(() => expect(result.current.job?.agent_id).toBe("agent-b"));
    await act(async () => resolveA!(job("agent-a", "import-a")));

    expect(result.current.job?.agent_id).toBe("agent-b");
  });

  it("stops only the previous page subscription when switching", async () => {
    let signalA: AbortSignal | undefined;
    vi.mocked(portabilityImportApi.streamEvents).mockImplementation(
      async (agentId, _jobId, _after, _onEvent, signal) => {
        if (agentId === "agent-a") signalA = signal;
        return new Promise(() => undefined);
      },
    );
    const { result, rerender } = renderHook(() => useImportJob());
    await act(() => result.current.scan(["codex"]));
    await waitFor(() => expect(signalA).toBeDefined());

    selectedAgent = "agent-b";
    rerender();

    expect(signalA?.aborted).toBe(true);
  });

  it("removes only the current agent's active job", async () => {
    sessionStorage.setItem(
      "qwenpaw.portability.activeImports",
      JSON.stringify({ "agent-a": "import-a", "agent-b": "import-b" }),
    );
    const { result, rerender } = renderHook(() => useImportJob());
    await waitFor(() => expect(result.current.job?.job_id).toBe("import-a"));
    act(() => result.current.reset());

    selectedAgent = "agent-b";
    rerender();
    await waitFor(() => expect(result.current.job?.job_id).toBe("import-b"));
  });

  it("recovers the current job when browser storage is empty", async () => {
    vi.mocked(portabilityImportApi.current).mockResolvedValue(
      job("agent-a", "import-current"),
    );
    const { result } = renderHook(() => useImportJob());

    await waitFor(() =>
      expect(result.current.job?.job_id).toBe("import-current"),
    );
  });

  it("clears an unavailable saved job so a new import can take over", async () => {
    sessionStorage.setItem(
      "qwenpaw.portability.activeImports",
      JSON.stringify({ "agent-a": "missing-job" }),
    );
    vi.mocked(portabilityImportApi.snapshot).mockRejectedValueOnce(
      new Error("import job not found"),
    );
    const { result } = renderHook(() => useImportJob());

    await waitFor(() =>
      expect(result.current.error).toBe("import job not found"),
    );
    await act(() => result.current.scan(["codex"]));

    expect(result.current.job?.job_id).toBe("import-agent-a");
  });

  it("does not subscribe after an unmounted scan resolves", async () => {
    let resolveCreate: (value: ReturnType<typeof job>) => void;
    vi.mocked(portabilityImportApi.create).mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveCreate = resolve;
        }),
    );
    const { result, unmount } = renderHook(() => useImportJob());
    const pending = result.current.scan(["codex"]);
    unmount();
    await act(async () => resolveCreate!(job("agent-a")));
    await pending;

    expect(portabilityImportApi.streamEvents).not.toHaveBeenCalled();
  });

  it("remains usable under Strict Mode", async () => {
    const { result } = renderHook(() => useImportJob(), {
      wrapper: ({ children }) => createElement(StrictMode, null, children),
    });

    await act(() => result.current.scan(["codex"]));

    expect(result.current.job?.job_id).toBe("import-agent-a");
  });

  it("accepts only newer snapshots for the visible job", async () => {
    vi.mocked(portabilityImportApi.streamEvents).mockImplementation(
      async (_agent, _jobId, _after, onEvent) => {
        onEvent({
          seq: 3,
          snapshot: { ...job("agent-a"), seq: 3, state: "completed" },
        });
        onEvent({
          seq: 2,
          snapshot: { ...job("agent-a"), seq: 2, state: "running" },
        });
      },
    );
    const { result } = renderHook(() => useImportJob());

    await act(() => result.current.scan(["codex"]));
    await waitFor(() => expect(result.current.job?.seq).toBe(3));

    expect(result.current.job?.state).toBe("completed");
  });

  it("starts and retries only the currently selected agent's job", async () => {
    vi.mocked(portabilityImportApi.retry).mockImplementation(
      async (agentId) => ({
        ...job(agentId, `retry-${agentId}`),
        state: "running",
      }),
    );
    const { result } = renderHook(() => useImportJob());
    await act(() => result.current.scan(["codex"]));
    await act(() => result.current.start({ codex: { sessions: true } }));
    await act(() =>
      result.current.retry({ codex: { sessions: false, skills: ["skill-1"] } }),
    );

    expect(portabilityImportApi.start).toHaveBeenCalledWith(
      "agent-a",
      "import-agent-a",
      { codex: { sessions: true } },
      false,
    );
    expect(portabilityImportApi.retry).toHaveBeenCalledWith(
      "agent-a",
      "import-agent-a",
      { codex: { sessions: false, skills: ["skill-1"] } },
      false,
    );
    expect(result.current.job?.job_id).toBe("retry-agent-a");
  });

  it("cancels only the visible agent's job", async () => {
    const { result } = renderHook(() => useImportJob());
    await act(() => result.current.scan(["codex"]));
    await act(() => result.current.cancel());

    expect(portabilityImportApi.cancel).toHaveBeenCalledWith(
      "agent-a",
      "import-agent-a",
    );
    expect(result.current.job?.state).toBe("interrupted");
  });
});
