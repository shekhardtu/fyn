import { FolderTree, Info, Lightbulb, Loader2, PencilLine, Plus, Trash2 } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Combobox } from "@/components/ui/combobox";
import { toast, UNDO_WINDOW_MS } from "@/components/ui/toast";
import { flushCategoryDeletion, flushHintDeletion, flushSubcategoryDeletion } from "@/lib/api";
import { formatCount, formatMoney } from "@/lib/format";
import type { CategoryDirectoryOut, CategoryDirectorySubcategoryOut, TransactionCategoryHintOut } from "@/lib/protocol";
import { cn } from "@/lib/utils";

type Editor =
  | { kind: "category"; id: string | null; name: string }
  | { kind: "subcategory"; id: string | null; name: string }
  | { kind: "hint"; id: string | null; merchant: string; subcategoryId: string };

type DeleteTarget =
  | { kind: "category"; id: string; label: string }
  | { kind: "subcategory"; id: string; label: string; categoryId: string }
  | { kind: "hint"; id: string; label: string; categoryId: string };

export type CategoryUsage = {
  amountMinor: number;
  count: number;
  sharePercent: number;
  subcategories: Map<string, { amountMinor: number; count: number }>;
};

export function CategoryManager({ categories, usage, currency, onCreateCategory, onRenameCategory, onDeleteCategory, onCreateSubcategory, onRenameSubcategory, onDeleteSubcategory, onCreateHint, onUpdateHint, onDeleteHint }: {
  categories: CategoryDirectoryOut[];
  usage: Map<string, CategoryUsage>;
  currency: string;
  onCreateCategory: (name: string) => Promise<CategoryDirectoryOut>;
  onRenameCategory: (id: string, name: string) => Promise<unknown>;
  onDeleteCategory: (id: string) => Promise<unknown>;
  onCreateSubcategory: (categoryId: string, name: string) => Promise<CategoryDirectorySubcategoryOut>;
  onRenameSubcategory: (categoryId: string, id: string, name: string) => Promise<unknown>;
  onDeleteSubcategory: (categoryId: string, id: string) => Promise<unknown>;
  onCreateHint: (categoryId: string, merchant: string, subcategoryId: string | null) => Promise<TransactionCategoryHintOut>;
  onUpdateHint: (categoryId: string, id: string, merchant: string, subcategoryId: string | null) => Promise<unknown>;
  onDeleteHint: (categoryId: string, id: string) => Promise<unknown>;
}) {
  const [selectedId, setSelectedId] = useState(categories[0]?.id ?? "");
  const [editor, setEditor] = useState<Editor | null>(null);
  const [saving, setSaving] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  // A delete that would be refused server-side is explained instead of sent.
  const [blocked, setBlocked] = useState<{ label: string; count: number } | null>(null);
  // Deleting takes effect at once and the undo slip is where it can be taken
  // back — the same contract as deleting a conversation. Withheld rows hide
  // while their window runs; the API call happens when the slip closes.
  const [withheld, setWithheld] = useState<string[]>([]);
  const pendingDeletes = useRef(new Map<string, { target: DeleteTarget; undone: boolean }>());
  const visibleCategories = useMemo(() => categories.filter((category) => !withheld.includes(category.id)), [categories, withheld]);
  const selected = visibleCategories.find((category) => category.id === selectedId) ?? visibleCategories[0];
  const selectedUsage = selected ? usage.get(selected.id) : undefined;

  const total = useMemo(() => [...usage.values()].reduce((sum, item) => sum + item.amountMinor, 0), [usage]);

  // Held in a ref because the toast calls this long after the render that
  // armed it.
  const resolve = useRef<(id: string) => void>(() => {});
  useEffect(() => {
    resolve.current = (id: string) => {
      const removal = pendingDeletes.current.get(id);
      if (!removal) return;
      pendingDeletes.current.delete(id);
      if (removal.undone) {
        setWithheld((current) => current.filter((item) => item !== id));
        return;
      }
      const { target } = removal;
      const call = target.kind === "category" ? onDeleteCategory(target.id)
        : target.kind === "subcategory" ? onDeleteSubcategory(target.categoryId, target.id)
        : onDeleteHint(target.categoryId, target.id);
      void call.catch((cause: unknown) => {
        setProblem(cause instanceof Error ? cause.message : `${target.label} could not be deleted.`);
      }).finally(() => {
        setWithheld((current) => current.filter((item) => item !== id));
      });
    };
  });

  // A delete you walked away from is still a delete: flush anything inside
  // its window before the page goes, rather than silently keeping it.
  useEffect(() => {
    const flush = () => pendingDeletes.current.forEach((removal) => {
      if (removal.undone) return;
      const { target } = removal;
      if (target.kind === "category") flushCategoryDeletion(target.id);
      else if (target.kind === "subcategory") flushSubcategoryDeletion(target.categoryId, target.id);
      else flushHintDeletion(target.categoryId, target.id);
    });
    window.addEventListener("pagehide", flush);
    return () => window.removeEventListener("pagehide", flush);
  }, []);

  function beginDelete(target: DeleteTarget) {
    setProblem(null);
    setBlocked(null);
    setEditor(null);
    pendingDeletes.current.set(target.id, { target, undone: false });
    setWithheld((current) => [...current, target.id]);
    toast.add({
      id: target.id,
      title: "Deleted",
      description: target.label,
      timeout: UNDO_WINDOW_MS,
      actionProps: {
        children: "Undo",
        onClick: () => {
          const removal = pendingDeletes.current.get(target.id);
          if (removal) removal.undone = true;
          toast.close(target.id);
        },
      },
      onClose: () => resolve.current(target.id),
    });
  }

  function requestDelete(target: DeleteTarget, inUseCount: number) {
    if (inUseCount > 0) {
      setEditor(null);
      setBlocked({ label: target.label, count: inUseCount });
      return;
    }
    beginDelete(target);
  }

  async function submit() {
    if (!editor || !selected) return;
    const name = editor.kind === "hint" ? editor.merchant.trim() : editor.name.trim();
    if (!name) { setProblem(editor.kind === "hint" ? "Enter a merchant name or phrase." : "Enter a name."); return; }
    setSaving(true); setProblem(null);
    try {
      if (editor.kind === "category") {
        if (editor.id) await onRenameCategory(editor.id, name);
        else {
          const created = await onCreateCategory(name);
          setSelectedId(created.id);
        }
      } else if (editor.kind === "subcategory") {
        if (editor.id) await onRenameSubcategory(selected.id, editor.id, name);
        else await onCreateSubcategory(selected.id, name);
      } else if (editor.id) {
        await onUpdateHint(selected.id, editor.id, name, editor.subcategoryId || null);
      } else {
        await onCreateHint(selected.id, name, editor.subcategoryId || null);
      }
      setEditor(null);
    } catch (cause) {
      setProblem(cause instanceof Error ? cause.message : "That change could not be saved.");
    } finally {
      setSaving(false);
    }
  }

  if (!selected) return <section className="rounded-xl border border-line bg-surface px-6 py-12 text-center"><FolderTree className="mx-auto text-secondary" /><h2 className="mt-4 font-heading text-title font-semibold text-ink">Create your first category</h2><Button type="button" className="mt-5" onClick={() => setEditor({ kind: "category", id: null, name: "" })}><Plus /> Add category</Button></section>;

  const inputClass = "manual-field h-10 w-full rounded-lg border border-line-strong bg-surface px-3 text-control text-ink outline-none";
  return <section aria-labelledby="taxonomy-manager-title" className="overflow-hidden rounded-xl border border-line bg-line">
    <div className="flex items-center justify-between gap-4 bg-surface px-5 py-4 sm:px-6">
      <div><p className="ledger-meta">Taxonomy manager</p><h2 id="taxonomy-manager-title" className="mt-1 font-heading text-title font-semibold text-ink">Categories, subcategories and hints</h2></div>
      <Button type="button" onClick={() => { setProblem(null); setBlocked(null); setEditor({ kind: "category", id: null, name: "" }); }}><Plus /> Add category</Button>
    </div>

    <div className="grid gap-px lg:grid-cols-[0.78fr_1.22fr]">
      <div className="bg-surface p-2 sm:p-3">
        <div role="group" className="space-y-1" aria-label="Expense categories">
          {categories.map((category) => {
            const active = category.id === selected.id;
            const metric = usage.get(category.id);
            return <button key={category.id} type="button" aria-pressed={active} onClick={() => { setSelectedId(category.id); setEditor(null); setBlocked(null); setProblem(null); }} className={cn("group relative w-full rounded-lg px-3 py-3 text-left transition-colors hover:bg-surface-sunken", active && "bg-surface-sunken")}>
              <span aria-hidden className="absolute inset-y-2 left-0 w-0.5 rounded-full bg-secondary opacity-0 group-aria-pressed:opacity-100" />
              <span className="flex items-center justify-between gap-3"><span className="truncate text-control font-medium text-ink" title={category.label}>{category.label}</span><span className="text-note text-ink-muted tabular-nums">{formatMoney(metric?.amountMinor ?? 0, currency)}</span></span>
              <span className="mt-1 block text-meta text-ink-muted">{category.subcategories.length} subcategories · {category.hints.length} hints</span>
            </button>;
          })}
        </div>
      </div>

      <div className="bg-ground px-5 py-5 sm:px-6">
        <div className="flex items-start justify-between gap-4">
          <div><p className="ledger-meta">Selected category</p><h3 className="mt-1 font-heading text-[1.35rem] font-semibold tracking-[-0.03em] text-ink">{selected.label}</h3><p className="mt-1 text-note text-ink-muted">{selected.editable ? "Custom category" : "Built-in category"} · {selectedUsage?.count ?? 0} transactions · {formatCount(total ? (selectedUsage?.amountMinor ?? 0) / total * 100 : 0, 1)}%</p></div>
          {selected.editable ? <div className="flex gap-1"><Button type="button" variant="ghost" size="icon" aria-label={`Rename ${selected.label}`} onClick={() => { setProblem(null); setBlocked(null); setEditor({ kind: "category", id: selected.id, name: selected.label }); }}><PencilLine /></Button><Button type="button" variant="ghost" size="icon" aria-label={`Delete ${selected.label}`} onClick={() => requestDelete({ kind: "category", id: selected.id, label: selected.label }, selectedUsage?.count ?? 0)}><Trash2 /></Button></div> : <span className="rounded-md border border-line bg-surface px-2 py-1 text-meta text-ink-muted">Protected</span>}
        </div>

        {problem ? <div role="alert" className="mt-4 flex items-start justify-between gap-3 rounded-lg border border-danger-line bg-danger-tint px-4 py-3 text-note text-danger-ink">
          <span>{problem}</span>
          <Button type="button" variant="ghost" className="h-7 shrink-0 text-danger-ink" onClick={() => setProblem(null)}>Dismiss</Button>
        </div> : null}
        {blocked ? <div role="status" className="mt-4 flex items-start gap-3 rounded-lg border border-line bg-surface px-4 py-3 text-note text-ink-body">
          <Info className="mt-0.5 shrink-0 text-secondary" />
          <span className="min-w-0 flex-1"><strong>{blocked.label}</strong> is categorizing {formatCount(blocked.count)} transaction{blocked.count === 1 ? "" : "s"}, so it can’t be deleted. Recategorize those transactions first, then delete it here.</span>
          <Button type="button" variant="ghost" className="h-7 shrink-0" onClick={() => setBlocked(null)}>Got it</Button>
        </div> : null}

        {editor ? <div className="mt-4 rounded-lg border border-secondary-line bg-secondary-tint/45 p-4">
          <p className="text-note font-semibold text-ink">{editor.id ? "Edit" : "Add"} {editor.kind === "hint" ? "transaction hint" : editor.kind}</p>
          <div className="mt-3 flex flex-col gap-3 sm:flex-row sm:items-center">
            {editor.kind === "hint" ? <><input autoFocus aria-label="Merchant hint" placeholder="e.g. Swiggy or Uber" value={editor.merchant} onChange={(event) => setEditor({ ...editor, merchant: event.target.value })} className={inputClass} /><div className="sm:w-52"><Combobox aria-label="Hint subcategory" value={editor.subcategoryId} onValueChange={(subcategoryId) => setEditor({ ...editor, subcategoryId })} options={[{ value: "", label: `Any ${selected.label}` }, ...selected.subcategories.map((item) => ({ value: item.id, label: item.label }))]} triggerClassName="h-10 bg-surface" /></div></> : <input autoFocus aria-label={`${editor.kind} name`} value={editor.name} onChange={(event) => setEditor({ ...editor, name: event.target.value })} maxLength={80} className={inputClass} />}
            <div className="flex shrink-0 items-center gap-2"><Button type="button" className="h-10" disabled={saving} onClick={submit}>{saving ? <Loader2 className="animate-spin" /> : null} Save</Button><Button type="button" className="h-10" variant="ghost" disabled={saving} onClick={() => setEditor(null)}>Cancel</Button></div>
          </div>
        </div> : null}

        <div className="mt-6 flex items-center justify-between"><div><p className="ledger-meta">Subcategories</p><p className="mt-1 text-note text-ink-muted">Organize transactions inside {selected.label}.</p></div><Button type="button" variant="outline" onClick={() => { setProblem(null); setBlocked(null); setEditor({ kind: "subcategory", id: null, name: "" }); }}><Plus /> Add subcategory</Button></div>
        {selected.subcategories.filter((subcategory) => !withheld.includes(subcategory.id)).length === 0 ? <div className="mt-3 rounded-lg border border-dashed border-line-strong bg-surface px-4 py-6 text-center text-note text-ink-muted">No subcategories yet. Add one to split {selected.label} into finer groups.</div> : <div className="mt-3 divide-y divide-line overflow-hidden rounded-lg border border-line bg-surface">
          {selected.subcategories.filter((subcategory) => !withheld.includes(subcategory.id)).map((subcategory) => { const metric = selectedUsage?.subcategories.get(subcategory.id); return <div key={subcategory.id} className="flex min-h-12 items-center gap-3 px-4 py-2.5"><div className="min-w-0 flex-1"><p className="truncate text-control font-medium text-ink-body">{subcategory.label}</p><p className="text-meta text-ink-muted">{metric?.count ?? 0} transactions · {formatMoney(metric?.amountMinor ?? 0, currency)}</p></div>{subcategory.editable ? <><Button type="button" variant="ghost" size="icon" aria-label={`Rename ${subcategory.label}`} onClick={() => { setProblem(null); setBlocked(null); setEditor({ kind: "subcategory", id: subcategory.id, name: subcategory.label }); }}><PencilLine /></Button><Button type="button" variant="ghost" size="icon" aria-label={`Delete ${subcategory.label}`} onClick={() => requestDelete({ kind: "subcategory", id: subcategory.id, label: subcategory.label, categoryId: selected.id }, metric?.count ?? 0)}><Trash2 /></Button></> : <span className="text-meta text-ink-muted">Built-in</span>}</div>; })}
        </div>}

        <div className="mt-7 flex items-center justify-between"><div><p className="ledger-meta">Transaction hints</p><p className="mt-1 text-note text-ink-muted">Teach fyn AI where merchants belong.</p></div><Button type="button" variant="outline" onClick={() => { setProblem(null); setBlocked(null); setEditor({ kind: "hint", id: null, merchant: "", subcategoryId: "" }); }}><Lightbulb /> Add hint</Button></div>
        {selected.hints.filter((hint) => !withheld.includes(hint.id)).length ? <div className="mt-3 divide-y divide-line overflow-hidden rounded-lg border border-line bg-surface">{selected.hints.filter((hint) => !withheld.includes(hint.id)).map((hint) => <div key={hint.id} className="flex min-h-12 items-center gap-3 px-4 py-2.5"><Lightbulb className="shrink-0 text-secondary" /><div className="min-w-0 flex-1"><p className="truncate text-control font-medium text-ink-body">Merchant matches “{hint.merchant}”</p><p className="text-meta text-ink-muted">Assign {selected.label}{hint.subcategory ? ` → ${hint.subcategory}` : ""}</p></div><Button type="button" variant="ghost" size="icon" aria-label={`Edit ${hint.merchant} hint`} onClick={() => { setProblem(null); setBlocked(null); setEditor({ kind: "hint", id: hint.id, merchant: hint.merchant, subcategoryId: hint.subcategoryId ?? "" }); }}><PencilLine /></Button><Button type="button" variant="ghost" size="icon" aria-label={`Delete ${hint.merchant} hint`} onClick={() => beginDelete({ kind: "hint", id: hint.id, label: `${hint.merchant} hint`, categoryId: selected.id })}><Trash2 /></Button></div>)}</div> : <div className="mt-3 rounded-lg border border-dashed border-line-strong bg-surface px-4 py-6 text-center text-note text-ink-muted">No explicit hints yet. fyn AI is using your transaction history.</div>}
      </div>
    </div>
  </section>;
}
