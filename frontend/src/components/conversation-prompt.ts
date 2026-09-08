import { useEffect } from "react";
import { useSearchParams } from "react-router";

/** Navigation suggestions seed an editable draft only after the thread loads. */
export function useConversationPrompt(ready: boolean, onPrefill: (prompt: string) => void) {
  const [params, setParams] = useSearchParams();
  const prompt = params.get("prompt");

  useEffect(() => {
    if (!ready || prompt === null) return;
    // Cancellable work also prevents Strict Mode from consuming a link twice.
    const frame = requestAnimationFrame(() => {
      if (prompt.trim()) onPrefill(prompt.trim());
      setParams((previous) => {
        const next = new URLSearchParams(previous);
        next.delete("prompt");
        return next;
      }, { replace: true, preventScrollReset: true });
    });
    return () => cancelAnimationFrame(frame);
  }, [ready, prompt, onPrefill, setParams]);
}
