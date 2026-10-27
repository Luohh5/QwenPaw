import { getHost, type HarnessSettingsProps } from "@qwenpaw/plugin-sdk";
import settingsCss from "./Settings.css?inline";
import { localMessages } from "./locale";

const host = getHost().host;
const React = host.React;
const { Button, Input, Tooltip, App } = host.antd;
const {
  CheckOutlined,
  QuestionCircleOutlined,
  ReloadOutlined,
  CopyOutlined,
  ExclamationCircleOutlined,
  ExportOutlined,
  LogoutOutlined,
  ThunderboltOutlined,
  ApartmentOutlined,
  SafetyOutlined,
} = host.antdIcons;
const BACKEND = "qoder";
interface Status {
  installed: boolean;
  authenticated: boolean;
  runtime_path?: string | null;
  error?: string | null;
  account?: { type?: string; email?: string; username?: string } | null;
  capabilities?: {
    qwenpaw_skills_projection?: boolean;
    qwenpaw_mcp_projection?: boolean;
    mcp_tool_allowlist?: boolean;
  };
}

export function Settings({ value, onChange, open }: HarnessSettingsProps) {
  const text = localMessages(host.useLocale());
  const { message } = App.useApp();
  const settings = React.useRef(value);
  settings.current = value;
  const [status, setStatus] = React.useState<Status | null>(null);
  const [checking, setChecking] = React.useState(false);
  const [connecting, setConnecting] = React.useState(false);
  const [error, setError] = React.useState("");
  const timer = React.useRef<ReturnType<typeof setTimeout>>();
  const timeout = React.useRef<ReturnType<typeof setTimeout>>();
  const polling = React.useRef(false);
  const active = React.useRef(false);
  const stop = React.useCallback(() => {
    polling.current = false;
    clearTimeout(timer.current);
    clearTimeout(timeout.current);
  }, []);
  const request = React.useCallback(async (operation: string) => {
    const response = await host.fetch(`/harnesses/${BACKEND}/${operation}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ settings: settings.current, device_code: false }),
    });
    const body = await response.json();
    if (!response.ok)
      throw new Error(
        typeof body.detail === "string"
          ? body.detail
          : body.detail?.message ?? `HTTP ${response.status}`,
      );
    return body;
  }, []);
  const load = React.useCallback(async () => {
    const result = (await request("status")) as Status;
    if (active.current) {
      setStatus(result);
      setError(result.error ?? "");
    }
    return result;
  }, [request]);
  const check = React.useCallback(async () => {
    setChecking(true);
    try {
      await load();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setChecking(false);
    }
  }, [load]);
  React.useEffect(() => {
    active.current = open;
    if (open) void check();
    return () => {
      active.current = false;
      stop();
    };
  }, [open, check, stop]);
  const beginPolling = () => {
    stop();
    polling.current = true;
    const poll = async () => {
      try {
        const result = await load();
        if (!polling.current) return;
        if (result.authenticated) {
          stop();
          setConnecting(false);
          message.success(text.connected);
          return;
        }
      } catch {
        /* The existing login flow retries until its overall timeout. */
      }
      if (polling.current) timer.current = setTimeout(() => void poll(), 2000);
    };
    timer.current = setTimeout(() => void poll(), 2000);
    timeout.current = setTimeout(() => {
      stop();
      setConnecting(false);
    }, 120000);
  };
  const connect = async () => {
    const popup = false ? window.open("about:blank", "_blank") : null;
    if (popup) popup.opener = null;
    setConnecting(true);
    try {
      const login = await request("login");
      if (!active.current) {
        popup?.close();
        return;
      }
      if (login.authUrl) {
        if (popup) popup.location.href = login.authUrl;
        else window.open(login.authUrl, "_blank", "noopener,noreferrer");
      } else if (login.type !== "external")
        throw new Error("Login did not start");
      beginPolling();
    } catch (cause) {
      popup?.close();
      setConnecting(false);
      message.error(cause instanceof Error ? cause.message : String(cause));
    }
  };
  const disconnect = async () => {
    try {
      await request("logout");
      await load();
      message.success(text.disconnected);
    } catch (cause) {
      message.error(cause instanceof Error ? cause.message : String(cause));
    }
  };
  const identity = status?.account?.email ?? status?.account?.username;
  const kind = status?.account?.type;
  const token = kind === "apiKey" || kind === "accessToken";
  const account = token
    ? text.apiKeyAuthenticated
    : kind === "chatgpt"
    ? text.chatGptAuthenticated
    : text.cliAuthenticated.replace("{{type}}", "Qoder");
  return (
    <div className="harness-qoder-settings">
      <style>{settingsCss}</style>
      <p className="providerHint">{text.hint}</p>
      <div className="binaryField">
        <label className="binaryLabel" htmlFor="qoder-binary">
          {text.binary}
        </label>
        <Tooltip title={text.binaryHelp}>
          <button
            type="button"
            className="helpIcon"
            aria-label={text.binaryHelp}
          >
            <QuestionCircleOutlined aria-hidden="true" />
          </button>
        </Tooltip>
        <Input
          id="qoder-binary"
          value={String(value.binary ?? "")}
          placeholder={text.binaryPlaceholder}
          allowClear
          onChange={(event: React.ChangeEvent<HTMLInputElement>) =>
            onChange({ ...value, binary: event.target.value })
          }
          onBlur={(event: React.FocusEvent<HTMLInputElement>) =>
            onChange({
              ...value,
              binary: event.target.value.trim() || undefined,
            })
          }
          suffix={
            <Button
              type="text"
              size="small"
              aria-label={text.detect}
              icon={<ReloadOutlined aria-hidden="true" />}
              loading={checking}
              onClick={() => void check()}
            >
              <span className="detectLabel">{text.detect}</span>
            </Button>
          }
        />
      </div>
      {status?.runtime_path && (
        <div className="binaryStatus detected">
          <strong className="binaryStatusLabel">
            <CheckOutlined aria-hidden="true" />
            {text.detected}
          </strong>{" "}
          <Tooltip title={status.runtime_path}>
            <code className="binaryPath">{status.runtime_path}</code>
          </Tooltip>{" "}
          <Button
            className="copyPathButton"
            type="text"
            aria-label={text.copy}
            title={text.copy}
            icon={<CopyOutlined aria-hidden="true" />}
            size="small"
            onClick={() =>
              void navigator.clipboard
                .writeText(status.runtime_path!)
                .then(() => message.success(text.copied))
                .catch(() => message.error(text.copyFailed))
            }
          />
        </div>
      )}
      {!checking && status && !status.installed && (
        <p className="binaryStatus notDetected" role="status">
          <ExclamationCircleOutlined aria-hidden="true" />
          {text.notFound}
        </p>
      )}
      <div className="accountRow">
        <div className="accountState">
          <strong className="accountLabel">{text.account}</strong>
          <span
            className={
              status?.authenticated ? "connectedState" : "disconnectedState"
            }
          >
            {status?.authenticated ? (
              <CheckOutlined aria-hidden="true" />
            ) : (
              <ExclamationCircleOutlined aria-hidden="true" />
            )}
            {status?.authenticated
              ? `${account}${identity ? ` · ${identity}` : ""}`
              : text.notConnected}
          </span>
        </div>
        <div className="accountAction">
          {!status?.authenticated && (
            <Button
              icon={<ExportOutlined aria-hidden="true" />}
              disabled={!status?.installed}
              loading={connecting}
              onClick={() => void connect()}
            >
              {text.connect}
            </Button>
          )}
          {false && status?.authenticated && kind === "chatgpt" && (
            <Button
              icon={<LogoutOutlined aria-hidden="true" />}
              onClick={() => void disconnect()}
            >
              {text.disconnect}
            </Button>
          )}
        </div>
      </div>
      {!status?.authenticated && status?.installed && (
        <p className="authHint">{text.authHint}</p>
      )}
      {status?.authenticated && (
        <p className="chatSettingsHint">{text.chatSettingsHint}</p>
      )}
      {error && (
        <p className="authHint" role="alert">
          {error}
        </p>
      )}
      {status && (
        <div className="capabilityPanel">
          <strong className="capabilityTitle">{text.capabilityTitle}</strong>
          <div className="capabilityGrid">
            <div className="capabilityItem">
              <ThunderboltOutlined aria-hidden="true" />
              <span>
                <strong>{text.inheritedSkills}</strong>
                <small>
                  {status.capabilities?.qwenpaw_skills_projection
                    ? text.runtimeInherited
                    : text.notSupported}
                </small>
              </span>
            </div>
            <div className="capabilityItem">
              <ApartmentOutlined aria-hidden="true" />
              <span>
                <strong>{text.inheritedMcp}</strong>
                <small>
                  {status.capabilities?.qwenpaw_mcp_projection
                    ? text.runtimeInherited
                    : text.notSupported}
                </small>
              </span>
            </div>
            <div className="capabilityItem">
              <SafetyOutlined aria-hidden="true" />
              <span>
                <strong>{text.policyCompatibility}</strong>
                <small>
                  {status.capabilities?.mcp_tool_allowlist
                    ? text.toolAllowlistSupported
                    : text.policyLimited}
                </small>
              </span>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
