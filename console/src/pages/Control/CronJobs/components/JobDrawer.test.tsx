import type { ComponentProps, ReactNode } from "react";
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("@agentscope-ai/design", () => {
  const Form = ({ children }: { children?: ReactNode }) => <>{children}</>;
  Form.Item = ({
    children,
    extra,
    label,
    name,
    rules,
  }: {
    children?: ReactNode | ((helpers: unknown) => ReactNode);
    extra?: ReactNode;
    label?: ReactNode;
    name?: string | string[];
    rules?: Array<{ required?: boolean }>;
  }) => {
    if (typeof children === "function") {
      return children({
        getFieldValue: vi.fn(() => undefined),
        setFieldValue: vi.fn(),
      });
    }
    const fieldName = Array.isArray(name) ? name.join(".") : name;
    return (
      <div
        data-testid={fieldName ? `field-${fieldName}` : undefined}
        data-required={rules?.some((rule) => rule.required) ? "true" : "false"}
      >
        {label && <span>{label}</span>}
        {children}
        {extra && <span>{extra}</span>}
      </div>
    );
  };
  Form.useWatch = vi.fn(() => undefined);

  const Select = ({ children }: { children?: ReactNode }) => <>{children}</>;
  Select.Option = ({ children }: { children?: ReactNode }) => <>{children}</>;

  const passthrough = ({ children }: { children?: ReactNode }) => (
    <>{children}</>
  );
  const Input = (props: ComponentProps<"input">) => <input {...props} />;
  Input.TextArea = () => <textarea />;
  return {
    Button: passthrough,
    Checkbox: passthrough,
    Drawer: passthrough,
    Form,
    Input,
    InputNumber: () => <input />,
    Select,
    Switch: () => <button type="button" />,
  };
});

vi.mock("antd", () => ({
  DatePicker: () => <input />,
  TimePicker: () => <input />,
}));

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

vi.mock("../../../../hooks/useTimezoneOptions", () => ({
  useTimezoneOptions: () => [{ label: "UTC", value: "UTC" }],
}));

import type { CronJobSpecOutput } from "../../../../api/types";
import { JobDrawer } from "./JobDrawer";

function pendingJob(remote: boolean): CronJobSpecOutput {
  return {
    id: remote ? "remote-imported" : "local-imported",
    name: "Imported task",
    enabled: false,
    schedule: { type: "cron", cron: "0 9 * * *", timezone: "UTC" },
    task_type: "agent",
    request: {
      input: [{ role: "user", content: "run report" }],
      request_context: {},
    },
    dispatch: {
      type: "channel",
      channel: "console",
      target: { user_id: "system", session_id: "isolated" },
    },
    meta: {
      portability: {
        requires_review: true,
        safety: "disabled_until_explicit_promotion",
        source_cwd_remote_or_unverified: remote,
      },
    },
  };
}

const fakeForm = {
  getFieldValue: vi.fn(() => undefined),
  setFieldValue: vi.fn(),
  submit: vi.fn(),
};

function renderDrawer(job: CronJobSpecOutput) {
  render(
    <JobDrawer
      open
      editingJob={job}
      form={fakeForm as never}
      saving={false}
      targetItems={[]}
      targetChannels={["console"]}
      targetsLoading={false}
      onReloadTargets={vi.fn().mockResolvedValue(undefined)}
      onClose={vi.fn()}
      onSubmit={vi.fn()}
    />,
  );
}

describe("JobDrawer imported project mapping", () => {
  it("shows a required local project mapping for remote imported jobs", () => {
    renderDrawer(pendingJob(true));

    const field = screen.getByTestId(
      "field-request.request_context.project_dir",
    );
    expect(field).toHaveAttribute("data-required", "true");
    expect(
      screen.getByText("cronJobs.importReviewProjectDirLabel"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("cronJobs.importReviewProjectDirExtra"),
    ).toBeInTheDocument();
  });

  it("shows the same mapping as optional for locally sourced imported jobs", () => {
    renderDrawer(pendingJob(false));

    expect(
      screen.getByTestId("field-request.request_context.project_dir"),
    ).toHaveAttribute("data-required", "false");
  });
});
