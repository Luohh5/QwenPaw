import type { ReactElement, ReactNode } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("@agentscope-ai/design", () => ({
  Button: ({
    children,
    disabled,
    loading,
    onClick,
  }: {
    children?: ReactNode;
    disabled?: boolean;
    loading?: boolean;
    onClick?: () => void;
  }) => (
    <button
      type="button"
      disabled={disabled}
      data-loading={loading ? "true" : "false"}
      onClick={onClick}
    >
      {children}
    </button>
  ),
  Tooltip: ({ children }: { children?: ReactNode }) => <>{children}</>,
  Dropdown: ({ children }: { children?: ReactNode }) => <>{children}</>,
  Tag: ({ children }: { children?: ReactNode }) => <span>{children}</span>,
}));

vi.mock("../../../../hooks/useAppMessage", () => ({
  useAppMessage: () => ({ message: { success: vi.fn(), error: vi.fn() } }),
}));

import type { CronJobSpecOutput } from "../../../../api/types";
import { createColumns } from "./columns";

const pendingJob = {
  id: "imported-job",
  name: "Imported Job",
  enabled: false,
  schedule: { type: "cron", cron: "0 9 * * *" },
  dispatch: {
    type: "channel",
    target: { user_id: "system", session_id: "isolated" },
  },
  meta: {
    portability: {
      requires_review: true,
      safety: "disabled_until_explicit_promotion",
    },
  },
} satisfies CronJobSpecOutput;

const labels: Record<string, string> = {
  "cronJobs.importReviewBadge": "Imported review required",
  "cronJobs.importReviewApprove": "Approve review",
  "common.enable": "Enable",
  "cronJobs.executeNow": "Run Now",
  "cronJobs.executionHistory": "History",
};

function makeColumns(onPromoteImported = vi.fn()) {
  return {
    columns: createColumns({
      onToggleEnabled: vi.fn(),
      onExecuteNow: vi.fn(),
      onPromoteImported,
      onViewHistory: vi.fn(),
      onEdit: vi.fn(),
      onDelete: vi.fn(),
      promotingJobIds: new Set(),
      t: ((key: string) => labels[key] ?? key) as never,
    }),
    onPromoteImported,
  };
}

describe("imported Cron review controls", () => {
  it("shows a clear review badge in the status column", () => {
    const { columns } = makeColumns();
    const statusColumn = columns.find((column) => column.key === "enabled");
    const renderCell = statusColumn?.render as (
      enabled: boolean,
      job: CronJobSpecOutput,
      index: number,
    ) => ReactNode;

    render(renderCell(false, pendingJob, 0) as ReactElement);

    expect(screen.getByText("Imported review required")).toBeInTheDocument();
  });

  it("keeps the review gate when the safety marker remains on a stale record", () => {
    const safetyGatedJob: CronJobSpecOutput = {
      ...pendingJob,
      meta: {
        portability: {
          requires_review: false,
          safety: "disabled_until_explicit_promotion",
        },
      },
    };
    const { columns } = makeColumns();
    const statusColumn = columns.find((column) => column.key === "enabled");
    const renderCell = statusColumn?.render as (
      enabled: boolean,
      job: CronJobSpecOutput,
      index: number,
    ) => ReactNode;

    render(renderCell(false, safetyGatedJob, 0) as ReactElement);

    expect(screen.getByText("Imported review required")).toBeInTheDocument();
  });

  it("disables ordinary enable/run actions and exposes only review approval", () => {
    const { columns, onPromoteImported } = makeColumns();
    const actionColumn = columns.find((column) => column.key === "action");
    const renderCell = actionColumn?.render as (
      value: unknown,
      job: CronJobSpecOutput,
      index: number,
    ) => ReactNode;

    render(renderCell(undefined, pendingJob, 0) as ReactElement);

    expect(screen.getByRole("button", { name: "Enable" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Run Now" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Approve review" }));
    expect(onPromoteImported).toHaveBeenCalledWith(pendingJob);
  });
});
