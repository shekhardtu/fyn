import { useInfiniteQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import * as Haptics from "expo-haptics";
import { router } from "expo-router";
import { useState } from "react";
import { ActivityIndicator, Pressable, StyleSheet, View } from "react-native";
import { Swipeable } from "react-native-gesture-handler";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { ListSkeleton } from "@/components/skeleton";
import { Banner, Button, Divider, Type } from "@/components/ui";
import { createConversation, deleteConversation, listConversations } from "@/lib/api";
import { formatRelative } from "@/lib/format";
import type { ConversationSummary } from "@/lib/protocol";
import { radius, space, type Palette } from "@/lib/theme";
import { useStyles, useTheme } from "@/lib/appearance";

/**
 * Everything you have talked about, newest first.
 *
 * The web app keeps this in a rail beside the thread. There is no room for a
 * rail on a phone, so it is a screen you push — which also means the list is
 * only paid for when it is asked for, rather than on every conversation load.
 */
export default function ConversationsScreen() {
  const styles = useStyles(makeStyles);
  const color = useTheme();
  const insets = useSafeAreaInsets();
  const queryClient = useQueryClient();
  const [problem, setProblem] = useState<string | null>(null);

  const history = useInfiniteQuery({
    queryKey: ["conversations"],
    queryFn: ({ pageParam }) => listConversations(pageParam),
    initialPageParam: null as string | null,
    getNextPageParam: (page) => page.nextCursor,
  });

  const remove = useMutation({
    mutationFn: (id: string) => deleteConversation(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["conversations"] }),
    onError: (cause: Error) => setProblem(cause.message),
  });

  const start = useMutation({
    mutationFn: createConversation,
    onSuccess: (created) => {
      void queryClient.invalidateQueries({ queryKey: ["conversations"] });
      router.replace({ pathname: "/c/[conversationId]", params: { conversationId: created.id } });
    },
    onError: (cause: Error) => setProblem(cause.message),
  });

  const conversations = history.data?.pages.flatMap((page) => page.items) ?? [];

  return (
    <View style={styles.root}>
      <View style={[styles.header, { paddingTop: insets.top + space.snug }]}>
        <Button variant="ghost" size="control" onPress={() => router.back()} accessibilityLabel="Back to the conversation">← Back</Button>
        <Type size="control" weight="semibold" color="ink" style={{ flex: 1, textAlign: "center" }}>Conversations</Type>
        <Button variant="ghost" size="control" onPress={() => start.mutate()} busy={start.isPending}>New</Button>
      </View>
      <Divider />

      {problem ? <View style={{ padding: space.gutter }}><Banner>{problem}</Banner></View> : null}

      {history.isPending ? (
        <ListSkeleton rows={7} height={60} />
      ) : history.isError ? (
        <View style={{ padding: space.gutter, gap: space.base }}>
          <Banner>{(history.error as Error).message}</Banner>
          <Button variant="outline" onPress={() => void history.refetch()}>Try again</Button>
        </View>
      ) : (
        <View style={{ flex: 1 }}>
          {conversations.map((conversation: ConversationSummary) => (
            <Row
              key={conversation.id}
              conversation={conversation}
              busy={remove.isPending}
              onOpen={() => {
                void Haptics.selectionAsync();
                router.replace({ pathname: "/c/[conversationId]", params: { conversationId: conversation.id } });
              }}
              onDelete={() => remove.mutate(conversation.id)}
            />
          ))}
          {!conversations.length ? (
            <Type size="note" color="muted" style={{ padding: space.loose, textAlign: "center" }}>
              Nothing here yet. Start a conversation and it will show up.
            </Type>
          ) : null}
          {history.hasNextPage ? (
            <Button variant="ghost" onPress={() => void history.fetchNextPage()} busy={history.isFetchingNextPage} style={{ margin: space.gutter }}>
              Load earlier
            </Button>
          ) : null}
        </View>
      )}
    </View>
  );
}

/**
 * One thread, swipeable.
 *
 * Swipe-from-the-right is the gesture iOS has taught everyone for destructive
 * row actions, and a permanent Delete button on every row is both noisier and
 * easier to hit by accident. The confirm step stays: deleting a thread erases
 * every row that points at it, so it asks once rather than offering an undo
 * that would have to outlive the screen.
 *
 * The button is still reachable without the gesture — VoiceOver users and
 * anyone who does not discover the swipe get it from the row's accessibility
 * actions, which is what keeps this from being a hidden-only control.
 */
function Row({ conversation, busy, onOpen, onDelete }: {
  conversation: ConversationSummary;
  busy: boolean;
  onOpen: () => void;
  onDelete: () => void;
}) {
  const styles = useStyles(makeStyles);
  const color = useTheme();
  const [confirming, setConfirming] = useState(false);

  const actions = () => (
    <View style={styles.swipeActions}>
      {confirming ? (
        <>
          <Button size="lg" variant="danger" onPress={onDelete} busy={busy}>Delete</Button>
          <Button size="lg" variant="ghost" onPress={() => setConfirming(false)}>Keep</Button>
        </>
      ) : (
        <Button size="lg" variant="danger" onPress={() => setConfirming(true)}>Delete</Button>
      )}
    </View>
  );

  return (
    <View>
      <Swipeable
        renderRightActions={actions}
        overshootRight={false}
        onSwipeableWillClose={() => setConfirming(false)}
      >
        <Pressable
          onPress={onOpen}
          style={({ pressed }) => [styles.row, pressed && { backgroundColor: color.sunken }]}
          accessibilityActions={[{ name: "delete", label: `Delete conversation: ${conversation.title}` }]}
          onAccessibilityAction={(event) => { if (event.nativeEvent.actionName === "delete") onDelete(); }}
        >
          <View style={{ flex: 1, minWidth: 0 }}>
            <Type size="control" weight="medium" color="ink" numberOfLines={1}>{conversation.title || "Untitled"}</Type>
            <Type size="meta" color="muted">{formatRelative(conversation.updatedAt)}</Type>
          </View>
        </Pressable>
      </Swipeable>
      <Divider />
    </View>
  );
}

const makeStyles = (color: Palette) => StyleSheet.create({
  root: { flex: 1, backgroundColor: color.surface },
  centre: { flex: 1, alignItems: "center", justifyContent: "center" },
  header: { flexDirection: "row", alignItems: "center", paddingHorizontal: space.snug, paddingBottom: space.snug, gap: space.snug },
  row: { flexDirection: "row", alignItems: "center", gap: space.base, paddingHorizontal: space.gutter, paddingVertical: space.base, minHeight: 60 },
  swipeActions: {
    flexDirection: "row",
    alignItems: "center",
    gap: space.snug,
    paddingHorizontal: space.base,
    backgroundColor: color.surface,
  },
});
