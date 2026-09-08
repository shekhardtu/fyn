import { Combobox as BaseCombobox } from "@base-ui/react/combobox";
import { Check, ChevronDown, Plus, Search } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import { cn } from "@/lib/utils";

export type ComboboxOption = { value: string; label: string };

/** An option row, or the synthetic "Add …" row appended while searching. */
type Row = ComboboxOption & { create?: boolean };

const CREATE = "\0create";

/**
 * The one dropdown in the product. A native `<select>` answers with the
 * browser's own menu; this one answers with the app's — searchable, keyboard
 * navigable, and able to grow ("Add …") where the caller allows it.
 *
 * The search row appears when the list is long enough to need it or when the
 * dropdown is creatable, because typing is then the whole point. Filtering is
 * done here rather than by Base UI so the create row can be appended to the
 * results as just another row the keyboard can reach.
 */
export function Combobox({ value, onValueChange, options, placeholder = "Choose…", disabled, searchable, searchPlaceholder = "Search…", emptyMessage = "No matches.", onCreate, createHint, triggerClassName, displayValue, "aria-label": ariaLabel }: {
  value: string;
  onValueChange: (value: string) => void;
  options: ComboboxOption[];
  placeholder?: string;
  disabled?: boolean;
  /** Defaults to "when it earns its place": lists past 5 rows, or creatable ones. */
  searchable?: boolean;
  searchPlaceholder?: string;
  emptyMessage?: string;
  /** When present, a query matching nothing exactly offers an "Add “…”" row. */
  onCreate?: (name: string) => void;
  /** One quiet line under the create row's label, e.g. "as a new subcategory". */
  createHint?: string;
  triggerClassName?: string;
  /** A short selected value while the menu retains descriptive option labels. */
  displayValue?: string;
  "aria-label": string;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const selected = useMemo(() => options.find((option) => option.value === value) ?? null, [options, value]);
  const showSearch = searchable ?? (options.length > 5 || Boolean(onCreate));

  const rows = useMemo<Row[]>(() => {
    const needle = query.trim().toLocaleLowerCase();
    const matches: Row[] = needle ? options.filter((option) => option.label.toLocaleLowerCase().includes(needle)) : [...options];
    const name = query.trim();
    if (onCreate && name && !options.some((option) => option.label.toLocaleLowerCase() === needle)) matches.push({ value: CREATE, label: name, create: true });
    return matches;
  }, [onCreate, options, query]);

  // The transaction drawer closes itself on a document-level Escape. When the
  // popup is open, Escape belongs to the popup alone — intercept it in the
  // capture phase so one press closes the menu and a second closes the drawer.
  useEffect(() => {
    if (!open) return;
    function onKeyDown(event: KeyboardEvent) {
      if (event.key !== "Escape") return;
      event.stopPropagation();
      setOpen(false);
    }
    document.addEventListener("keydown", onKeyDown, true);
    return () => document.removeEventListener("keydown", onKeyDown, true);
  }, [open]);

  return <BaseCombobox.Root<Row>
    items={rows}
    filter={null}
    autoHighlight
    disabled={disabled}
    open={open}
    onOpenChange={(next) => { setOpen(next); if (!next) setQuery(""); }}
    inputValue={query}
    onInputValueChange={setQuery}
    value={selected}
    isItemEqualToValue={(left, right) => left.value === right.value}
    onValueChange={(next: Row | null) => {
      if (!next) return;
      if (next.create) { onCreate?.(next.label); return; }
      onValueChange(next.value);
    }}
  >
    <BaseCombobox.Trigger
      aria-label={ariaLabel}
      className={cn(
        "group/trigger flex h-[var(--h-field)] w-full items-center justify-between gap-2 rounded-lg border border-line-strong bg-surface px-3 text-left text-control text-ink transition-colors duration-[110ms] ease-linear",
        "data-[popup-open]:border-secondary disabled:opacity-60",
        triggerClassName,
      )}
    >
      <BaseCombobox.Value>
        {(current: Row | null) => current ? <span className="truncate">{displayValue ?? current.label}</span> : <span className="truncate text-ink-muted">{placeholder}</span>}
      </BaseCombobox.Value>
      <ChevronDown aria-hidden className="size-3.5 shrink-0 text-ink-muted transition-transform duration-[var(--m-enter)] ease-[var(--ease)] group-data-[popup-open]/trigger:rotate-180" />
    </BaseCombobox.Trigger>

    <BaseCombobox.Portal>
      <BaseCombobox.Positioner sideOffset={5} align="start" className="z-[60] outline-none">
        {/* On touch, leaving focus alone keeps the keyboard down until the
            search line is actually tapped — a list you can scan with a thumb
            first, not a keyboard shoved over half of it. */}
        <BaseCombobox.Popup initialFocus={(openType) => openType !== "touch"} className="combobox-popup flex max-h-[min(21rem,var(--available-height))] w-max min-w-[max(var(--anchor-width),11rem)] max-w-[min(20rem,var(--available-width))] flex-col overflow-hidden rounded-lg border border-line bg-surface text-ink shadow-[var(--shadow-overlay)]">
          {/* `data-field`: the popup border already frames the search line, so the
              input's own global focus ring would draw a second box inside it. */}
          {showSearch ? <div data-field className="flex shrink-0 items-center gap-2 border-b border-line px-3">
            <Search aria-hidden className="size-3.5 shrink-0 text-ink-muted" />
            <BaseCombobox.Input placeholder={searchPlaceholder} className="h-10 w-full bg-transparent text-control text-ink outline-none placeholder:text-ink-muted" />
          </div> : null}
          <BaseCombobox.Empty className="text-note text-ink-muted not-empty:px-3 not-empty:py-5 not-empty:text-center">{query.trim() ? emptyMessage : null}</BaseCombobox.Empty>
          <BaseCombobox.List className="panel-scroll min-h-0 flex-1 overflow-y-auto not-empty:p-1">
            {(item: Row) => item.create
              ? <div key={CREATE}>
                {rows.length > 1 ? <div aria-hidden className="mx-1 my-1 border-t border-line" /> : null}
                <BaseCombobox.Item value={item} className="grid cursor-pointer grid-cols-[1rem_minmax(0,1fr)] items-center gap-2 rounded-md py-2 pr-3 pl-1.5 text-control font-medium text-secondary select-none data-[highlighted]:bg-secondary-tint data-[highlighted]:text-secondary-hover">
                  <Plus aria-hidden className="size-3.5" />
                  <span className="truncate">Add “{item.label}”{createHint ? <span className="block truncate text-meta font-normal text-ink-muted">{createHint}</span> : null}</span>
                </BaseCombobox.Item>
              </div>
              : <BaseCombobox.Item key={item.value} value={item} className="grid cursor-pointer grid-cols-[1rem_minmax(0,1fr)] items-center gap-2 rounded-md py-2 pr-3 pl-1.5 text-control text-ink-body select-none data-[highlighted]:bg-secondary-tint data-[highlighted]:text-secondary-hover data-[selected]:font-medium data-[selected]:text-ink">
                {/* The check column is drawn by hand rather than with ItemIndicator:
                    the indicator unmounts on unselected rows, which collapsed every
                    label into the 1rem first grid column. */}
                <span aria-hidden className="grid size-4 place-items-center">{item.value === value ? <Check className="size-3.5" /> : null}</span>
                <span className="truncate">{item.label}</span>
              </BaseCombobox.Item>}
          </BaseCombobox.List>
        </BaseCombobox.Popup>
      </BaseCombobox.Positioner>
    </BaseCombobox.Portal>
  </BaseCombobox.Root>;
}
