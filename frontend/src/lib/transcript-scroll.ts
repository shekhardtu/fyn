/**
 * Resolve an element's block-start position inside one specific scroller.
 *
 * `Element.scrollIntoView()` is deliberately avoided for transcript controls:
 * it can move every scrollable ancestor, and a smooth target becomes stale
 * while virtualized rows are exchanging estimates for measured heights. The
 * caller can instead give this offset to the transcript virtualizer, keeping a
 * single owner for both positioning and measurement reconciliation.
 */
export function transcriptElementOffset(scroller: HTMLElement, target: HTMLElement) {
  const scrollerRect = scroller.getBoundingClientRect();
  const targetRect = target.getBoundingClientRect();
  const margin = Number.parseFloat(window.getComputedStyle(target).scrollMarginTop) || 0;
  const rawOffset = scroller.scrollTop + targetRect.top - scrollerRect.top - margin;
  const maxOffset = Math.max(0, scroller.scrollHeight - scroller.clientHeight);
  return Math.min(Math.max(0, rawOffset), maxOffset);
}

/**
 * Keep a newly submitted turn one viewport tall while its answer is forming.
 * The blank remainder shrinks one-for-one as content arrives, so the physical
 * end of the scroller stays at the prompt instead of moving on every token.
 * Once the answer fills the viewport the reserve reaches zero and ordinary
 * end-following resumes naturally.
 */
export function focusedTurnSpacerHeight(viewportHeight: number, topInset: number, turnContentHeight: number) {
  if (![viewportHeight, topInset, turnContentHeight].every(Number.isFinite)) return 0;
  return Math.max(0, viewportHeight - Math.max(0, topInset) - Math.max(0, turnContentHeight));
}
