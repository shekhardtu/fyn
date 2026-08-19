import { Menu } from "lucide-react";
import { useCallback, useRef, useState, type ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";

export const SITE_HEADER_HEIGHT = 56;

/** One scroll-direction rule for every signed-in page.
 *
 * `scrollTop` increasing means the document is moving upward: reclaim the
 * header. Reversing direction brings it back. Tiny deltas are ignored so track
 * pads, row measurements and browser scroll anchoring cannot make it flicker.
 */
export function useAutoHideSiteHeader() {
  const [headerVisible, setHeaderVisible] = useState(true);
  const previousScrollTop = useRef(0);

  const updateHeaderForScroll = useCallback((scrollTop: number, userDriven = true) => {
    const next = Math.max(0, scrollTop);
    const delta = next - previousScrollTop.current;
    previousScrollTop.current = next;
    if (!userDriven) return;
    if (next <= 8) setHeaderVisible(true);
    else if (Math.abs(delta) >= 4) setHeaderVisible(delta < 0);
  }, []);

  const showHeader = useCallback(() => setHeaderVisible(true), []);
  return { headerVisible, updateHeaderForScroll, showHeader };
}

export function SiteHeader({ title, subtitle, subtitleClassName, hidden = false, navOpen, onOpenNav, end, onRenameTitle }: {
  title: string;
  subtitle: ReactNode;
  subtitleClassName?: string;
  hidden?: boolean;
  navOpen: boolean;
  onOpenNav: () => void;
  end?: ReactNode;
  /** Present only when the title is a thread's name rather than a fixed page
   *  heading: double-clicking it then edits in place. */
  onRenameTitle?: (title: string) => void;
}) {
  const [editingTitle, setEditingTitle] = useState(false);
  const [draft, setDraft] = useState(title);
  const headingRef = useRef<HTMLHeadingElement>(null);
  // Keyboard exits hand focus back to the heading; a pointer exit (blur) has
  // already placed it somewhere else deliberately.
  const refocusHeading = () => requestAnimationFrame(() => headingRef.current?.focus());
  function commitTitle() {
    setEditingTitle(false);
    const next = draft.replace(/\s+/g, " ").trim();
    if (onRenameTitle && next && next !== title) onRenameTitle(next);
  }
  return <header
    inert={hidden ? true : undefined}
    className={cn(
      "sticky top-0 z-20 flex h-14 shrink-0 items-center gap-3 border-b border-line bg-surface px-3 transition-transform duration-200 will-change-transform sm:px-6",
      hidden && "pointer-events-none -translate-y-full",
    )}
  >
    <Button variant="ghost" size="icon-lg" aria-label="Open navigation" aria-expanded={navOpen} aria-controls="conversation-rail" className="rounded-xl md:hidden" onClick={onOpenNav}><Menu size={20} /></Button>
    <div className="min-w-0 flex-1">
      {editingTitle && onRenameTitle
        ? <input
          autoFocus
          value={draft}
          maxLength={160}
          onChange={(event) => setDraft(event.target.value)}
          // The old caption arrives selected: typing replaces, arrows amend.
          onFocus={(event) => event.currentTarget.select()}
          onKeyDown={(event) => {
            if (event.key === "Enter") { commitTitle(); refocusHeading(); }
            if (event.key === "Escape") { setEditingTitle(false); refocusHeading(); }
          }}
          onBlur={commitTitle}
          aria-label={`Rename conversation: ${title}`}
          className="rename-line w-full bg-transparent font-heading text-body font-semibold tracking-[-0.015em] text-ink outline-none"
        />
        : onRenameTitle
          // The hint waits: it teaches the double-click without shouting at
          // every pointer that crosses the title.
          ? <TooltipProvider delay={700}><Tooltip>
            <TooltipTrigger render={
              <h1
                ref={headingRef}
                tabIndex={-1}
                onDoubleClick={() => { setDraft(title); setEditingTitle(true); }}
                // The second press would select a word before the input opens.
                onMouseDown={(event) => { if (event.detail > 1) event.preventDefault(); }}
                className="truncate font-heading text-body font-semibold tracking-[-0.015em] text-ink outline-none"
              />
            }>{title}</TooltipTrigger>
            <TooltipContent>Double-click to rename</TooltipContent>
          </Tooltip></TooltipProvider>
          : <h1 className="truncate font-heading text-body font-semibold tracking-[-0.015em] text-ink">{title}</h1>}
      <p className={cn("text-meta text-ink-muted", subtitleClassName)}>{subtitle}</p>
    </div>
    {end ? <div className="ml-auto shrink-0">{end}</div> : null}
  </header>;
}
