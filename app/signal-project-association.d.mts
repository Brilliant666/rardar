import type { ProjectIdentityContext } from "./project-identity.mjs";

export type AuditedProjectAssociation = Readonly<{
  associationType: "github_repository";
  projectIdVersion: 1;
  projectId: string;
  repository: string;
}>;

export type AssociatedSignal<TSignal> = TSignal & {
  projectAssociation: AuditedProjectAssociation | null;
};

export function associateSignalWithCatalog<TSignal extends { repo?: unknown }>(
  signal: TSignal,
  identityContext: ProjectIdentityContext,
): Promise<AuditedProjectAssociation | null>;

export function associateSignalSnapshotWithCatalog<
  TSnapshot extends {
    signals: ReadonlyArray<{ id?: unknown; repo?: unknown }>;
    topSignals: ReadonlyArray<{ id?: unknown; repo?: unknown }>;
  },
>(
  signalSnapshot: TSnapshot,
  identityContext: ProjectIdentityContext,
): Promise<Omit<TSnapshot, "signals" | "topSignals"> & {
  signals: Array<AssociatedSignal<TSnapshot["signals"][number]>>;
  topSignals: Array<AssociatedSignal<
    TSnapshot["signals"][number] | TSnapshot["topSignals"][number]
  >>;
}>;
