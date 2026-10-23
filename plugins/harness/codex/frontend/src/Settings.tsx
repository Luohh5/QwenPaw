import { getHost, type HarnessSettingsProps } from "@qwenpaw/plugin-sdk";
import { localMessages } from "./locale";

const host = getHost().host;
const React = host.React;
const { Button, Input, Tooltip, App } = host.antd;
const BACKEND = "codex";
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
    const popup = true ? window.open("about:blank", "_blank") : null;
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
    : text.cliAuthenticated.replace("{{type}}", "Codex");
  return (
    <div style={{ display: "grid", gap: 12, marginTop: 16 }}>
      <p>{text.hint}</p>
      <label>
        <Tooltip title={text.binaryHelp}>
          <span>{text.binary}</span>
        </Tooltip>
        <Input
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
            <Button type="text" loading={checking} onClick={() => void check()}>
              {text.detect}
            </Button>
          }
        />
      </label>
      {status?.runtime_path && (
        <div>
          <strong>{text.detected}</strong>{" "}
          <Tooltip title={status.runtime_path}>
            <code style={{ overflowWrap: "anywhere" }}>
              {status.runtime_path}
            </code>
          </Tooltip>{" "}
          <Button
            size="small"
            onClick={() =>
              void navigator.clipboard
                .writeText(status.runtime_path!)
                .then(() => message.success(text.copied))
                .catch(() => message.error(text.copyFailed))
            }
          >
            {text.copy}
          </Button>
        </div>
      )}
      {!checking && status && !status.installed && (
        <p role="status">{text.notFound}</p>
      )}
      <div>
        <strong>{text.account}</strong> ·{" "}
        {status?.authenticated
          ? `${account}${identity ? ` · ${identity}` : ""}`
          : text.notConnected}
        {!status?.authenticated && (
          <Button
            disabled={!status?.installed}
            loading={connecting}
            onClick={() => void connect()}
          >
            {text.connect}
          </Button>
        )}
        {true && status?.authenticated && kind === "chatgpt" && (
          <Button onClick={() => void disconnect()}>{text.disconnect}</Button>
        )}
      </div>
      {!status?.authenticated && status?.installed && <p>{text.authHint}</p>}
      {status?.authenticated && <p>{text.chatSettingsHint}</p>}
      {error && <p role="alert">{error}</p>}
      {status && (
        <div>
          <strong>{text.capabilityTitle}</strong>
          <p>
            {text.inheritedSkills}:{" "}
            {status.capabilities?.qwenpaw_skills_projection
              ? text.runtimeInherited
              : text.notSupported}
          </p>
          <p>
            {text.inheritedMcp}:{" "}
            {status.capabilities?.qwenpaw_mcp_projection
              ? text.runtimeInherited
              : text.notSupported}
          </p>
          <p>
            {text.policyCompatibility}:{" "}
            {status.capabilities?.mcp_tool_allowlist
              ? text.toolAllowlistSupported
              : text.policyLimited}
          </p>
        </div>
      )}
    </div>
  );
}
