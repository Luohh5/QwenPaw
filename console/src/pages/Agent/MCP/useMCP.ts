import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useAppMessage } from "../../../hooks/useAppMessage";
import api from "../../../api";
import type { MCPAccessPolicy, MCPClientInfo } from "../../../api/types";
import { useTranslation } from "react-i18next";
import { useAgentStore } from "../../../stores/agentStore";
import {
  harnessApi,
  type HarnessDiscoveredMCPServer,
} from "../../../api/modules/harness";

export function useMCP() {
  const { t } = useTranslation();
  const { selectedAgent, agents } = useAgentStore();
  const identity = useMemo(
    () => ({ active: true, agentId: selectedAgent }),
    [selectedAgent],
  );
  const currentIdentity = useRef(identity);
  currentIdentity.current = identity;
  const isCurrent = useCallback(
    () => identity.active && currentIdentity.current === identity,
    [identity],
  );
  useEffect(() => {
    identity.active = true;
    return () => {
      identity.active = false;
    };
  }, [identity]);
  const [loadedIdentity, setLoadedIdentity] = useState(identity);
  const selectedAgentInfo = agents.find((item) => item.id === selectedAgent);
  const selectedBackend = selectedAgentInfo?.backend ?? "qwenpaw";
  const canDiscoverProviderMCP = Boolean(
    selectedAgentInfo?.backend_capabilities?.provider_mcp_discovery,
  );
  const [clients, setClients] = useState<MCPClientInfo[]>([]);
  const [providerServers, setProviderServers] = useState<
    HarnessDiscoveredMCPServer[]
  >([]);
  const [loading, setLoading] = useState(false);
  const { message } = useAppMessage();

  const loadClients = useCallback(async () => {
    if (!isCurrent()) return;
    setLoading(true);
    try {
      const data = await api.listMCPClients();
      if (!isCurrent()) return;
      setLoadedIdentity(identity);
      setClients(data);
      if (selectedBackend !== "qwenpaw" && canDiscoverProviderMCP) {
        try {
          const discovered = await harnessApi.listMCP(selectedBackend);
          if (!isCurrent()) return;
          setProviderServers(discovered.servers);
          if (discovered.message) {
            message.warning(discovered.message);
          }
        } catch (error) {
          if (!isCurrent()) return;
          console.warn("Failed to discover Provider MCP servers:", error);
          setProviderServers([]);
        }
      } else {
        setProviderServers([]);
      }
    } catch (error) {
      if (!isCurrent()) return;
      console.error("Failed to load MCP clients:", error);
      message.error(t("mcp.loadError"));
    } finally {
      if (isCurrent()) setLoading(false);
    }
  }, [
    canDiscoverProviderMCP,
    message,
    selectedBackend,
    t,
    identity,
    isCurrent,
  ]);

  useEffect(() => {
    setClients([]);
    setProviderServers([]);
    loadClients();
  }, [loadClients, selectedAgent]);

  const createClient = useCallback(
    async (
      key: string,
      clientData: {
        name: string;
        description?: string;
        command: string;
        enabled?: boolean;
        transport?: "stdio" | "streamable_http" | "sse";
        url?: string;
        headers?: Record<string, string>;
        args?: string[];
        env?: Record<string, string>;
        cwd?: string;
        http_timeout?: number;
      },
    ) => {
      if (!isCurrent()) return false;
      try {
        await api.createMCPClient({
          client_key: key,
          client: clientData,
        });
        if (!isCurrent()) return false;
        message.success(t("mcp.createSuccess"));
        await loadClients();
        return true;
      } catch (error) {
        if (!isCurrent()) return false;
        const errorMsg =
          error instanceof Error ? error.message : t("mcp.createError");
        message.error(errorMsg);
        return false;
      }
    },
    [message, t, loadClients, isCurrent],
  );

  const updateClient = useCallback(
    async (
      key: string,
      updates: {
        name?: string;
        description?: string;
        command?: string;
        enabled?: boolean;
        transport?: "stdio" | "streamable_http" | "sse";
        url?: string;
        headers?: Record<string, string>;
        args?: string[];
        env?: Record<string, string>;
        cwd?: string;
        http_timeout?: number;
      },
    ) => {
      if (!isCurrent()) return false;
      try {
        await api.updateMCPClient(key, updates);
        if (!isCurrent()) return false;
        setClients((current) =>
          current.map((item) =>
            item.key === key ? { ...item, ...updates } : item,
          ),
        );
        return true;
      } catch (error) {
        if (!isCurrent()) return false;
        const errorMsg =
          error instanceof Error ? error.message : t("mcp.updateError");
        message.error(errorMsg);
        return false;
      }
    },
    [message, t, isCurrent],
  );

  const toggleEnabled = useCallback(
    async (client: MCPClientInfo) => {
      if (!isCurrent()) return;
      try {
        await api.toggleMCPClient(client.key);
        if (!isCurrent()) return;
        setClients((current) =>
          current.map((item) =>
            item.key === client.key
              ? { ...item, enabled: !client.enabled }
              : item,
          ),
        );
      } catch {
        if (!isCurrent()) return;
        message.error(t("mcp.toggleError"));
      }
    },
    [message, t, isCurrent],
  );

  const deleteClient = useCallback(
    async (client: MCPClientInfo) => {
      if (!isCurrent()) return;
      try {
        await api.deleteMCPClient(client.key);
        if (!isCurrent()) return;
        message.success(t("mcp.deleteSuccess"));
        await loadClients();
      } catch {
        if (!isCurrent()) return;
        message.error(t("mcp.deleteError"));
      }
    },
    [message, t, loadClients, isCurrent],
  );

  const updatePolicy = useCallback(
    async (clientKey: string, policy: MCPAccessPolicy) => {
      if (!isCurrent()) return false;
      try {
        await api.updateMCPPolicy(clientKey, policy);
        if (!isCurrent()) return false;
        message.success(t("mcp.access.saveSuccess"));
        await loadClients();
        return true;
      } catch (error) {
        if (!isCurrent()) return false;
        const errorMsg =
          error instanceof Error ? error.message : t("mcp.access.saveError");
        message.error(errorMsg);
        return false;
      }
    },
    [message, t, loadClients, isCurrent],
  );

  return {
    clients: loadedIdentity === identity ? clients : [],
    providerServers: loadedIdentity === identity ? providerServers : [],
    loading,
    createClient,
    updateClient,
    updatePolicy,
    toggleEnabled,
    deleteClient,
    refreshClients: loadClients,
  };
}
