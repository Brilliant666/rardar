"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import {
  applyDecisionStateEvent,
  mergeGenerationBoundDecisionReads,
  replayDecisionStateEvents,
  type DecisionState,
} from "../decision-flow.mjs";
import {
  feedbackEventName,
  generationStaleEventName,
  getDeviceId,
  projectActionEventName,
} from "./device-id";

type DecisionStateContextValue = {
  generationId: string;
  stateByProjectId: Map<string, DecisionState>;
  loading: boolean;
  error: string | null;
  staleGeneration: boolean;
  retry: () => void;
  reload: () => void;
};

const DecisionStateContext = createContext<DecisionStateContextValue | null>(null);
const EMPTY_ACTIONS: readonly string[] = Object.freeze([]);

export function DecisionStateProvider({
  generationId,
  children,
}: {
  generationId: string;
  children: ReactNode;
}) {
  const [stateSnapshot, setStateSnapshot] = useState<{
    generationId: string;
    values: Map<string, DecisionState>;
  }>(() => ({ generationId, values: new Map() }));
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [staleGeneration, setStaleGeneration] = useState(false);
  const requestVersion = useRef(0);
  const mutationVersion = useRef(0);
  const mutationJournal = useRef<Array<{ version: number; detail: unknown }>>([]);

  const refresh = useCallback(async () => {
    const currentRequestVersion = ++requestVersion.current;
    const mutationVersionAtStart = mutationVersion.current;
    const deviceId = getDeviceId();
    if (!deviceId) {
      setLoading(false);
      setStaleGeneration(false);
      setError("无法建立本地设备身份，行动状态暂不可用。");
      return;
    }
    setLoading(true);
    setError(null);
    setStaleGeneration(false);
    const query = new URLSearchParams({ deviceId });
    try {
      const [actionResponse, feedbackResponse] = await Promise.all([
        fetch(`/api/actions?${query}`, { cache: "no-store" }),
        fetch(`/api/feedback?${query}`, { cache: "no-store" }),
      ]);
      if (!actionResponse.ok || !feedbackResponse.ok) {
        throw new Error("decision state unavailable");
      }
      const [actionPayload, feedbackPayload] = await Promise.all([
        actionResponse.json(),
        feedbackResponse.json(),
      ]);
      if (currentRequestVersion !== requestVersion.current) return;
      const merged = mergeGenerationBoundDecisionReads(
        generationId,
        actionPayload,
        feedbackPayload,
      );
      if (!merged) {
        setStaleGeneration(true);
        setError("数据版本已经更新，请刷新页面后继续。");
        return;
      }
      const next = replayDecisionStateEvents(
        merged,
        mutationJournal.current,
        mutationVersionAtStart,
      );
      setStateSnapshot({ generationId, values: next });
    } catch {
      if (currentRequestVersion !== requestVersion.current) return;
      setStaleGeneration(false);
      setError("行动状态暂时无法读取，请重试。");
    } finally {
      if (currentRequestVersion === requestVersion.current) setLoading(false);
    }
  }, [generationId]);

  useEffect(() => {
    const initialRefresh = window.setTimeout(() => void refresh(), 0);
    return () => {
      requestVersion.current += 1;
      window.clearTimeout(initialRefresh);
    };
  }, [generationId, refresh]);

  useEffect(() => {
    const applyEvent = (event: Event) => {
      const detail = event instanceof CustomEvent ? event.detail : null;
      if (detail?.generationId !== generationId) return;
      const version = ++mutationVersion.current;
      mutationJournal.current.push({ version, detail });
      if (mutationJournal.current.length > 100) mutationJournal.current.shift();
      setStateSnapshot((current) => ({
        generationId,
        values: applyDecisionStateEvent(
          current.generationId === generationId ? current.values : new Map(),
          detail,
        ),
      }));
    };
    window.addEventListener(projectActionEventName, applyEvent);
    window.addEventListener(feedbackEventName, applyEvent);
    return () => {
      window.removeEventListener(projectActionEventName, applyEvent);
      window.removeEventListener(feedbackEventName, applyEvent);
    };
  }, [generationId]);

  useEffect(() => {
    const markStale = (event: Event) => {
      const detail = event instanceof CustomEvent ? event.detail : null;
      if (detail?.expectedGenerationId !== generationId) return;
      setLoading(false);
      setStaleGeneration(true);
      setError("数据版本已经更新，请刷新页面后继续。");
    };
    window.addEventListener(generationStaleEventName, markStale);
    return () => window.removeEventListener(generationStaleEventName, markStale);
  }, [generationId]);

  const stateByProjectId = useMemo(
    () => stateSnapshot.generationId === generationId ? stateSnapshot.values : new Map(),
    [generationId, stateSnapshot],
  );
  const currentLoading = loading || stateSnapshot.generationId !== generationId;
  const currentError = stateSnapshot.generationId === generationId ? error : null;

  const value = useMemo(() => ({
    generationId,
    stateByProjectId,
    loading: currentLoading,
    error: currentError,
    staleGeneration,
    retry: () => void refresh(),
    reload: () => window.location.reload(),
  }), [currentError, currentLoading, generationId, refresh, staleGeneration, stateByProjectId]);

  return <DecisionStateContext.Provider value={value}>{children}</DecisionStateContext.Provider>;
}

export function useDecisionStateCollection() {
  const context = useContext(DecisionStateContext);
  if (!context) throw new Error("decision state components require DecisionStateProvider");
  return context;
}

export function useDecisionState(projectId: string) {
  const context = useDecisionStateCollection();
  return {
    ...context,
    state: context.stateByProjectId.get(projectId) ?? {
      projectIdVersion: 1 as const,
      projectId,
      actions: EMPTY_ACTIONS,
      feedback: null,
    },
  };
}
