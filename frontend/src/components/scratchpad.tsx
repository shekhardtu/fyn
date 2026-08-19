import { GripVertical, NotebookPen, Scaling, X } from "lucide-react";
import { useCallback, useEffect, useLayoutEffect, useRef, useState, type CSSProperties, type KeyboardEvent as ReactKeyboardEvent, type PointerEvent as ReactPointerEvent } from "react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

const STORAGE_PREFIX = "fyn.scratchpad.session.v2";
const TAB_STORAGE_PREFIX = "fyn.scratchpad.tab.v2";
const LEGACY_STORAGE_PREFIX = "fyn.scratchpad.v1";
const SESSION_POINTER_KEY = "fyn.scratchpad.active-session.v1";
const SESSION_COOKIE = "fyn_scratchpad_session";
const VIEWPORT_MARGIN = 12;
const KEYBOARD_MOVE = 12;
const MIN_CARD_WIDTH = 260;
const MIN_CARD_HEIGHT = 240;
const NOTE_LIMIT = 12_000;

type Point = { x: number; y: number };
type Size = { width: number; height: number };
type ScratchpadState = {
  note: string;
  open: boolean;
  position: Point | null;
  size: Size | null;
};

type PointerInteraction = {
  kind: "drag" | "resize";
  pointerId: number;
  startPointer: Point;
  startPosition: Point;
  startSize: Size;
  offset: Point;
  pending: { position: Point; size: Size };
};

const EMPTY_SCRATCHPAD: ScratchpadState = { note: "", open: false, position: null, size: null };

function storageKey(scope: string, sessionId: string) {
  return `${STORAGE_PREFIX}:${sessionId}:${scope}`;
}

function tabStorageKey(scope: string, sessionId: string) {
  return `${TAB_STORAGE_PREFIX}:${sessionId}:${scope}`;
}

function removeSessionData(sessionId: string) {
  const prefix = `${STORAGE_PREFIX}:${sessionId}:`;
  for (let index = localStorage.length - 1; index >= 0; index -= 1) {
    const key = localStorage.key(index);
    if (key?.startsWith(prefix)) localStorage.removeItem(key);
  }
}

function sessionCookie() {
  const prefix = `${SESSION_COOKIE}=`;
  const pair = document.cookie.split("; ").find((item) => item.startsWith(prefix));
  return pair ? decodeURIComponent(pair.slice(prefix.length)) : null;
}

/** A session cookie supplies the shared lifetime that Web Storage lacks:
 *  localStorage can share the note across tabs, while the cookie disappears
 *  when the browser session ends. The next session removes the now-orphaned
 *  local record before creating its own id. */
function browserSessionId() {
  if (typeof window === "undefined") return "server";
  try {
    const current = sessionCookie();
    const previous = localStorage.getItem(SESSION_POINTER_KEY);
    if (current) {
      if (previous && previous !== current) removeSessionData(previous);
      localStorage.setItem(SESSION_POINTER_KEY, current);
      return current;
    }

    if (previous) removeSessionData(previous);
    const id = typeof crypto.randomUUID === "function"
      ? crypto.randomUUID()
      : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
    document.cookie = `${SESSION_COOKIE}=${encodeURIComponent(id)}; Path=/; SameSite=Strict`;
    localStorage.setItem(SESSION_POINTER_KEY, id);
    return id;
  } catch {
    // Storage or cookies may be blocked. The widget still works in this page,
    // but sharing cannot be promised without either browser facility.
    return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  }
}

function validPoint(value: unknown): value is Point {
  if (!value || typeof value !== "object") return false;
  const point = value as Record<string, unknown>;
  return Number.isFinite(point.x) && Number.isFinite(point.y);
}

function validSize(value: unknown): value is Size {
  if (!value || typeof value !== "object") return false;
  const size = value as Record<string, unknown>;
  return Number.isFinite(size.width) && Number.isFinite(size.height);
}

function parseScratchpad(raw: string | null): ScratchpadState {
  if (!raw) return EMPTY_SCRATCHPAD;
  try {
    const saved = JSON.parse(raw) as Record<string, unknown>;
    return {
      note: typeof saved.note === "string" ? saved.note.slice(0, NOTE_LIMIT) : "",
      open: saved.open === true,
      position: validPoint(saved.position) ? saved.position : null,
      size: validSize(saved.size) ? saved.size : null,
    };
  } catch {
    return EMPTY_SCRATCHPAD;
  }
}

function readScratchpad(scope: string, sessionId: string): ScratchpadState {
  if (typeof window === "undefined") return EMPTY_SCRATCHPAD;
  try {
    const key = storageKey(scope, sessionId);
    const saved = localStorage.getItem(key);
    if (saved) {
      const shared = parseScratchpad(saved);
      const tabSaved = sessionStorage.getItem(tabStorageKey(scope, sessionId));
      // The fallback carries the open state and position forward once from the
      // earlier all-shared shape. New writes strip those fields from the shared
      // record so future tabs begin with their own view state.
      const tab = tabSaved ? parseScratchpad(tabSaved) : shared;
      const migrated = { note: shared.note, open: tab.open, position: tab.position, size: tab.size };
      saveScratchpad(scope, sessionId, migrated);
      return migrated;
    }

    // Preserve a note created by the earlier tab-only implementation on the
    // first reload after this shared-session version ships.
    const legacyKey = `${LEGACY_STORAGE_PREFIX}:${scope}`;
    const legacy = sessionStorage.getItem(legacyKey);
    if (!legacy) return EMPTY_SCRATCHPAD;
    const migrated = parseScratchpad(legacy);
    saveScratchpad(scope, sessionId, migrated);
    sessionStorage.removeItem(legacyKey);
    return migrated;
  } catch {
    return EMPTY_SCRATCHPAD;
  }
}

function saveScratchpad(scope: string, sessionId: string, state: ScratchpadState) {
  try {
    // Only content is shared. Open state and coordinates belong to one tab;
    // sharing them lets differently sized windows clamp and overwrite each
    // other, which makes a card dragged to an edge appear to bounce back.
    localStorage.setItem(storageKey(scope, sessionId), JSON.stringify({ note: state.note }));
    sessionStorage.setItem(tabStorageKey(scope, sessionId), JSON.stringify({ open: state.open, position: state.position, size: state.size }));
  } catch {
    // A blocked or full store should never stop the scratchpad working in the
    // current page.
  }
}

function clampPosition(point: Point, width: number, height: number): Point {
  const maxX = Math.max(VIEWPORT_MARGIN, window.innerWidth - width - VIEWPORT_MARGIN);
  const maxY = Math.max(VIEWPORT_MARGIN, window.innerHeight - height - VIEWPORT_MARGIN);
  return {
    x: Math.min(Math.max(point.x, VIEWPORT_MARGIN), maxX),
    y: Math.min(Math.max(point.y, VIEWPORT_MARGIN), maxY),
  };
}

function clampSize(size: Size): Size {
  const availableWidth = Math.max(160, window.innerWidth - VIEWPORT_MARGIN * 2);
  const availableHeight = Math.max(180, window.innerHeight - VIEWPORT_MARGIN * 2);
  const minWidth = Math.min(MIN_CARD_WIDTH, availableWidth);
  const minHeight = Math.min(MIN_CARD_HEIGHT, availableHeight);
  return {
    width: Math.min(Math.max(size.width, minWidth), availableWidth),
    height: Math.min(Math.max(size.height, minHeight), availableHeight),
  };
}

/** A private note shared across this browser session's tabs. The session-cookie
 *  id gates its localStorage record, while open state and position stay
 *  tab-local so different viewport sizes never fight over placement. */
export function Scratchpad({ storageScope }: { storageScope: string }) {
  const [sessionId] = useState(browserSessionId);
  const [state, setState] = useState<ScratchpadState>(() => readScratchpad(storageScope, sessionId));
  const cardRef = useRef<HTMLElement>(null);
  const noteRef = useRef<HTMLTextAreaElement>(null);
  const launcherRef = useRef<HTMLButtonElement>(null);
  const interaction = useRef<PointerInteraction | null>(null);
  const animationFrame = useRef<number | null>(null);
  const previousUserSelect = useRef("");
  const hasOpened = useRef(state.open);

  const update = useCallback((change: (current: ScratchpadState) => ScratchpadState) => {
    setState((current) => {
      const next = change(current);
      saveScratchpad(storageScope, sessionId, next);
      return next;
    });
  }, [sessionId, storageScope]);

  useEffect(() => {
    const key = storageKey(storageScope, sessionId);
    const syncFromAnotherTab = (event: StorageEvent) => {
      if (event.key !== key) return;
      const shared = parseScratchpad(event.newValue);
      setState((current) => ({ ...current, note: shared.note }));
    };
    window.addEventListener("storage", syncFromAnotherTab);
    return () => window.removeEventListener("storage", syncFromAnotherTab);
  }, [sessionId, storageScope]);

  useEffect(() => {
    if (state.open) {
      hasOpened.current = true;
      noteRef.current?.focus();
    } else if (hasOpened.current) {
      launcherRef.current?.focus();
    }
  }, [state.open]);

  const constrainSavedPosition = useCallback(() => {
    const card = cardRef.current;
    if (!card) return;
    const rect = card.getBoundingClientRect();
    const currentPosition = state.position ?? { x: rect.left, y: rect.top };
    const nextSize = state.size ? clampSize(state.size) : null;
    const width = nextSize?.width ?? rect.width;
    const height = nextSize?.height ?? rect.height;
    const nextPosition = clampPosition(currentPosition, width, height);
    if (
      (!state.position || (nextPosition.x === state.position.x && nextPosition.y === state.position.y))
      && (!state.size || (nextSize?.width === state.size.width && nextSize.height === state.size.height))
    ) return;
    update((current) => ({ ...current, position: nextPosition, size: nextSize }));
  }, [state.position, state.size, update]);

  useLayoutEffect(() => {
    if (!state.open) return;
    constrainSavedPosition();
    window.addEventListener("resize", constrainSavedPosition);
    return () => window.removeEventListener("resize", constrainSavedPosition);
  }, [constrainSavedPosition, state.open]);

  // Pointer movement is intentionally imperative. Re-rendering on every mouse
  // or touch event needlessly reconciles the textarea and its full draft. The
  // card paints at most once per frame, then React and storage receive the final
  // geometry only when the gesture ends.
  useEffect(() => {
    const paintPendingGeometry = () => {
      animationFrame.current = null;
      const card = cardRef.current;
      const active = interaction.current;
      if (!card || !active) return;
      const { position, size } = active.pending;
      card.style.left = `${position.x}px`;
      card.style.top = `${position.y}px`;
      card.style.right = "auto";
      card.style.bottom = "auto";
      if (active.kind === "resize") {
        card.style.width = `${size.width}px`;
        card.style.height = `${size.height}px`;
      }
    };

    const move = (event: PointerEvent) => {
      const active = interaction.current;
      if (!active || event.pointerId !== active.pointerId) return;
      if (active.kind === "drag") {
        active.pending = {
          position: clampPosition(
            { x: event.clientX - active.offset.x, y: event.clientY - active.offset.y },
            active.startSize.width,
            active.startSize.height,
          ),
          size: active.startSize,
        };
      } else {
        const deltaX = event.clientX - active.startPointer.x;
        const deltaY = event.clientY - active.startPointer.y;
        const anchoredRight = active.startPosition.x + active.startSize.width >= window.innerWidth - VIEWPORT_MARGIN * 2;
        const anchoredBottom = active.startPosition.y + active.startSize.height >= window.innerHeight - VIEWPORT_MARGIN * 2;
        const requested = {
          width: active.startSize.width + (anchoredRight ? -deltaX : deltaX),
          height: active.startSize.height + (anchoredBottom ? -deltaY : deltaY),
        };
        const size = clampSize(requested);
        const requestedPosition = {
          x: anchoredRight ? active.startPosition.x + active.startSize.width - size.width : active.startPosition.x,
          y: anchoredBottom ? active.startPosition.y + active.startSize.height - size.height : active.startPosition.y,
        };
        active.pending = {
          position: clampPosition(requestedPosition, size.width, size.height),
          size,
        };
      }
      if (animationFrame.current === null) animationFrame.current = window.requestAnimationFrame(paintPendingGeometry);
    };

    const finish = (event: PointerEvent) => {
      const active = interaction.current;
      if (!active || event.pointerId !== active.pointerId) return;
      if (animationFrame.current !== null) {
        window.cancelAnimationFrame(animationFrame.current);
        animationFrame.current = null;
      }
      paintPendingGeometry();
      const final = active.pending;
      interaction.current = null;
      const card = cardRef.current;
      card?.removeAttribute("data-interacting");
      document.body.style.userSelect = previousUserSelect.current;
      update((current) => ({
        ...current,
        position: final.position,
        size: active.kind === "resize" ? final.size : current.size,
      }));
    };

    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", finish);
    window.addEventListener("pointercancel", finish);
    return () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", finish);
      window.removeEventListener("pointercancel", finish);
      if (animationFrame.current !== null) window.cancelAnimationFrame(animationFrame.current);
      animationFrame.current = null;
      const hadInteraction = interaction.current !== null;
      interaction.current = null;
      if (hadInteraction) document.body.style.userSelect = previousUserSelect.current;
    };
  }, [update]);

  const startInteraction = (kind: PointerInteraction["kind"], event: ReactPointerEvent<HTMLElement>) => {
    if (event.button !== 0 || interaction.current) return;
    const card = cardRef.current;
    if (!card) return;
    const rect = card.getBoundingClientRect();
    const startPosition = { x: rect.left, y: rect.top };
    const startSize = { width: rect.width, height: rect.height };
    interaction.current = {
      kind,
      pointerId: event.pointerId,
      startPointer: { x: event.clientX, y: event.clientY },
      startPosition,
      startSize,
      offset: { x: event.clientX - rect.left, y: event.clientY - rect.top },
      pending: { position: startPosition, size: startSize },
    };
    card.style.left = `${rect.left}px`;
    card.style.top = `${rect.top}px`;
    card.style.right = "auto";
    card.style.bottom = "auto";
    card.dataset.interacting = "true";
    previousUserSelect.current = document.body.style.userSelect;
    document.body.style.userSelect = "none";
    event.currentTarget.setPointerCapture?.(event.pointerId);
    event.preventDefault();
  };

  const startDrag = (event: ReactPointerEvent<HTMLElement>) => {
    if ((event.target as HTMLElement).closest("[data-no-drag]")) return;
    startInteraction("drag", event);
  };

  const startResize = (event: ReactPointerEvent<HTMLButtonElement>) => {
    event.stopPropagation();
    startInteraction("resize", event);
  };

  const moveWithKeyboard = (event: ReactKeyboardEvent<HTMLButtonElement>) => {
    const direction: Record<string, Point> = {
      ArrowLeft: { x: -1, y: 0 },
      ArrowRight: { x: 1, y: 0 },
      ArrowUp: { x: 0, y: -1 },
      ArrowDown: { x: 0, y: 1 },
    };
    const delta = direction[event.key];
    const card = cardRef.current;
    if (!delta || !card) return;
    event.preventDefault();
    const rect = card.getBoundingClientRect();
    const step = event.shiftKey ? KEYBOARD_MOVE * 2 : KEYBOARD_MOVE;
    const next = clampPosition({ x: rect.left + delta.x * step, y: rect.top + delta.y * step }, rect.width, rect.height);
    update((current) => ({ ...current, position: next }));
  };

  const resizeWithKeyboard = (event: ReactKeyboardEvent<HTMLButtonElement>) => {
    const delta: Record<string, Size> = {
      ArrowLeft: { width: -1, height: 0 },
      ArrowRight: { width: 1, height: 0 },
      ArrowUp: { width: 0, height: -1 },
      ArrowDown: { width: 0, height: 1 },
    };
    const direction = delta[event.key];
    const card = cardRef.current;
    if (!direction || !card) return;
    event.preventDefault();
    const rect = card.getBoundingClientRect();
    const step = event.shiftKey ? KEYBOARD_MOVE * 2 : KEYBOARD_MOVE;
    const size = clampSize({
      width: rect.width + direction.width * step,
      height: rect.height + direction.height * step,
    });
    const position = clampPosition({ x: rect.left, y: rect.top }, size.width, size.height);
    update((current) => ({ ...current, position, size }));
  };

  if (!state.open) {
    return <Button
      ref={launcherRef}
      type="button"
      size="icon"
      aria-label="Open scratchpad"
      title="Open scratchpad"
      onClick={() => update((current) => ({ ...current, open: true }))}
      className="fixed right-4 bottom-[max(5rem,env(safe-area-inset-bottom))] z-[45] size-12 rounded-full opacity-60 shadow-[var(--shadow-overlay)] transition-opacity duration-[180ms] hover:opacity-100 focus-visible:opacity-100 md:right-6 md:bottom-6"
    >
      <NotebookPen className="size-5!" />
    </Button>;
  }

  const positionStyle: CSSProperties = state.position
    ? { left: state.position.x, top: state.position.y, ...(state.size ?? {}) }
    : { right: VIEWPORT_MARGIN, bottom: VIEWPORT_MARGIN, ...(state.size ?? {}) };

  return <section
    ref={cardRef}
    aria-label="Scratchpad"
    style={positionStyle}
    className={cn(
      "fixed z-[45] flex h-[22rem] max-h-[calc(100dvh-1.5rem)] max-w-[calc(100vw-1.5rem)] w-[min(20rem,calc(100vw-1.5rem))] flex-col overflow-hidden rounded-xl border border-line bg-surface opacity-60 shadow-[var(--shadow-overlay)] transition-opacity duration-[180ms] hover:opacity-100 focus-within:opacity-100 data-[interacting=true]:select-none",
    )}
  >
    <header
      onPointerDown={startDrag}
      className="flex shrink-0 touch-none cursor-grab items-center gap-2 border-b border-secondary-line bg-secondary-tint px-2 py-2 select-none active:cursor-grabbing"
    >
      <Button
        type="button"
        variant="ghost"
        size="icon"
        data-drag-handle
        aria-label="Move scratchpad"
        title="Drag to move. Arrow keys also move it."
        onKeyDown={moveWithKeyboard}
        className="touch-none cursor-grab text-secondary hover:bg-secondary-line/50 hover:text-secondary-hover active:cursor-grabbing"
      >
        <GripVertical size={17} />
      </Button>
      <div className="min-w-0 flex-1">
        <h2 className="font-heading text-control font-semibold text-ink">Scratchpad</h2>
        <p className="text-meta text-ink-muted">Shared in this browser session</p>
      </div>
      <Button data-no-drag type="button" variant="ghost" size="icon" aria-label="Close scratchpad" onClick={() => update((current) => ({ ...current, open: false }))}>
        <X size={16} />
      </Button>
    </header>

    <label htmlFor="scratchpad-note" className="sr-only">Scratchpad note</label>
    <textarea
      ref={noteRef}
      id="scratchpad-note"
      value={state.note}
      maxLength={NOTE_LIMIT}
      onChange={(event) => update((current) => ({ ...current, note: event.target.value }))}
      onKeyDown={(event) => {
        if (event.key === "Escape") update((current) => ({ ...current, open: false }));
      }}
      placeholder="Capture a thought, a number to check, or an idea for later…"
      className="scratchpad-paper min-h-0 flex-1 resize-none border-0 px-5 py-4 text-body text-ink-body outline-none placeholder:text-ink-muted"
    />
    <p className="shrink-0 border-t border-line-soft px-4 py-2 pr-10 text-meta text-ink-muted" aria-live="polite">
      {state.note ? "Saved and shared across open tabs" : "Start typing — your note saves automatically"}
    </p>
    <Button
      data-no-drag
      type="button"
      variant="ghost"
      size="icon-sm"
      aria-label="Resize scratchpad"
      title="Drag to resize. Arrow keys also resize it."
      onPointerDown={startResize}
      onKeyDown={resizeWithKeyboard}
      className="absolute right-1 bottom-1 z-10 touch-none cursor-nwse-resize text-ink-muted hover:text-ink"
    >
      <Scaling size={14} />
    </Button>
  </section>;
}
