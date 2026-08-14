import { FlashList, type FlashListRef } from "@shopify/flash-list";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import * as DocumentPicker from "expo-document-picker";
import * as Haptics from "expo-haptics";
import { router, useLocalSearchParams } from "expo-router";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ActivityIndicator,
  KeyboardAvoidingView,
  Platform,
  Pressable,
  StyleSheet,
  TextInput,
  View,
} from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { AgentActivityTrace } from "@/components/agent-activity";
import { Markdown } from "@/components/markdown";
import { TranscriptSkeleton } from "@/components/skeleton";
import { Banner, Button, Divider, Type } from "@/components/ui";
import { WidgetView, type WidgetActionHandler } from "@/components/widget-renderer";
import {
  bootstrap,
  cancelAgentRun,
  createConversation,
  isUnauthorized,
  loadAgentThreadState,
  loadConversation,
  openInterrupts,
  reconnectAgentRun,
  resumeAgentInterrupt,
  sendAgentAction,
  sendAgentMessage,
  uploadCsv,
  type AgentActivity,
  type AgentRunPhase,
  type FynInterrupt,
  type PickedFile,
} from "@/lib/api";
import { useIsOffline } from "@/lib/connectivity";
import { readComposerEntry, formatMoney } from "@/lib/format";
import type { AgentResponse, Message, Widget, WidgetActionId } from "@/lib/protocol";
import { clearSession } from "@/lib/session";
import { height, radius, space, type Palette } from "@/lib/theme";
import { useStyles, useTheme } from "@/lib/appearance";
import { completedWidgetIds, csvProblem, mergeTurn, optimisticMessage, responseToMessage, type Retry } from "@/lib/thread";
import { activeWidgetId, applyWidgetUpdates, isLegacyAnalysisLifecycleWidget } from "@/lib/widget-state";

const STARTERS = ["Got ₹2 lakh salary today", "How much did I spend this month?", "Where can I cut back?"];

type Row =
  | { kind: "message"; message: Message }
  | { kind: "activity" }
  | { kind: "error" };

export default function ConversationScreen() {
  const styles = useStyles(makeStyles);
  const color = useTheme();
  const { conversationId } = useLocalSearchParams<{ conversationId: string }>();
  const insets = useSafeAreaInsets();
  const queryClient = useQueryClient();
  const listRef = useRef<FlashListRef<Row>>(null);
  const composerRef = useRef<TextInput>(null);

  const initial = useQuery({ queryKey: ["bootstrap"], queryFn: bootstrap });
  const thread = useQuery({
    queryKey: ["conversation", conversationId],
    queryFn: () => loadConversation(conversationId),
    enabled: Boolean(conversationId),
  });
  const agentState = useQuery({
    queryKey: ["agent-state", conversationId],
    queryFn: () => loadAgentThreadState(conversationId),
    enabled: Boolean(conversationId),
    staleTime: 2_000,
    refetchOnWindowFocus: true,
  });

  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [usedWidgets, setUsedWidgets] = useState<Set<string>>(new Set());
  const [pendingWidget, setPendingWidget] = useState<string | null>(null);
  const [activities, setActivities] = useState<AgentActivity[]>([]);
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  const [runPhase, setRunPhase] = useState<AgentRunPhase | null>(null);
  const [stoppingRun, setStoppingRun] = useState(false);
  const [interrupts, setInterrupts] = useState<FynInterrupt[] | null>(null);
  const [reasoningSummary, setReasoningSummary] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [retry, setRetry] = useState<Retry>(null);
  const [upload, setUpload] = useState<{ name: string; percent: number } | null>(null);
  const offline = useIsOffline();

  // The transcript is seeded from whichever query answers first and then owned
  // locally, because a turn appends to it before any refetch could.
  const hydrated = useRef<string | null>(null);
  useEffect(() => {
    const loaded = thread.data?.messages;
    if (!loaded || hydrated.current === conversationId) return;
    hydrated.current = conversationId;
    setMessages(loaded);
    setUsedWidgets(completedWidgetIds(loaded));
    setActiveRunId(null);
    setRunPhase(null);
    setStoppingRun(false);
    setInterrupts(null);
    setReasoningSummary("");
  }, [thread.data, conversationId]);

  // One controller for everything this screen has in flight. Created inside the
  // effect deliberately: holding it in state would keep a single controller
  // across a development remount, so the discarded first mount would abort the
  // one the real mount goes on to use.
  const inFlight = useRef<AbortController | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    inFlight.current = controller;
    return () => { controller.abort(); inFlight.current = null; };
  }, [conversationId]);

  const currency = initial.data?.user.currency ?? "INR";

  const succeeded = useCallback((response: AgentResponse) => {
    setMessages((current) => mergeTurn(applyWidgetUpdates(current, response.widgetUpdates), responseToMessage(response)));
    setError(null);
    setRetry(null);
    void Haptics.notificationAsync(Haptics.NotificationFeedbackType.Success);
    void Promise.all([
      queryClient.invalidateQueries({ queryKey: ["conversations"] }),
      queryClient.invalidateQueries({ queryKey: ["conversation", conversationId] }),
      queryClient.invalidateQueries({ queryKey: ["agent-state", conversationId] }),
    ]);
  }, [conversationId, queryClient]);

  const failed = useCallback((cause: Error, next: Retry) => {
    // An abort is this screen being closed, not something that went wrong with
    // it. There is nobody left to tell, and nothing to offer to retry.
    if (cause.name === "AbortError") return;
    if (isUnauthorized(cause)) {
      void clearSession();
      router.replace("/sign-in");
      return;
    }
    setError(cause.message);
    setRetry(next);
    void Haptics.notificationAsync(Haptics.NotificationFeedbackType.Error);
  }, []);

  const availableInterrupts = useMemo(
    () => interrupts ?? (agentState.data ? openInterrupts(agentState.data) : []),
    [agentState.data, interrupts],
  );
  const fallbackInterrupt = useMemo(() => {
    const widgetIds = new Set(messages.flatMap((message) => message.widgets.map((widget) => widget.id)));
    return availableInterrupts.find((interrupt) => !interrupt.widgetId || !widgetIds.has(interrupt.widgetId)) ?? null;
  }, [availableInterrupts, messages]);

  const updateActivity = useCallback((activity: AgentActivity) => {
    setActivities((current) => {
      const index = current.findIndex((item) => item.id === activity.id);
      if (index === -1) return [...current, activity];
      return current.map((item, itemIndex) => (itemIndex === index ? activity : item));
    });
  }, []);

  const agentRunCallbacks = useMemo(() => ({
    onRunCreated: (runId: string) => {
      setActiveRunId(runId);
      setStoppingRun(false);
    },
    onPhase: (phase: AgentRunPhase) => setRunPhase(phase),
    onActivity: updateActivity,
    onReasoning: setReasoningSummary,
  }), [updateActivity]);

  const chat = useMutation({
    mutationFn: ({ text }: { text: string }) => sendAgentMessage(conversationId, text, agentRunCallbacks, inFlight.current?.signal),
    onMutate: () => { setActivities([]); setReasoningSummary(""); },
    onSuccess: (result) => {
      setActivities([]);
      setActiveRunId(null);
      setStoppingRun(false);
      setInterrupts(result.interrupts);
      succeeded(result.response);
    },
    onError: (cause: Error, variables) => {
      setActivities([]);
      setActiveRunId(null);
      setStoppingRun(false);
      // A message that never reached the server should not look delivered: drop
      // the bubble and put the text back where it can be sent again.
      setMessages((current) => current.filter((message) => !(message.id.startsWith("optimistic-") && message.content === variables.text)));
      if (cause.name === "AbortError") return;
      setInput((current) => current || variables.text);
      failed(cause, { kind: "chat", text: variables.text });
    },
  });

  const action = useMutation({
    mutationFn: ({ widgetId, action: id, payload, markUsed }: { widgetId: string; action: WidgetActionId; payload: Record<string, unknown>; markUsed: boolean }) =>
      sendAgentAction(
        conversationId,
        widgetId,
        id,
        payload,
        markUsed,
        availableInterrupts.find((interrupt) => interrupt.widgetId === widgetId),
        agentRunCallbacks,
        inFlight.current?.signal,
      ),
    onSuccess: (result, variables) => {
      setPendingWidget(null);
      setActiveRunId(null);
      setStoppingRun(false);
      setInterrupts(result.interrupts);
      // Locking the card belongs to the success path; a failed action has to
      // stay pressable or the person is stuck until a restart.
      if (variables.markUsed) setUsedWidgets((current) => new Set(current).add(variables.widgetId));
      succeeded(result.response);
    },
    onError: (cause: Error, variables) => {
      setPendingWidget(null);
      setActiveRunId(null);
      setStoppingRun(false);
      failed(cause, { kind: "action", ...variables });
    },
  });

  const interrupt = useMutation({
    mutationFn: ({ current, response }: {
      current: FynInterrupt;
      response: { status: "resolved"; payload: unknown } | { status: "cancelled" };
    }) => resumeAgentInterrupt(
      conversationId,
      current,
      response,
      agentRunCallbacks,
      inFlight.current?.signal,
    ),
    onSuccess: (result) => {
      setActiveRunId(null);
      setStoppingRun(false);
      setInterrupts(result.interrupts);
      succeeded(result.response);
    },
    onError: (cause: Error) => {
      setActiveRunId(null);
      setStoppingRun(false);
      failed(cause, null);
    },
  });

  const importing = useMutation({
    mutationFn: (file: PickedFile) => uploadCsv(conversationId, file, (percent) => setUpload({ name: file.name, percent }), inFlight.current?.signal),
    onMutate: (file) => setUpload({ name: file.name, percent: 0 }),
    onSuccess: (result, file) => {
      setUpload(null);
      setMessages((current) => [...current, { ...optimisticMessage(`Uploaded ${file.name}`), id: `upload-${Date.now()}` }]);
      succeeded(result.agentResponse);
    },
    onError: (cause: Error, file) => { setUpload(null); failed(cause, { kind: "upload", file }); },
  });

  const reconnectingRun = useRef<string | null>(null);
  useEffect(() => {
    const discovered = agentState.data?.activeRun;
    if (!discovered || chat.isPending || action.isPending || interrupt.isPending || activeRunId === discovered.id || reconnectingRun.current === discovered.id) return;
    reconnectingRun.current = discovered.id;
    setActivities([]);
    setReasoningSummary("");
    reconnectAgentRun(conversationId, discovered.id, agentRunCallbacks, inFlight.current?.signal)
      .then((result) => {
        setActivities([]);
        setActiveRunId(null);
        setStoppingRun(false);
        setInterrupts(result.interrupts);
        succeeded(result.response);
      })
      .catch((cause: Error) => {
        setActivities([]);
        setActiveRunId(null);
        setStoppingRun(false);
        if (cause.name !== "AbortError") failed(cause, null);
      })
      .finally(() => { reconnectingRun.current = null; });
  }, [activeRunId, action.isPending, agentRunCallbacks, agentState.data?.activeRun, chat.isPending, conversationId, failed, interrupt.isPending, succeeded]);

  const agentRunning = Boolean(activeRunId) && runPhase !== "interrupted" && runPhase !== "succeeded" && runPhase !== "failed";
  const pausedForInterrupt = availableInterrupts.length > 0;
  const busy = chat.isPending || action.isPending || interrupt.isPending || importing.isPending || agentRunning;
  const agentStatus = stoppingRun
    ? "Stopping after the current safe step"
    : runPhase === "reconnecting"
      ? "Reconnecting to fyn AI"
      : agentRunning
        ? "fyn AI is working"
        : pausedForInterrupt
          ? "Waiting for your choice"
          : initial.data?.user.name ?? "Private workspace";
  const liveWidget = useMemo(() => activeWidgetId(messages), [messages]);

  const stopAgent = useCallback(() => {
    if (!activeRunId || stoppingRun) return;
    setStoppingRun(true);
    void Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Medium);
    void cancelAgentRun(activeRunId).catch((cause: Error) => {
      setStoppingRun(false);
      failed(cause, null);
    });
  }, [activeRunId, failed, stoppingRun]);

  const send = useCallback((value: string) => {
    const text = value.trim();
    if (!text || busy || pausedForInterrupt) return;
    if (offline) {
      // Deliberately not queued. A message held on the device and replayed
      // later could commit a financial action out of order, and the server
      // serialises one turn per conversation precisely so that cannot happen.
      // Refusing now, with the text kept, is the honest version.
      setError("You’re offline. Your message is still here — send it when you’re back.");
      return;
    }
    void Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Light);
    setInput("");
    setError(null);
    setRetry(null);
    setMessages((current) => [...current, optimisticMessage(text)]);
    chat.mutate({ text });
  }, [busy, chat, offline, pausedForInterrupt]);

  const onWidgetAction = useCallback<WidgetActionHandler>((widgetId, id, payload, options) => {
    // Per-row controls (avoidable expenses, the calculators) opt out of the
    // lock so one decision does not retire every other control on the card.
    const markUsed = options?.markUsed !== false;
    if (widgetId !== liveWidget || pendingWidget || (markUsed && usedWidgets.has(widgetId))) return;
    setError(null);
    setPendingWidget(widgetId);
    action.mutate({ widgetId, action: id, payload, markUsed });
  }, [liveWidget, pendingWidget, usedWidgets, action]);

  const retryLast = useCallback(() => {
    if (!retry) return;
    setError(null);
    if (retry.kind === "chat") send(retry.text);
    if (retry.kind === "action") { setPendingWidget(retry.widgetId); action.mutate(retry); }
    if (retry.kind === "upload") importing.mutate(retry.file);
    setRetry(null);
  }, [retry, send, action, importing]);

  async function attach() {
    const picked = await DocumentPicker.getDocumentAsync({ type: ["text/csv", "text/comma-separated-values", "public.comma-separated-values-text"], copyToCacheDirectory: true });
    if (picked.canceled || !picked.assets?.length) return;
    const asset = picked.assets[0];
    const file: PickedFile = { uri: asset.uri, name: asset.name, mimeType: asset.mimeType, size: asset.size };
    const problem = csvProblem(file);
    if (problem) { setError(problem); return; }
    setError(null);
    importing.mutate(file);
  }

  async function startNewConversation() {
    try {
      const created = await createConversation();
      router.replace({ pathname: "/c/[conversationId]", params: { conversationId: created.id } });
    } catch (cause) {
      failed(cause as Error, null);
    }
  }

  const rows: Row[] = useMemo(() => {
    const built: Row[] = messages.map((message) => ({ kind: "message" as const, message }));
    if (agentRunning) built.push({ kind: "activity" });
    if (error) built.push({ kind: "error" });
    return built;
  }, [messages, agentRunning, error]);

  // Following the conversation means staying with its newest turn. FlashList
  // maintains position from the bottom, so this only has to nudge it when the
  // list actually grows.
  useEffect(() => {
    if (!rows.length) return;
    const timer = setTimeout(() => listRef.current?.scrollToEnd({ animated: true }), 60);
    return () => clearTimeout(timer);
  }, [rows.length]);

  const reading = readComposerEntry(input);

  if (thread.isPending && !messages.length) {
    return (
      <View style={styles.root}>
        <View style={[styles.header, { paddingTop: insets.top + space.snug }]} />
        <Divider />
        <TranscriptSkeleton />
      </View>
    );
  }

  return (
    <KeyboardAvoidingView
      style={styles.root}
      behavior={Platform.OS === "ios" ? "padding" : undefined}
      keyboardVerticalOffset={0}
    >
      <View style={[styles.header, { paddingTop: insets.top + space.snug }]}>
        <View style={{ flex: 1, minWidth: 0 }}>
          <Type size="control" weight="semibold" color="ink" numberOfLines={1}>
            {thread.data?.title || "New conversation"}
          </Type>
          <Type size="meta" color={agentRunning || pausedForInterrupt ? "secondary" : "muted"}>{agentStatus}</Type>
        </View>
        <Button variant="ghost" size="control" onPress={() => router.push("/conversations")} accessibilityLabel="All conversations">Threads</Button>
        <Button variant="ghost" size="control" onPress={startNewConversation} accessibilityLabel="Start a new conversation">New</Button>
        <Button variant="ghost" size="control" onPress={() => router.push("/profile")} accessibilityLabel="Settings">•••</Button>
      </View>
      <Divider />

      <FlashList
        ref={listRef}
        data={rows}
        extraData={{ liveWidget, pendingWidget, usedWidgets, busy, agentRunning, reasoningSummary }}
        keyExtractor={(row, index) => (row.kind === "message" ? row.message.id : `${row.kind}-${index}`)}
        contentContainerStyle={{ paddingHorizontal: space.gutter, paddingTop: space.gutter, paddingBottom: space.gutter }}
        keyboardShouldPersistTaps="handled"
        keyboardDismissMode="interactive"
        ListEmptyComponent={<EmptyThread onPick={(starter) => send(starter)} />}
        renderItem={({ item }) => {
          if (item.kind === "activity") return <AgentActivityTrace activities={activities} reasoningSummary={reasoningSummary} />;
          if (item.kind === "error") {
            return (
              <View style={{ marginTop: space.base }}>
                <Banner action={retry ? <Button size="control" variant="outline" onPress={retryLast}>Retry</Button> : undefined}>
                  {error}
                </Banner>
              </View>
            );
          }
          return (
            <MessageRow
              message={item.message}
              currency={currency}
              liveWidget={liveWidget}
              pendingWidget={pendingWidget}
              usedWidgets={usedWidgets}
              busy={busy}
              onAction={onWidgetAction}
            />
          );
        }}
      />

      <View style={[styles.dock, { paddingBottom: Math.max(insets.bottom, space.base) }]}>
        {fallbackInterrupt ? (
          <InterruptFallback
            interrupt={fallbackInterrupt}
            busy={interrupt.isPending}
            onResolve={(response) => interrupt.mutate({ current: fallbackInterrupt, response })}
          />
        ) : null}
        {offline ? (
          <View style={styles.offline}>
            <Type size="meta" weight="semibold" color="attention">Offline</Type>
            <Type size="meta" color="muted" style={{ flex: 1 }}>Showing your last synced conversation.</Type>
          </View>
        ) : null}
        {upload ? (
          <View style={styles.upload}>
            <Type size="meta" color="muted" numberOfLines={1} style={{ flex: 1 }}>Uploading {upload.name}</Type>
            <Type size="meta" color="secondary" weight="semibold" tabular>{upload.percent}%</Type>
          </View>
        ) : null}
        {reading ? (
          <View style={styles.reading}>
            <Type size="meta" color="muted">Reads as </Type>
            <Type size="meta" weight="semibold" color={reading.kind === "income" ? "in" : "out"} tabular>
              {formatMoney(reading.amountMinor, currency)}
            </Type>
            <Type size="meta" color="muted"> {reading.kind}</Type>
          </View>
        ) : null}
        <View style={styles.composer}>
          <Pressable
            onPress={attach}
            disabled={busy}
            accessibilityLabel="Attach a CSV statement"
            hitSlop={space.snug}
            style={({ pressed }) => [styles.attach, pressed && { backgroundColor: color.sunken }]}
          >
            <Type size="title" color="muted">+</Type>
          </Pressable>
          <TextInput
            ref={composerRef}
            value={input}
            onChangeText={setInput}
            placeholder={pausedForInterrupt ? "Respond using the card above…" : "Spent ₹500 on lunch…"}
            placeholderTextColor={color.inkMuted}
            multiline
            style={styles.input}
            editable={!busy && !pausedForInterrupt}
            accessibilityLabel="Message fyn AI"
            onSubmitEditing={() => send(input)}
            blurOnSubmit={false}
          />
          <Pressable
            onPress={agentRunning ? stopAgent : () => send(input)}
            disabled={agentRunning ? stoppingRun : busy || pausedForInterrupt || !input.trim()}
            accessibilityLabel={agentRunning ? "Stop fyn AI" : "Send"}
            style={({ pressed }) => [
              styles.sendButton,
              {
                backgroundColor: agentRunning
                  ? pressed ? color.lineStrong : color.ink
                  : busy || pausedForInterrupt || !input.trim()
                    ? color.lineStrong
                    : pressed ? color.secondaryHover : color.secondary,
              },
            ]}
          >
            {agentRunning
              ? stoppingRun
                ? <ActivityIndicator size="small" color={color.onSecondary} />
                : <Type size="control" weight="semibold" color="onSecondary">■</Type>
              : <Type size="control" weight="semibold" color="onSecondary">↑</Type>}
          </Pressable>
        </View>
      </View>
    </KeyboardAvoidingView>
  );
}

function InterruptFallback({ interrupt, busy, onResolve }: {
  interrupt: FynInterrupt;
  busy: boolean;
  onResolve: (response: { status: "resolved"; payload: unknown } | { status: "cancelled" }) => void;
}) {
  const styles = useStyles(makeStyles);
  const toolApproval = interrupt.reason === "tool_call";
  return (
    <View style={styles.interruptFallback} accessibilityLabel="Agent response required">
      <Type size="control" weight="semibold" color="ink">fyn AI is waiting for you</Type>
      <Type size="note" color="muted">{interrupt.message || "Review this request before the agent continues."}</Type>
      <View style={styles.interruptActions}>
        <Button size="control" variant="ghost" disabled={busy} onPress={() => onResolve({ status: "cancelled" })}>Cancel request</Button>
        {toolApproval ? <>
          <Button size="control" variant="outline" disabled={busy} onPress={() => onResolve({ status: "resolved", payload: { approved: false } })}>Don’t approve</Button>
          <Button size="control" busy={busy} onPress={() => onResolve({ status: "resolved", payload: { approved: true } })}>Approve</Button>
        </> : null}
      </View>
    </View>
  );
}

function EmptyThread({ onPick }: { onPick: (starter: string) => void }) {
  const styles = useStyles(makeStyles);
  const color = useTheme();
  return (
    <View style={{ paddingVertical: space.section, gap: space.base }}>
      <Type size="title" weight="semibold" color="ink">Your finances, in one conversation</Type>
      <Type size="note" color="muted">Tell fyn AI what happened, or ask it what you spent. Everything it records is yours alone.</Type>
      <View style={{ gap: space.snug, marginTop: space.snug }}>
        {STARTERS.map((starter) => (
          <Pressable
            key={starter}
            onPress={() => onPick(starter)}
            style={({ pressed }) => [styles.starter, pressed && { backgroundColor: color.sunken }]}
          >
            <Type size="control" color="body">{starter}</Type>
          </Pressable>
        ))}
      </View>
    </View>
  );
}

function MessageRow({ message, currency, liveWidget, pendingWidget, usedWidgets, busy, onAction }: {
  message: Message;
  currency: string;
  liveWidget: string | null;
  pendingWidget: string | null;
  usedWidgets: Set<string>;
  busy: boolean;
  onAction: WidgetActionHandler;
}) {
  const styles = useStyles(makeStyles);
  const mine = message.role === "user";
  const widgets = message.widgets.filter((widget) => widget.type !== "agent_activity" && !isLegacyAnalysisLifecycleWidget(widget));

  return (
    <View style={{ marginBottom: space.wide }}>
      {message.content ? (
        mine ? (
          <View style={styles.mine}><Type size="body" color="onSecondary">{message.content}</Type></View>
        ) : (
          <Markdown>{message.content}</Markdown>
        )
      ) : null}

      {widgets.length ? (
        <View style={{ gap: space.base, marginTop: message.content ? space.base : 0 }}>
          {widgets.map((widget: Widget) => {
            // Retired for good: answered, cancelled, or superseded by a later
            // turn. `disabled` adds the transient case — a request in flight.
            const spent = widget.id !== liveWidget
              || usedWidgets.has(widget.id)
              || widget.data.lifecycle === "completed"
              || widget.data.lifecycle === "cancelled";
            return (
              <WidgetView
                key={widget.id}
                widget={widget}
                currency={currency}
                disabled={busy || spent}
                spent={spent}
                pending={pendingWidget === widget.id}
                onAction={onAction}
              />
            );
          })}
        </View>
      ) : null}
    </View>
  );
}

const makeStyles = (color: Palette) => StyleSheet.create({
  root: { flex: 1, backgroundColor: color.surface },
  centre: { flex: 1, alignItems: "center", justifyContent: "center", backgroundColor: color.surface },
  header: {
    flexDirection: "row",
    alignItems: "center",
    gap: space.base,
    paddingHorizontal: space.gutter,
    paddingBottom: space.snug,
    backgroundColor: color.surface,
  },
  mine: {
    alignSelf: "flex-end",
    maxWidth: "85%",
    paddingHorizontal: space.base,
    paddingVertical: space.snug,
    borderRadius: radius.card,
    borderBottomRightRadius: radius.chip,
    backgroundColor: color.secondary,
  },
  starter: {
    paddingHorizontal: space.base,
    paddingVertical: space.base,
    borderRadius: radius.control,
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: color.line,
    backgroundColor: color.surface,
  },
  dock: {
    paddingHorizontal: space.base,
    paddingTop: space.snug,
    borderTopWidth: StyleSheet.hairlineWidth,
    borderTopColor: color.line,
    backgroundColor: color.surface,
    gap: space.snug,
  },
  interruptFallback: {
    gap: space.tight,
    padding: space.base,
    borderRadius: radius.card,
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: color.lineStrong,
    backgroundColor: color.surface,
  },
  interruptActions: {
    flexDirection: "row",
    flexWrap: "wrap",
    justifyContent: "flex-end",
    gap: space.snug,
    marginTop: space.tight,
  },
  composer: {
    flexDirection: "row",
    alignItems: "flex-end",
    gap: space.snug,
  },
  attach: {
    width: height.touch,
    height: height.touch,
    alignItems: "center",
    justifyContent: "center",
    borderRadius: radius.control,
  },
  input: {
    flex: 1,
    minHeight: height.touch,
    maxHeight: 132,
    paddingHorizontal: space.base,
    paddingTop: space.snug + 2,
    paddingBottom: space.snug + 2,
    borderRadius: radius.card,
    borderWidth: 1,
    borderColor: color.lineStrong,
    fontSize: 15,
    color: color.ink,
    backgroundColor: color.surface,
  },
  sendButton: {
    width: height.touch,
    height: height.touch,
    alignItems: "center",
    justifyContent: "center",
    borderRadius: height.touch / 2,
  },
  upload: { flexDirection: "row", alignItems: "center", gap: space.snug, paddingHorizontal: space.tight },
  offline: {
    flexDirection: "row",
    alignItems: "center",
    gap: space.snug,
    paddingHorizontal: space.base,
    paddingVertical: space.snug,
    borderRadius: radius.control,
    backgroundColor: color.attentionTint,
  },
  reading: { flexDirection: "row", alignItems: "center", paddingHorizontal: space.tight },
});
