import { Menu } from "lucide-react";
import { useCallback, useRef, useState, type ReactNode } from "react";
import { Button } from "@/components/ui/button";
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

export function SiteHeader({ title, subtitle, subtitleClassName, hidden = false, navOpen, onOpenNav, end }: {
  title: string;
  subtitle: ReactNode;
  subtitleClassName?: string;
  hidden?: boolean;
  navOpen: boolean;
  onOpenNav: () => void;
  end?: ReactNode;
}) {
  return <header
    inert={hidden ? true : undefined}
    className={cn(
      "sticky top-0 z-20 flex h-14 shrink-0 items-center gap-3 border-b border-line bg-surface px-3 transition-transform duration-200 will-change-transform sm:px-6",
      hidden && "pointer-events-none -translate-y-full",
    )}
  >
    <Button variant="ghost" size="icon-lg" aria-label="Open navigation" aria-expanded={navOpen} aria-controls="conversation-rail" className="rounded-xl md:hidden" onClick={onOpenNav}><Menu size={20} /></Button>
    <div className="min-w-0 flex-1">
      <h1 className="truncate font-heading text-body font-semibold tracking-[-0.015em] text-ink">{title}</h1>
      <p className={cn("text-meta text-ink-muted", subtitleClassName)}>{subtitle}</p>
    </div>
    {end ? <div className="ml-auto shrink-0">{end}</div> : null}
  </header>;
}
