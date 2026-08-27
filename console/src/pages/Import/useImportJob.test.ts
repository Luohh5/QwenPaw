import { act, renderHook, waitFor } from "@testing-library/react";
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
    streamEvents: vi.fn(),
  },
}));

const job = {
  job_id: "import-1",
  agent_id: "agent-a",
  state: "awaiting_selection" as const,
  phase: "select",
  seq: 1,
  providers: [],
  logs: [],
};

describe("useImportJob", () => {
  beforeEach(() => {
    selectedAgent = "agent-a";
    vi.clearAllMocks();
    vi.mocked(portabilityImportApi.create).mockResolvedValue(job);
    vi.mocked(portabilityImportApi.start).mockResolvedValue({
      ...job,
      state: "running",
      seq: 2,
    });
    vi.mocked(portabilityImportApi.streamEvents).mockReturnValue(
      new Promise(() => undefined),
    );
  });

  it("keeps the job pinned when the selected agent changes", async () => {
    const { result, rerender } = renderHook(() => useImportJob());
    await act(() => result.current.scan(["codex"]));

    selectedAgent = "agent-b";
    rerender();
    await act(() => result.current.start({ codex: { sessions: true } }));

    expect(portabilityImportApi.create).toHaveBeenCalledWith("agent-a", [
      "codex",
    ]);
    expect(portabilityImportApi.start).toHaveBeenCalledWith(
      "agent-a",
      "import-1",
      { codex: { sessions: true } },
    );
  });

  it("accepts only newer server snapshots", async () => {
    vi.mocked(portabilityImportApi.streamEvents).mockImplementation(
      async (_agent, _jobId, _after, onEvent) => {
        onEvent({ seq: 3, snapshot: { ...job, seq: 3, state: "completed" } });
        onEvent({ seq: 2, snapshot: { ...job, seq: 2, state: "running" } });
      },
    );
    const { result } = renderHook(() => useImportJob());

    await act(() => result.current.scan(["codex"]));
    await waitFor(() => expect(result.current.job?.seq).toBe(3));

    expect(result.current.job?.state).toBe("completed");
  });
});
