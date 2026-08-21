import type { Element as HastElement, ElementContent } from "hast";
import { ArrowDown, ArrowUp, Check, ChevronDown, ChevronsLeftRight, ChevronsRightLeft, ChevronsUpDown, ChevronUp, Copy, Search, X } from "lucide-react";
import { Children, createContext, isValidElement, useContext, useEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode, type RefObject } from "react";
import { Button } from "@/components/ui/button";
import { formatCount } from "@/lib/format";
import { cn } from "@/lib/utils";
import { useTablesWide, WIDE_TABLE_BREAKOUT } from "@/lib/wide-tables";

/** A table longer than this arrives folded. Fifty rows is already more than
 *  anyone reads in one pass, and an answer that ends in two hundred of them
 *  buries whatever the agent said underneath it — the rest are one press away
 *  rather than a page of scrolling on the way back to the composer. */
const FOLD_ABOVE = 50;
/** Under this a table is a short list: no footer, no filter, no row count.
 *  Chrome arrives when the data is big enough to need it, and not before. */
const CHROME_ABOVE = 12;
/** The measure compares rows against each other, so it needs rows to compare. */
const MEASURE_FROM = 5;

type Align = "start" | "center" | "end";
type Sort = { column: number; direction: "asc" | "desc" } | null;

/** What the whole table knows and a single cell cannot work out for itself.
 *  Read once off the parsed Markdown, then used by everything: alignment,
 *  sorting, filtering, the measure, and the clipboard. */
type TableShape = {
  align: Align[];
  figures: boolean[];
  headings: string[];
  /** Body cell text, `[row][column]`, in the order the answer arrived in. */
  values: string[][];
  /** The numeric reading of each cell, or null where there is not one. */
  numbers: (number | null)[][];
  /** The largest value in each column, or null for a column that draws no
   *  measure. Totals are excluded — a total is the sum of the column, so
   *  scaling against it would flatten every real row to nothing. */
  measures: (number | null)[];
  /** Rows that read as a total. They sort to the bottom whatever the order,
   *  because a total adrift in the middle of a table is worse than no total
   *  at all. */
  totals: boolean[];
};

const EMPTY_SHAPE: TableShape = { align: [], figures: [], headings: [], values: [], numbers: [], measures: [], totals: [] };

type Grid = {
  shape: TableShape;
  /** Row indices to render, in render order: filtered, sorted, folded. */
  order: number[];
  sort: Sort;
  onSort: (column: number) => void;
};

const GridContext = createContext<Grid>({ shape: EMPTY_SHAPE, order: [], sort: null, onSort: () => {} });
const ColumnContext = createContext(0);
const RowContext = createContext(-1);

/** Which message the Markdown came from. Set by `MarkdownMessage`; a table
 *  uses it to know itself apart from every other table in the thread. */
export const SourceContext = createContext("");

/** How a reader has left each table.
 *
 *  It cannot live in the component alone: the transcript is virtualised, so a
 *  turn eight rows out of the window is unmounted and mounted again on the way
 *  back. Without this, opening two hundred rows, reading something further up
 *  and returning would find the table folded again and the sort gone — and the
 *  virtualiser would measure the shrunken row and jolt everything below it.
 *
 *  Keyed by the message plus where the table starts inside it, which is stable
 *  across re-parses of the same answer. */
type TableView = { sort: Sort; filter: string; expanded: boolean };
const REMEMBERED = new Map<string, TableView>();

function childElements(node: HastElement | ElementContent | undefined) {
  if (!node || node.type !== "element") return [];
  return node.children.filter((child): child is HastElement => child.type === "element");
}

function textOf(node: ElementContent | undefined): string {
  if (!node) return "";
  if (node.type === "text") return node.value;
  if (node.type !== "element") return "";
  return node.children.map(textOf).join("");
}

/** A cell with nothing to say. Neither a figure nor a word, so it votes on
 *  neither side when a column is being classified, and it sinks to the bottom
 *  of any sort. */
const BLANK = /^(?:|[-–—]|n\/?a|null|none)$/i;

/** A row that is the sum of the ones above it — three tests, not one. The
 *  label has to say so, everything else in the row has to be a figure, and it
 *  has to be the last row, because that is where a total goes. Matching the
 *  word alone would sort "Total Wine & More" to the foot of every table and
 *  exempt it from the fold, which is a bug nobody would think to look for. */
const TOTAL = /^(?:(?:grand\s+)?(?:sub)?total|sum|overall)\b/i;

function readTotals(values: string[][], numbers: (number | null)[][]) {
  const last = values.length - 1;
  const row = values[last];
  const summary = last >= 0
    && TOTAL.test(row[0] ?? "")
    && row.slice(1).some((cell, index) => !BLANK.test(cell) && numbers[last][index + 1] !== null)
    && row.slice(1).every((cell, index) => BLANK.test(cell) || numbers[last][index + 1] !== null);
  return values.map((_, index) => summary && index === last);
}

/** Indian and Western scale suffixes alike: a column can be written in crores
 *  or in millions, and either way the sort has to agree with the eye. */
const SCALES: Record<string, number> = { k: 1e3, m: 1e6, bn: 1e9, cr: 1e7, lakh: 1e5, l: 1e5 };

/** A figure, not a word: what is left once the ornaments come off — a
 *  currency mark or code, grouping separators, a sign, a percent or a scale
 *  suffix, the brackets an accountant writes negatives in — is a plain
 *  number. Deliberately strict about the leftovers, because the cost of a
 *  false positive is a column of prose shoved against the right edge. */
function figureValue(text: string): number | null {
  const trimmed = text.trim();
  if (BLANK.test(trimmed)) return null;
  const suffix = /(k|m|bn|cr|lakh|l)\s*\)?$/i.exec(trimmed);
  const bare = trimmed
    .replace(/[₹$€£¥]|\b(?:inr|usd|eur|gbp|jpy|rs)\b\.?/gi, "")
    .replace(/[(),\u00a0\u202f\s]/g, "")
    .replace(/%$/, "")
    .replace(/(?:k|m|bn|cr|lakh|l)$/i, "")
    .replace(/^[-+−]/, "");
  if (!/^\d+(?:\.\d+)?$/.test(bare)) return null;
  const magnitude = Number(bare) * (suffix ? SCALES[suffix[1].toLowerCase()] ?? 1 : 1);
  return /^[-−]/.test(trimmed) || /^\(.*\)$/.test(trimmed) ? -magnitude : magnitude;
}

/** Four digits in the 1900s or 2000s. A year passes every test for a figure
 *  and is a quantity by none of them: measures drawn against 2026 are all
 *  full. */
function looksLikeYears(values: (number | null)[]) {
  return values.every((value) => value === null || (Number.isInteger(value) && value >= 1900 && value <= 2100));
}

const DECLARED: Record<string, Align> = { left: "start", center: "center", right: "end" };

/** Read the table's shape off the parsed Markdown rather than off the DOM.
 *
 *  Alignment written into the Markdown (`|---:|`) wins, because someone said
 *  it. Where nothing was said the column is classified by what is in it: a
 *  column of money or counts belongs on the right with its digits in columns,
 *  and that is the single thing that separates a table you can read a total
 *  off from one you cannot. */
function readShape(node: HastElement | undefined): TableShape {
  const sections = childElements(node);
  const headRow = childElements(sections.find((section) => section.tagName === "thead"))[0];
  const bodyRows = sections
    .filter((section) => section.tagName === "tbody")
    .flatMap((body) => childElements(body).filter((row) => row.tagName === "tr"));
  const headCells = childElements(headRow);
  const columns = Math.max(headCells.length, ...bodyRows.map((row) => childElements(row).length), 0);

  const values = bodyRows.map((row) => {
    const cells = childElements(row);
    return Array.from({ length: columns }, (_, column) => textOf(cells[column]).trim());
  });
  const numbers = values.map((row) => row.map(figureValue));
  const totals = readTotals(values, numbers);

  const align: Align[] = [];
  const figures: boolean[] = [];
  const measures: (number | null)[] = [];
  for (let column = 0; column < columns; column += 1) {
    const said = headCells[column]?.properties?.align ?? childElements(bodyRows[0])[column]?.properties?.align;
    const spoken = typeof said === "string" ? DECLARED[said] : undefined;
    const filled = values.map((row) => row[column]).filter((cell) => !BLANK.test(cell));
    const figure = filled.length > 0 && filled.every((cell) => figureValue(cell) !== null);
    figures.push(figure);
    align.push(spoken ?? (figure ? "end" : "start"));

    // The measure is drawn against the rows a reader is comparing: real rows,
    // never the total, and only where every one of them is a positive quantity
    // on one shared scale. A column of percentages is skipped — a share is
    // already a proportion, and drawing it twice, once in digits and once in
    // length, is the same fact wearing two hats.
    const compared = values.map((_, row) => totals[row] ? null : numbers[row][column]);
    const present = compared.filter((value): value is number => value !== null);
    const largest = Math.max(0, ...present);
    const measurable = figure
      && present.length >= MEASURE_FROM
      && present.every((value) => value >= 0)
      && largest > 0
      && !filled.every((cell) => /%\s*\)?$/.test(cell))
      && !looksLikeYears(compared);
    measures.push(measurable ? largest : null);
  }
  return { align, figures, headings: headCells.map((cell) => textOf(cell).trim()), values, numbers, measures, totals };
}

/** The nearest ancestor that actually scrolls. Where the table is mounted
 *  decides what its header can pin itself to, and the transcript is only one
 *  of the answers. */
function scrollParent(from: HTMLElement | null) {
  for (let node = from; node && node !== document.body; node = node.parentElement) {
    if (/auto|scroll|overlay/.test(getComputedStyle(node).overflowY)) return node;
  }
  return null;
}

/** Sticky, by hand, because CSS sticky cannot do this here — measured in
 *  Chrome, not assumed.
 *
 *  `position: sticky` resolves against the nearest scroll port, and in the
 *  transcript there is a `transform` in between: the virtualiser positions
 *  every turn with `translateY`. A sticky header inside one parks at its
 *  offset *plus* that translation — hundreds of pixels down the page, exactly
 *  the row's own offset. And the table's horizontal scroller is no way out
 *  either: `overflow-x: auto` makes it a scroll port on both axes, so a header
 *  sticky to it never moves vertically at all.
 *
 *  So the header is moved by hand. Each frame the reader scrolls, this asks
 *  how far the table's top has gone past the page's own pinned edge and
 *  translates the header down by that much, capped so it rides out with the
 *  last row rather than escaping the table. Nothing here goes through React:
 *  it is a transform and three data attributes per frame, on the tables that
 *  are on screen.
 *
 *  The edge it pins under is measured, not configured — whatever the page has
 *  marked `data-sticky-anchor` reports its own bottom, so the header follows
 *  the site header as that hides and returns instead of holding a copy of its
 *  height that would be wrong in both states. */
function usePinnedHeader(frameRef: RefObject<HTMLDivElement | null>) {
  useEffect(() => {
    const frame = frameRef.current;
    const scroller = frame?.querySelector<HTMLElement>("[data-table-scroll]");
    const table = scroller?.querySelector("table");
    const head = table?.querySelector("thead");
    if (!frame || !scroller || !table || !head) return;
    const page = scrollParent(frame.parentElement);
    const anchor = page?.querySelector<HTMLElement>("[data-sticky-anchor]") ?? null;

    let queued = 0;
    let pinned: number | null = null;
    let band: number | null = null;
    let frozen: boolean | null = null;
    let shifted: boolean | null = null;

    const settle = () => {
      queued = 0;
      // Sideways first, and cheapest: whether there is anything past the right
      // edge at all, and whether the reader has gone looking for it.
      const overflows = scroller.scrollWidth - scroller.clientWidth > 1;
      if (overflows !== frozen) { frozen = overflows; frame.dataset.frozen = String(overflows); }
      const away = overflows && scroller.scrollLeft > 1;
      if (away !== shifted) { shifted = away; frame.dataset.shifted = String(away); }

      if (!page) return;
      const port = page.getBoundingClientRect();
      const inset = anchor ? Math.max(0, anchor.getBoundingClientRect().bottom - port.top) : 0;
      const box = table.getBoundingClientRect();
      // The transform never changes the layout, so the table's own box stays
      // the honest measure of how far the header may travel.
      const height = Math.round(head.getBoundingClientRect().height);
      const travel = Math.max(0, box.height - height);
      const offset = Math.round(Math.min(Math.max(port.top + inset - box.top, 0), travel));
      // Both are published: the controls dock under the travelling header, and
      // where that lands is the offset plus the band's own height.
      if (height !== band) { band = height; frame.style.setProperty("--head-h", `${height}px`); }
      if (offset === pinned) return;
      pinned = offset;
      head.style.transform = offset ? `translate3d(0, ${offset}px, 0)` : "";
      frame.dataset.pinned = String(offset > 0);
      frame.style.setProperty("--pin-offset", `${offset}px`);
    };

    const schedule = () => { if (!queued) queued = requestAnimationFrame(settle); };
    settle();

    page?.addEventListener("scroll", schedule, { passive: true });
    scroller.addEventListener("scroll", schedule, { passive: true });
    window.addEventListener("resize", schedule);
    // The site header hides and returns on a 200ms transition. Scrolling keeps
    // this in step for as long as it lasts; this catches the case where the
    // reader stops mid-flight and the anchor keeps moving without them.
    anchor?.addEventListener("transitionend", schedule);
    // Unfolding the rows, filtering them, a font landing, a phone turning
    // sideways: all of it changes where the header may travel to.
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(schedule);
    observer?.observe(table);
    observer?.observe(scroller);

    return () => {
      cancelAnimationFrame(queued);
      page?.removeEventListener("scroll", schedule);
      scroller.removeEventListener("scroll", schedule);
      window.removeEventListener("resize", schedule);
      anchor?.removeEventListener("transitionend", schedule);
      observer?.disconnect();
      head.style.transform = "";
      frame.style.removeProperty("--pin-offset");
      frame.style.removeProperty("--head-h");
    };
  }, [frameRef]);
}

/** The table, back as the Markdown it arrived as — carrying the alignment the
 *  columns are actually drawn with, so what lands in the next editor reads the
 *  way it read here. Cells are escaped, because a pipe inside one would
 *  otherwise open a column that is not there. */
function toMarkdown(shape: TableShape, order: number[]) {
  const rule: Record<Align, string> = { start: "---", center: ":---:", end: "---:" };
  const line = (cells: string[]) => `| ${cells.map((cell) => cell.replace(/\|/g, "\\|")).join(" | ")} |`;
  return [
    line(shape.headings),
    line(shape.align.map((align) => rule[align])),
    ...order.map((row) => line(shape.values[row])),
  ].join("\n");
}

/** What the table is holding back, in one line. It says nothing about the
 *  sort: the header already shows which column is doing it. */
function countRows({ total, matched, shown, query }: { total: number; matched: number; shown: number; query: string }) {
  if (query && matched === 0) return `No rows match “${query}”`;
  if (query) return `${formatCount(matched)} of ${formatCount(total)} rows match “${query}”${shown < matched ? `, showing ${formatCount(shown)}` : ""}`;
  return shown < total ? `Showing ${formatCount(shown)} of ${formatCount(total)} rows` : `All ${formatCount(total)} rows`;
}

/** A Markdown table, read as a table of business figures rather than as
 *  decorated prose: a header and a label column that hold their place, columns
 *  that line up and sort by what is in them, a filter and a fold over anything
 *  too long to read at once, and the whole thing one press from a spreadsheet.
 */
export function MarkdownTable({ node, children }: { node?: HastElement; children?: ReactNode }) {
  const shape = useMemo(() => readShape(node), [node]);
  const source = useContext(SourceContext);
  // No source, no memory: without a message to key on, every table in the app
  // would answer to the same key and inherit the last one's sort.
  const view = source ? `${source}:${node?.position?.start.offset ?? 0}` : null;
  const seen = view ? REMEMBERED.get(view) : undefined;
  const [sort, setSort] = useState<Sort>(seen?.sort ?? null);
  const [filter, setFilter] = useState(seen?.filter ?? "");
  const [expanded, setExpanded] = useState(seen?.expanded ?? false);
  const [copied, setCopied] = useState(false);
  const [wide, setWide] = useTablesWide();
  const frameRef = useRef<HTMLDivElement>(null);
  const copiedTimer = useRef(0);
  usePinnedHeader(frameRef);
  useEffect(() => () => window.clearTimeout(copiedTimer.current), []);
  useEffect(() => { if (view) REMEMBERED.set(view, { sort, filter, expanded }); }, [expanded, filter, sort, view]);

  const total = shape.values.length;
  const query = filter.trim();

  /** Filtered and sorted but not yet folded: the table as the reader has asked
   *  for it, and what the clipboard takes. */
  const matched = useMemo(() => {
    const needle = query.toLowerCase();
    const rows = shape.values.map((_, index) => index);
    const kept = needle ? rows.filter((row) => shape.values[row].some((cell) => cell.toLowerCase().includes(needle))) : rows;
    if (!sort) return kept;
    const direction = sort.direction === "asc" ? 1 : -1;
    return [...kept].sort((left, right) => {
      if (shape.totals[left] !== shape.totals[right]) return shape.totals[left] ? 1 : -1;
      const a = shape.numbers[left][sort.column];
      const b = shape.numbers[right][sort.column];
      // Numbers before words and words before blanks, whichever way the column
      // points: an empty cell is not the smallest value, it is no value.
      if (a !== null && b !== null) return (a - b) * direction;
      if (a !== null) return -1;
      if (b !== null) return 1;
      const textA = shape.values[left][sort.column];
      const textB = shape.values[right][sort.column];
      if (!textA !== !textB) return textA ? -1 : 1;
      return textA.localeCompare(textB) * direction;
    });
  }, [query, shape, sort]);

  const folds = matched.length > FOLD_ABOVE;
  const folded = folds && !expanded;
  const order = useMemo(() => {
    if (!folded) return matched;
    // The fold takes the first fifty rows and never the total: a summary whose
    // one summarising line has been folded away is the wrong fifty rows.
    const rows = matched.filter((row) => !shape.totals[row]).slice(0, FOLD_ABOVE);
    return [...rows, ...matched.filter((row) => shape.totals[row])];
  }, [folded, matched, shape]);

  function sortBy(column: number) {
    setSort((current) => {
      // Figures open largest-first and words A to Z, because that is the
      // question each kind of column is usually being asked.
      const first = shape.figures[column] ? "desc" : "asc";
      if (current?.column !== column) return { column, direction: first };
      if (current.direction === first) return { column, direction: first === "asc" ? "desc" : "asc" };
      // The third press returns the order the answer arrived in — an order the
      // agent chose, and one the reader may want back.
      return null;
    });
  }

  function toggleFold() {
    const folding = expanded;
    setExpanded(!expanded);
    // Folding a hundred rows away from underneath the reader would leave them
    // somewhere past the end of a table that no longer reaches that far. Put
    // the shortened table back in view, and only ever by the smallest move
    // that does it.
    if (folding) requestAnimationFrame(() => frameRef.current?.scrollIntoView({ block: "nearest" }));
  }

  async function copyTable() {
    // Markdown, because that is what it was: the answer wrote a table and the
    // clipboard hands back a table, ready to render wherever it lands rather
    // than arriving as a wall of tabs. What is copied is what the reader asked
    // for — the current filter and order — but never only the folded fifty: an
    // export that silently drops rows is worse than no export.
    const text = toMarkdown(shape, matched);
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      // Cleared before it is replaced, so pressing twice cannot leave an
      // earlier timer to reset the label out from under the later one.
      window.clearTimeout(copiedTimer.current);
      copiedTimer.current = window.setTimeout(() => setCopied(false), 1800);
    } catch {
      setCopied(false);
    }
  }

  const grid = useMemo<Grid>(
    () => ({ shape, order, sort, onSort: sortBy }),
    // `sortBy` closes over nothing that outlives a render, and the provider is
    // only here to keep cells from prop-drilling.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [order, shape, sort],
  );
  const widthLabel = wide ? "Narrow every table back to the message" : "Widen every table to the conversation";
  const copyLabel = copied ? `Copied ${formatCount(matched.length)} rows` : "Copy as Markdown";

  return <GridContext.Provider value={grid}>
    <div ref={frameRef} className={cn("ledger-table", wide && WIDE_TABLE_BREAKOUT)} data-wide={wide || undefined}>
      {/* The table's own controls, in the gutter above it — never on the header
          band, whose column names have to stay readable. They wait for a
          pointer rather than sitting on every table in the thread announcing
          themselves, and once the header pins they dock beneath it so they are
          still within reach fifty rows down. */}
      <div className="ledger-table-tools">
        <button type="button" className="ledger-table-tool" aria-label={copyLabel} title={copyLabel} onClick={copyTable}>
          {copied ? <Check size={13} /> : <Copy size={13} />}
        </button>
        <button type="button" className="ledger-table-tool" data-width data-inline-disclosure="true" aria-pressed={wide} aria-label={widthLabel} title={widthLabel} onClick={() => setWide(!wide)}>
          {wide ? <ChevronsRightLeft size={13} /> : <ChevronsLeftRight size={13} />}
        </button>
      </div>
      <div
        data-table-scroll
        tabIndex={0}
        role="region"
        aria-label={shape.headings.some(Boolean) ? `Table: ${shape.headings.filter(Boolean).join(", ")}` : "Table"}
        className="table-scroll"
      >
        <table>{children}</table>
      </div>
      {total > CHROME_ABOVE ? <div className="ledger-table-foot">
        <p aria-live="polite" className="ledger-table-count">{countRows({ total, matched: matched.length, shown: order.length, query })}</p>
        <div className="ledger-table-controls">
          <label className="ledger-table-filter" data-inline-disclosure="true">
            <Search aria-hidden size={13} />
            <span className="sr-only">Filter rows</span>
            <input
              className="manual-field"
              value={filter}
              placeholder="Filter rows"
              onChange={(event) => setFilter(event.target.value)}
              onKeyDown={(event) => { if (event.key === "Escape" && filter) { event.stopPropagation(); setFilter(""); } }}
            />
            {filter ? <button type="button" aria-label="Clear the filter" onClick={() => setFilter("")}><X size={12} /></button> : null}
          </label>
          {folds ? <Button type="button" variant="ghost" size="sm" data-inline-disclosure="true" aria-expanded={expanded} onClick={toggleFold}>
            {expanded ? <>Show first {FOLD_ABOVE} <ChevronUp /></> : <>Show all {formatCount(matched.length)} <ChevronDown /></>}
          </Button> : null}
        </div>
      </div> : null}
    </div>
  </GridContext.Provider>;
}

export function MarkdownTableBody({ children }: { children?: ReactNode }) {
  // A filter that matches nothing leaves the header standing over no rows, and
  // the footer says so once. There is no second empty state inside the table:
  // the sentence would be the same sentence, and the way out — the clear
  // control — is already sitting beside the words that emptied it.
  const { order } = useContext(GridContext);
  const rows = Children.toArray(children).filter(isValidElement);
  return <tbody>{order.map((row) => rows[row] ? <RowContext.Provider key={row} value={row}>{rows[row]}</RowContext.Provider> : null)}</tbody>;
}

/** Cells cannot count themselves, so the row hands each one its column. */
export function MarkdownTableRow({ children }: { children?: ReactNode }) {
  const { shape } = useContext(GridContext);
  const row = useContext(RowContext);
  const cells = Children.toArray(children).filter(isValidElement);
  return <tr data-total={shape.totals[row] || undefined}>{cells.map((cell, column) => <ColumnContext.Provider key={column} value={column}>{cell}</ColumnContext.Provider>)}</tr>;
}

export function MarkdownTableHeadCell({ children }: { children?: ReactNode }) {
  const { shape, sort, onSort } = useContext(GridContext);
  const column = useContext(ColumnContext);
  const sorted = sort?.column === column ? sort.direction : null;
  return <th
    scope="col"
    data-align={shape.align[column] ?? "start"}
    data-figure={shape.figures[column] || undefined}
    aria-sort={sorted ? (sorted === "asc" ? "ascending" : "descending") : "none"}
  >
    <button type="button" data-inline-disclosure="true" data-sorted={sorted ?? undefined} onClick={() => onSort(column)}>
      <span>{children}</span>
      {sorted === "asc" ? <ArrowUp size={12} /> : sorted === "desc" ? <ArrowDown size={12} /> : <ChevronsUpDown size={12} />}
    </button>
  </th>;
}

export function MarkdownTableCell({ children }: { children?: ReactNode }) {
  const { shape } = useContext(GridContext);
  const column = useContext(ColumnContext);
  const row = useContext(RowContext);
  const largest = shape.measures[column];
  const value = shape.numbers[row]?.[column];
  // The measure: a rule under the figure, as long as the figure is large
  // against the biggest real row in its column. It never replaces or rounds
  // the number — it turns a column of them into a shape the eye can read in
  // one pass — and it stays off the total row, which is not one of the things
  // being compared.
  const measure = largest && typeof value === "number" && !shape.totals[row] ? Math.max(value / largest, 0) : null;
  return <td data-align={shape.align[column] ?? "start"} data-figure={shape.figures[column] || undefined}>
    {children}
    {measure === null ? null : <span aria-hidden className="ledger-measure" style={{ "--measure": measure } as CSSProperties} />}
  </td>;
}
