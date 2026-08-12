import { ConversationThread } from "@/components/workspace";

export default async function ConversationPage({ params }: PageProps<"/c/[conversationId]">) {
  const { conversationId } = await params;
  return <ConversationThread conversationId={conversationId} />;
}
