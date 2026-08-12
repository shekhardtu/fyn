"use client";

import { ArrowLeftRight, ChevronDown, ChevronUp, Download, ExternalLink, Eye, Maximize2, Minimize2, PencilLine, ReceiptText, RotateCcw, Search, Trash2 } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { Button } from "@/components/ui/button";
import { formatCount, formatDay, formatDimension, formatMoney, formatTimestamp } from "@/lib/format";
import type { DataTableColumn, DataTableData, DataTableRowAction, WidgetActionId } from "@/lib/protocol";
import { cn } from "@/lib/utils";

type Row = Record<string, unknown>;

export type DataTableViewProps = {
  data: DataTableData;
  disabled?: boolean;
  pending?: boolean;
  embedded?: boolean;
  onInlineWidthChange?: (expanded: boolean) => void;
  onAction?: (action: WidgetActionId, payload: Record<string, unknown>) => void;
};

const actionIcons: Record<NonNullable<DataTableRowAction["icon"]>, ReactNode> = {
  edit: <PencilLine size={14} />,
  remove: <Trash2 size={14} />,
  view: <Eye size={14} />,
  review: <Search size={14} />,
  download: <Download size={14} />,
  retry: <RotateCcw size={14} />,
  open: <ExternalLink size={14} />,
};

function primitive(value: unknown): string {
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return "";
}

function number(value: unknown): number {
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

function priorityClass(priority: DataTableColumn["priority"]) {
  return priority === "detail" ? "hidden lg:table-cell" : priority === "secondary" ? "hidden sm:table-cell" : "table-cell";
}

function secondaryText(column: DataTableColumn, row: Row) {
  return column.secondaryKeys
    .map((key) => primitive(row[key]))
    .filter(Boolean)
    .join(" · ");
}

function CellValue({ column, row }: { column: DataTableColumn; row: Row }) {
  const value = row[column.key];
  const secondary = secondaryText(column, row);

  if (column.type === "entity") return <div className="flex min-w-[160px] items-center gap-2">
    <span className="grid size-8 shrink-0 place-items-center rounded-xl bg-surface-sunken text-secondary"><ReceiptText size={14} /></span>
    <span className="min-w-0"><span className="block truncate font-medium text-ink">{primitive(value) || "Recorded item"}</span>{secondary ? <span className="mt-0.5 block truncate text-meta text-ink-muted">{secondary}</span> : null}</span>
  </div>;
  if (column.type === "money") return <span className="money whitespace-nowrap font-semibold text-ink">{formatMoney(value, primitive(row[column.currencyKey ?? "currency"]) || "INR")}</span>;
  if (column.type === "date") return <span className="whitespace-nowrap">{formatDay(value)}</span>;
  if (column.type === "datetime") {
    const parsed = new Date(primitive(value));
    return <span className="whitespace-nowrap">{Number.isNaN(parsed.valueOf()) ? primitive(value) : formatTimestamp(parsed)}</span>;
  }
  if (column.type === "number") return <span className="money whitespace-nowrap">{formatCount(number(value))}</span>;
  if (column.type === "percentage") return <span className="money whitespace-nowrap">{formatCount(number(value), 2)}%</span>;
  if (column.type === "boolean") return <span>{value === true ? "Yes" : value === false ? "No" : "—"}</span>;
  if (column.type === "status") return <span className="inline-flex rounded-full bg-surface-sunken px-2 py-1 text-meta font-semibold tracking-wide text-ink-muted uppercase">{formatDimension(value) || "Unknown"}</span>;
  if (column.type === "tags") {
    const tags = Array.isArray(value) ? value.map(primitive).filter(Boolean) : primitive(value) ? [primitive(value)] : [];
    return tags.length ? <span className="flex max-w-[220px] flex-wrap gap-1">{tags.map((tag) => <span key={tag} className="rounded-full bg-secondary-tint px-2 py-0.5 text-meta text-secondary">{tag}</span>)}</span> : <span>—</span>;
  }
  return <span className="block max-w-[280px] truncate">{formatDimension(value) || "—"}{secondary ? <span className="block text-meta text-ink-muted">{secondary}</span> : null}</span>;
}

function permittedActions(data: DataTableData, row: Row) {
  const rawCapabilities = row[data.capabilitiesKey];
  const capabilities = Array.isArray(rawCapabilities) ? rawCapabilities.map(primitive) : [];
  return data.rowActions.filter((action) => !action.capability || capabilities.includes(action.capability));
}

export function DataTableView({ data, disabled, pending, embedded = false, onInlineWidthChange, onAction }: DataTableViewProps) {
  const INITIAL_ROWS = 10;
  const [showAll, setShowAll] = useState(false);
  const [inlineWide, setInlineWide] = useState(false);
  const [maximized, setMaximized] = useState(false);
  const displayedRows = showAll ? data.rows : data.rows.slice(0, INITIAL_ROWS);
  const remaining = Math.max(0, data.rows.length - displayedRows.length);

  useEffect(() => {
    if (!maximized) return;
    const priorOverflow = document.body.style.overflow;
    const close = (event: KeyboardEvent) => { if (event.key === "Escape") setMaximized(false); };
    document.body.style.overflow = "hidden";
    window.addEventListener("keydown", close);
    return () => {
      document.body.style.overflow = priorOverflow;
      window.removeEventListener("keydown", close);
    };
  }, [maximized]);

  useEffect(() => () => onInlineWidthChange?.(false), [onInlineWidthChange]);

  function toggleInlineWidth() {
    const next = !inlineWide;
    setInlineWide(next);
    onInlineWidthChange?.(next);
  }

  function tableSurface(maximizedSurface = false) {
    const showHeader = !embedded || inlineWide || maximizedSurface;
    const controls = maximizedSurface
      ? <Button type="button" variant="ghost" size="icon-sm" onClick={() => setMaximized(false)} aria-label="Restore table from maximized view" title="Restore" className="shrink-0 text-ink-muted hover:text-ink"><Minimize2 /></Button>
      : <div className="flex shrink-0 items-center gap-0.5"><Button type="button" variant="ghost" size="icon-sm" onClick={toggleInlineWidth} aria-pressed={inlineWide} aria-label={inlineWide ? "Use normal table width" : "Use full conversation width"} title={inlineWide ? "Normal width" : "Full conversation width"} className={cn("text-ink-muted hover:text-ink", inlineWide && "bg-surface-sunken text-secondary")}><ArrowLeftRight /></Button><Button type="button" variant="ghost" size="icon-sm" onClick={() => setMaximized(true)} aria-label="Maximize table" title="Maximize" className="text-ink-muted hover:text-ink"><Maximize2 /></Button></div>;
    return <section className={cn(
      maximizedSurface ? "flex h-full min-h-0 w-full flex-col overflow-hidden rounded-lg border border-line bg-surface shadow-[var(--shadow-overlay)]" : (!embedded || inlineWide) && "overflow-hidden rounded-lg border border-line bg-surface",
      inlineWide && !maximizedSurface && !onInlineWidthChange && "relative left-1/2 z-20 w-[calc(100vw-2rem)] max-w-[var(--column-w)] -translate-x-1/2 sm:w-[calc(100vw-3rem)] md:w-[min(var(--column-w),calc(100vw-var(--rail-w)-3rem))]",
    )}>
    {showHeader ? <div className="flex items-start gap-3 border-b border-line px-4 py-4"><div className="min-w-0 flex-1"><h3 className="font-heading text-body font-semibold text-ink">{data.title}</h3>{data.body ? <p className="mt-1 text-note leading-5 text-ink-muted">{data.body}</p> : null}<p className="mt-1 text-meta text-ink-muted">{formatCount(data.rows.length)} record{data.rows.length === 1 ? "" : "s"}</p></div>{controls}</div> : <div className="flex items-center justify-end gap-2 border-b border-line px-3 py-2">{controls}</div>}
    {data.rows.length ? <div tabIndex={0} role="region" aria-label={`${data.title} table; scroll to see more`} className={cn("min-h-0 overflow-auto overscroll-auto focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-secondary", maximizedSurface ? "flex-1" : "max-h-[min(70vh,36rem)]")}><table className="w-full min-w-[520px] text-left text-note">
      <caption className="sr-only">{data.title}</caption>
      <thead className="sticky top-0 z-10 bg-surface shadow-[0_1px_0_var(--line)]"><tr className="border-b border-line text-meta font-semibold tracking-[0.08em] text-ink-muted uppercase">
        {data.columns.map((column) => <th key={column.key} scope="col" className={cn("px-4 py-3 font-semibold", priorityClass(column.priority), column.align === "right" && "text-right")}>{column.label}</th>)}
        {data.rowActions.length ? <th scope="col" className="px-4 py-3 text-right font-semibold"><span className="sr-only">Actions</span></th> : null}
      </tr></thead>
      <tbody className="divide-y divide-line">{displayedRows.map((row, rowIndex) => {
        const rowActions = permittedActions(data, row);
        return <tr key={primitive(row[data.rowIdKey]) || String(rowIndex)} className="transition-colors hover:bg-surface-sunken/60">
          {data.columns.map((column) => <td key={column.key} className={cn("px-4 py-3 text-ink-muted", priorityClass(column.priority), column.align === "right" && "text-right")}><CellValue column={column} row={row} /></td>)}
          {data.rowActions.length ? <td className="px-4 py-3"><div className="flex justify-end gap-2">{rowActions.map((action) => {
            const destructive = action.style === "danger" || action.icon === "remove";
            const resource = row[action.resourceKey];
            return <Button key={action.id} type="button" variant="outline" size="sm" disabled={disabled || pending || resource == null || !onAction} aria-label={action.label} title={action.label} onClick={() => onAction?.(action.action, { [action.payloadKey]: resource })} className={cn(destructive && "border-danger-line text-danger-ink hover:bg-danger-tint")}>
              {action.icon ? actionIcons[action.icon] : null}<span className="hidden sm:inline">{action.label}</span>
            </Button>;
          })}</div></td> : null}
        </tr>;
      })}</tbody>
    </table></div> : <p className="px-4 py-6 text-center text-note leading-5 text-ink-muted">{data.emptyMessage}</p>}
    {data.rows.length ? <div className="flex flex-wrap items-center gap-2 border-t border-line bg-surface px-4 py-3"><p className="mr-auto text-meta text-ink-muted">Showing <span className="font-semibold text-ink-body">{formatCount(displayedRows.length)}</span> of {formatCount(data.rows.length)}{remaining ? ` · ${formatCount(remaining)} remaining` : ""}</p>{data.rows.length > INITIAL_ROWS ? <Button type="button" variant="outline" size="sm" onClick={() => setShowAll((current) => !current)} aria-expanded={showAll}>{showAll ? <ChevronUp size={14} /> : <ChevronDown size={14} />}{showAll ? "Show first 10" : `Show all ${formatCount(data.rows.length)}`}</Button> : null}</div> : null}
  </section>;
  }

  return <>
    {tableSurface()}
    {maximized && typeof document !== "undefined" ? createPortal(<div className="fixed inset-0 z-[100] bg-ink/45 p-2 backdrop-blur-sm sm:p-4" onMouseDown={(event) => { if (event.target === event.currentTarget) setMaximized(false); }}><div role="dialog" aria-modal="true" aria-label={`${data.title} maximized table`} className="h-full w-full">{tableSurface(true)}</div></div>, document.body) : null}
  </>;
}
