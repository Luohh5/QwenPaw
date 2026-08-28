import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Collapse,
  Empty,
  Progress,
  Spin,
  Steps,
  Tag,
  Tooltip,
} from "antd";
import { CheckCircle2, CircleAlert, Download, PackageOpen } from "lucide-react";
import { PageHeader } from "@/components/PageHeader";
import type {
  ImportAssetResult,
  ImportAssetState,
  ImportProviderSnapshot,
  ImportSelection,
  ImportSource,
} from "../../api/types/import";
import { useImportJob } from "./useImportJob";
import styles from "./index.module.less";

const GROUPS = ["memory", "cron", "skill", "mcp", "plugin"] as const;
const FIELDS = {
  memory: "memory",
  cron: "cron",
  skill: "skills",
  mcp: "mcp",
  plugin: "plugins",
} as const;
const COLORS: Record<ImportAssetState, string> = {
  pending: "default",
  repairing: "processing",
  not_needed: "default",
  failed: "error",
  succeeded: "success",
};

function AssetStatus({ asset }: { asset: ImportAssetResult }) {
  const { t } = useTranslation();
  const fallback =
    asset.state === "not_needed"
      ? t("portabilityImport.hints.notNeeded")
      : asset.state === "failed"
      ? t("portabilityImport.hints.failed")
      : asset.state === "succeeded" && asset.enabled === false
      ? t("portabilityImport.hints.disabled")
      : "";
  const tag = (
    <Tag color={COLORS[asset.state]}>
      {t(`portabilityImport.states.${asset.state}`)}
    </Tag>
  );
  return asset.message || fallback ? (
    <Tooltip title={asset.message || fallback}>{tag}</Tooltip>
  ) : (
    tag
  );
}

const conversationsDone = (provider: ImportProviderSnapshot) =>
  provider.sessions_total > 0 &&
  provider.sessions_processed >= provider.sessions_total;

function ConversationStatus({
  provider,
}: {
  provider: ImportProviderSnapshot;
}) {
  const { t } = useTranslation();
  if (provider.state === "failed") {
    return (
      <Tag color="error">{t("portabilityImport.sessionStates.failed")}</Tag>
    );
  }
  if (provider.state === "completed" || conversationsDone(provider)) {
    return (
      <Tag color="success">
        {t("portabilityImport.sessionStates.succeeded", {
          count: provider.sessions_imported,
          total: provider.sessions_total,
        })}
      </Tag>
    );
  }
  return (
    <Tag color="processing">
      {t("portabilityImport.sessionStates.importing", {
        count: provider.sessions_processed,
        total: provider.sessions_total,
      })}
    </Tag>
  );
}

function completion(providers: ImportProviderSnapshot[]) {
  const assets = providers.flatMap((provider) => provider.assets);
  const sessionRows = providers.filter(
    (provider) => provider.selection.sessions && provider.sessions_total,
  );
  const doneAssets = assets.filter((asset) =>
    ["not_needed", "failed", "succeeded"].includes(asset.state),
  ).length;
  const doneSessions = sessionRows.filter(
    (provider) =>
      ["completed", "failed"].includes(provider.state) ||
      conversationsDone(provider),
  ).length;
  const total = assets.length + sessionRows.length;
  return total ? Math.round(((doneAssets + doneSessions) / total) * 100) : 0;
}

export default function ImportPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { sources, job, loading, error, detect, scan, start, reset } =
    useImportJob();
  const [selectedSources, setSelectedSources] = useState<ImportSource[]>([]);
  const sourcesInitialized = useRef(false);
  const [selections, setSelections] = useState<
    Partial<Record<ImportSource, ImportSelection>>
  >({});

  useEffect(() => {
    void detect().catch(() => undefined);
  }, [detect]);
  useEffect(() => {
    if (!job && sources.length && !sourcesInitialized.current) {
      sourcesInitialized.current = true;
      setSelectedSources(
        sources
          .filter((source) => source.detected)
          .map((source) => source.source),
      );
    }
  }, [job, sources]);
  useEffect(() => {
    if (job?.state !== "awaiting_selection" || Object.keys(selections).length) {
      return;
    }
    setSelections(
      Object.fromEntries(
        job.providers
          .filter((provider) => provider.state === "ready")
          .map((provider) => [provider.source, provider.selection]),
      ),
    );
  }, [job, selections]);

  const current = !job ? 0 : job.state === "awaiting_selection" ? 1 : 2;
  const isRunning = job?.state === "running" || job?.state === "scanning";
  const isDone = Boolean(
    job &&
      ["completed", "completed_with_issues", "failed", "interrupted"].includes(
        job.state,
      ),
  );
  const percent = useMemo(() => completion(job?.providers ?? []), [job]);

  const updateSelection = (
    source: ImportSource,
    update: (selection: ImportSelection) => ImportSelection,
  ) => {
    setSelections((currentSelections) => ({
      ...currentSelections,
      [source]: update(currentSelections[source] ?? {}),
    }));
  };

  const toggleAsset = (
    provider: ImportProviderSnapshot,
    asset: ImportAssetResult,
    checked: boolean,
  ) => {
    const field = FIELDS[asset.asset_type];
    updateSelection(provider.source, (selection) => {
      const values = new Set(selection[field] ?? []);
      if (checked) values.add(asset.source_id);
      else values.delete(asset.source_id);
      return { ...selection, [field]: [...values] };
    });
  };

  const toggleGroup = (
    provider: ImportProviderSnapshot,
    type: (typeof GROUPS)[number],
    checked: boolean,
  ) => {
    const field = FIELDS[type];
    const ids = provider.assets
      .filter((asset) => asset.asset_type === type)
      .map((asset) => asset.source_id);
    updateSelection(provider.source, (selection) => ({
      ...selection,
      [field]: checked ? ids : [],
    }));
  };

  return (
    <div className={styles.page}>
      <PageHeader
        parent={t("nav.apps")}
        current={t("portabilityImport.title")}
      />
      <main className={styles.content}>
        <div className={styles.intro}>
          <Download size={28} />
          <div>
            <h2>{t("portabilityImport.title")}</h2>
            <p>{t("portabilityImport.description")}</p>
          </div>
        </div>
        <Steps
          current={current}
          items={[
            { title: t("portabilityImport.steps.sources") },
            { title: t("portabilityImport.steps.inventory") },
            { title: t("portabilityImport.steps.progress") },
          ]}
        />
        {error && <Alert type="error" showIcon message={error} />}

        {!job && (
          <section className={styles.section}>
            <div className={styles.sectionHeading}>
              <h3>{t("portabilityImport.chooseSources")}</h3>
              <p>{t("portabilityImport.chooseSourcesHint")}</p>
            </div>
            {loading && !sources.length ? (
              <div className={styles.center}>
                <Spin />
              </div>
            ) : (
              <div className={styles.sourceGrid}>
                {sources.map((source) => (
                  <Card key={source.source} className={styles.sourceCard}>
                    <Checkbox
                      checked={selectedSources.includes(source.source)}
                      disabled={!source.detected}
                      onChange={(event) =>
                        setSelectedSources((selected) =>
                          event.target.checked
                            ? [...selected, source.source]
                            : selected.filter((item) => item !== source.source),
                        )
                      }
                    >
                      <strong>{source.name}</strong>
                    </Checkbox>
                    <Tag color={source.detected ? "success" : "default"}>
                      {t(
                        source.detected
                          ? "portabilityImport.detected"
                          : "portabilityImport.notDetected",
                      )}
                    </Tag>
                  </Card>
                ))}
              </div>
            )}
            {!loading && sources.every((source) => !source.detected) && (
              <Empty description={t("portabilityImport.noSources")} />
            )}
            <div className={styles.actions}>
              <Button
                type="primary"
                disabled={!selectedSources.length}
                loading={loading}
                onClick={() => void scan(selectedSources)}
              >
                {t("portabilityImport.continue")}
              </Button>
            </div>
          </section>
        )}

        {job?.state === "scanning" && (
          <div className={styles.center}>
            <Spin size="large" />
            <span>{t("portabilityImport.scanning")}</span>
          </div>
        )}

        {job?.state === "awaiting_selection" && (
          <section className={styles.section}>
            <div className={styles.sectionHeading}>
              <h3>{t("portabilityImport.chooseContent")}</h3>
              <p>{t("portabilityImport.defaultSelected")}</p>
            </div>
            {job.providers.map((provider) => (
              <Card key={provider.source} title={provider.source.toUpperCase()}>
                {provider.state === "failed" ? (
                  <Alert type="error" showIcon message={provider.error} />
                ) : (
                  <>
                    <div className={styles.row}>
                      <Checkbox
                        checked={selections[provider.source]?.sessions ?? false}
                        disabled={!provider.sessions_total}
                        onChange={(event) =>
                          updateSelection(provider.source, (selection) => ({
                            ...selection,
                            sessions: event.target.checked,
                          }))
                        }
                      >
                        {t("portabilityImport.conversations")}
                      </Checkbox>
                      <span>
                        {t("portabilityImport.items", {
                          count: provider.sessions_total,
                        })}
                      </span>
                    </div>
                    <Collapse
                      className={styles.groups}
                      defaultActiveKey={[...GROUPS]}
                      items={GROUPS.flatMap((type) => {
                        const assets = provider.assets.filter(
                          (asset) => asset.asset_type === type,
                        );
                        if (!assets.length) return [];
                        const field = FIELDS[type];
                        const selected =
                          selections[provider.source]?.[field] ?? [];
                        return [
                          {
                            key: type,
                            label: (
                              <Checkbox
                                checked={selected.length === assets.length}
                                indeterminate={Boolean(
                                  selected.length &&
                                    selected.length < assets.length,
                                )}
                                onClick={(event) => event.stopPropagation()}
                                onChange={(event) =>
                                  toggleGroup(
                                    provider,
                                    type,
                                    event.target.checked,
                                  )
                                }
                              >
                                {t(`portabilityImport.groups.${type}`)} (
                                {assets.length})
                              </Checkbox>
                            ),
                            children: assets.map((asset) => (
                              <div
                                className={styles.assetRow}
                                key={asset.source_id}
                              >
                                <Checkbox
                                  checked={selected.includes(asset.source_id)}
                                  onChange={(event) =>
                                    toggleAsset(
                                      provider,
                                      asset,
                                      event.target.checked,
                                    )
                                  }
                                >
                                  {asset.name}
                                </Checkbox>
                              </div>
                            )),
                          },
                        ];
                      })}
                    />
                  </>
                )}
              </Card>
            ))}
            <div className={styles.actions}>
              <Button
                type="primary"
                loading={loading}
                onClick={() => void start(selections)}
              >
                {t("portabilityImport.start")}
              </Button>
            </div>
          </section>
        )}

        {job && current === 2 && (
          <section className={styles.section}>
            <div>
              <Progress
                percent={isDone ? 100 : percent}
                status={job.state === "failed" ? "exception" : "active"}
              />
            </div>
            <div className={styles.resultHeader}>
              {isDone ? <CheckCircle2 /> : <PackageOpen />}
              <div>
                <h3>
                  {t(
                    isDone
                      ? "portabilityImport.finished"
                      : "portabilityImport.importing",
                  )}
                </h3>
                <p>{t("portabilityImport.keepOpen")}</p>
              </div>
            </div>
            {job.providers.map((provider) => (
              <Card key={provider.source} title={provider.source.toUpperCase()}>
                {provider.selection.sessions && provider.sessions_total > 0 && (
                  <div className={styles.row}>
                    <span>{t("portabilityImport.conversations")}</span>
                    <ConversationStatus provider={provider} />
                  </div>
                )}
                {provider.assets.map((asset) => (
                  <div
                    className={styles.assetRow}
                    key={`${asset.asset_type}:${asset.source_id}`}
                  >
                    <span>{asset.name}</span>
                    <AssetStatus asset={asset} />
                  </div>
                ))}
                {provider.error && (
                  <Alert
                    icon={<CircleAlert />}
                    type="error"
                    showIcon
                    message={provider.error}
                  />
                )}
              </Card>
            ))}
            {job.logs.length > 0 && (
              <Collapse
                items={[
                  {
                    key: "logs",
                    label: t("portabilityImport.details"),
                    children: (
                      <pre className={styles.logs}>{job.logs.join("\n")}</pre>
                    ),
                  },
                ]}
              />
            )}
            <div className={styles.actions}>
              <Button
                type="primary"
                disabled={!isDone || isRunning}
                onClick={() => {
                  reset();
                  navigate("/chat");
                }}
              >
                {t("portabilityImport.done")}
              </Button>
            </div>
          </section>
        )}
      </main>
    </div>
  );
}
