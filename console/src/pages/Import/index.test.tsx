import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { useImportJob } from "./useImportJob";
import ImportPage from ".";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));
vi.mock("./useImportJob", () => ({ useImportJob: vi.fn() }));
vi.mock("@/components/PageHeader", () => ({
  PageHeader: ({ current }: { current: string }) => <h1>{current}</h1>,
}));

const actions = {
  detect: vi.fn(),
  scan: vi.fn(),
  start: vi.fn(),
  reset: vi.fn(),
};

function state(overrides = {}) {
  return {
    sources: [
      { source: "codex", name: "Codex", detected: true },
      { source: "qoder", name: "Qoder", detected: true },
    ],
    job: null,
    loading: false,
    error: "",
    ...actions,
    ...overrides,
  };
}

function renderPage() {
  return render(
    <MemoryRouter>
      <ImportPage />
    </MemoryRouter>,
  );
}

describe("ImportPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(actions.detect).mockResolvedValue([]);
  });

  it("detects applications and supports multi-source selection", () => {
    vi.mocked(useImportJob).mockReturnValue(state() as never);
    renderPage();

    expect(actions.detect).toHaveBeenCalled();
    expect(screen.getByText("Codex")).toBeInTheDocument();
    expect(screen.getByText("Qoder")).toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", { name: "portabilityImport.continue" }),
    );
    expect(actions.scan).toHaveBeenCalledWith(["codex", "qoder"]);
  });

  it("allows clearing all sources and disables continue", () => {
    vi.mocked(useImportJob).mockReturnValue(state() as never);
    renderPage();

    fireEvent.click(screen.getByRole("checkbox", { name: "Codex" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Qoder" }));

    expect(
      screen.getByRole("button", { name: "portabilityImport.continue" }),
    ).toBeDisabled();
  });

  it("shows default-selected conversations and grouped assets", () => {
    vi.mocked(useImportJob).mockReturnValue(
      state({
        job: {
          job_id: "job",
          agent_id: "agent",
          state: "awaiting_selection",
          phase: "select",
          seq: 2,
          logs: [],
          providers: [
            {
              source: "codex",
              state: "ready",
              plan_id: "plan",
              sessions_total: 4,
              sessions_processed: 0,
              sessions_imported: 0,
              sessions_skipped: 0,
              selection: { sessions: true, skills: ["skill-1"] },
              assets: [
                {
                  asset_type: "skill",
                  source_id: "skill-1",
                  name: "Review Skill",
                  state: "pending",
                  enabled: null,
                  reason_code: "",
                  message: "",
                },
              ],
              warnings: [],
              error: "",
            },
          ],
        },
      }) as never,
    );
    renderPage();

    expect(
      screen.getByText("portabilityImport.conversations"),
    ).toBeInTheDocument();
    expect(screen.getByText("Review Skill")).toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", { name: "portabilityImport.start" }),
    );
    expect(actions.start).toHaveBeenCalledWith({
      codex: expect.objectContaining({ sessions: true, skills: ["skill-1"] }),
    });
  });

  it("renders progress and the five public result states", () => {
    const states = [
      "pending",
      "repairing",
      "not_needed",
      "failed",
      "succeeded",
    ];
    vi.mocked(useImportJob).mockReturnValue(
      state({
        job: {
          job_id: "job",
          agent_id: "agent",
          state: "completed_with_issues",
          phase: "done",
          seq: 8,
          logs: ["done"],
          providers: [
            {
              source: "qoder",
              state: "completed",
              plan_id: "plan",
              sessions_total: 3,
              sessions_processed: 3,
              sessions_imported: 2,
              sessions_skipped: 1,
              selection: { sessions: true },
              assets: states.map((assetState, index) => ({
                asset_type: "plugin",
                source_id: `plugin-${index}`,
                name: `Plugin ${index}`,
                state: assetState,
                enabled: assetState === "succeeded" ? false : null,
                reason_code: "reason",
                message: "detail",
              })),
              warnings: [],
              error: "",
            },
          ],
        },
      }) as never,
    );
    renderPage();

    for (const assetState of states) {
      expect(
        screen.getByText(`portabilityImport.states.${assetState}`),
      ).toBeInTheDocument();
    }
    expect(screen.getByText("portabilityImport.done")).toBeInTheDocument();
    expect(screen.getByRole("progressbar")).toBeInTheDocument();
  });

  it("shows conversations complete while tools are still importing", () => {
    vi.mocked(useImportJob).mockReturnValue(
      state({
        job: {
          job_id: "job",
          agent_id: "agent",
          state: "running",
          phase: "import",
          seq: 4,
          logs: [],
          providers: [
            {
              source: "codex",
              state: "running",
              plan_id: "plan",
              sessions_total: 2,
              sessions_processed: 2,
              sessions_imported: 2,
              sessions_skipped: 0,
              selection: { sessions: true },
              assets: [],
              warnings: [],
              error: "",
            },
          ],
        },
      }) as never,
    );
    renderPage();

    expect(
      screen.getByText("portabilityImport.sessionStates.succeeded"),
    ).toBeInTheDocument();
    expect(screen.getByRole("progressbar")).toHaveAttribute(
      "aria-valuenow",
      "100",
    );
  });
});
