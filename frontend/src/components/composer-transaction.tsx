import { Dialog } from "@base-ui/react/dialog";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Check, Loader2, Plus, X } from "lucide-react";
import { useRef, useState } from "react";
import { TransactionForm } from "@/components/transaction-form";
import { Button } from "@/components/ui/button";
import { createTransactionRecord } from "@/lib/api";
import { formatMoney } from "@/lib/format";
import { editableTransactionTypes } from "@/lib/protocol";
import { useUserDefaults } from "@/components/user-defaults";

/** A local transaction draft survives dismissal; saving never submits chat. */
export function ComposerTransaction({ disabled }: { disabled: boolean }) {
  const [open, setOpen] = useState(false);
  const [draftVersion, setDraftVersion] = useState(0);
  const [saved, setSaved] = useState<string | null>(null);
  const { currency } = useUserDefaults();
  const amountRef = useRef<HTMLInputElement>(null);
  const queryClient = useQueryClient();
  const save = useMutation({
    mutationFn: createTransactionRecord,
    onSuccess: (transaction) => {
      setSaved(`${formatMoney(transaction.amountMinor, transaction.currency)} ${transaction.transactionType.replaceAll("_", " ")} added`);
      setDraftVersion((version) => version + 1);
      setOpen(false);
      void queryClient.invalidateQueries({ queryKey: ["transactions"] });
      void queryClient.invalidateQueries({ queryKey: ["overview"] });
    },
  });

  return <Dialog.Root open={open} onOpenChange={(next) => { if (!save.isPending) { setOpen(next); if (next) setSaved(null); } }}>
    <Dialog.Trigger render={<Button type="button" variant="ghost" disabled={disabled} className="composer-transaction-trigger" />}>
      <Plus aria-hidden /><span>Add transaction</span>
    </Dialog.Trigger>
    <span role="status" className="sr-only">{saved}</span>
    {saved ? <span className="composer-saved" aria-hidden><Check size={13} />{saved}</span> : null}
    <Dialog.Portal keepMounted>
      <Dialog.Backdrop className="fixed inset-0 z-40 bg-scrim/30 backdrop-blur-[2px] data-[closed]:hidden" />
      <Dialog.Popup initialFocus={amountRef} onSubmit={(event) => event.stopPropagation()} className="composer-transaction-dialog fixed inset-x-3 bottom-3 z-50 mx-auto flex max-h-[calc(100dvh-1.5rem)] max-w-lg flex-col overflow-hidden rounded-2xl border border-line-strong bg-surface shadow-[var(--shadow-overlay)] outline-none data-[closed]:hidden sm:top-1/2 sm:bottom-auto sm:-translate-y-1/2">
        <div className="flex items-start gap-3 border-b border-line px-5 py-4">
          <span className="grid size-10 shrink-0 place-items-center rounded-xl bg-secondary-tint text-secondary"><Plus size={20} /></span>
          <div className="min-w-0 flex-1"><Dialog.Title className="font-heading text-title font-semibold text-ink">Add transaction</Dialog.Title><Dialog.Description className="mt-1 text-note text-ink-muted">A quick entry. Your conversation stays here.</Dialog.Description></div>
          <Dialog.Close render={<Button type="button" variant="ghost" size="icon-lg" disabled={save.isPending} aria-label="Close quick transaction" />}><X /></Dialog.Close>
        </div>
        <TransactionForm
          key={draftVersion}
          initialValues={{ currency }}
          categories={[]}
          fields={["transaction_type", "amount", "merchant", "transaction_at"]}
          transactionTypes={editableTransactionTypes.filter((type) => type !== "transfer")}
          density="entry"
          amountInputRef={amountRef}
          disabled={save.isPending}
          problem={save.error?.message}
          className="flex min-h-0 flex-1 flex-col"
          onSubmit={(values) => {
            if (!values.transactionAt || save.isPending) return;
            save.mutate({ currency: values.currency, expectedVersion: null, amountMinor: values.amountMinor, merchant: values.merchant, transactionAt: values.transactionAt, transactionType: values.transactionType, categoryId: null, subcategoryId: null, spendNature: "unknown", location: null, latitude: null, longitude: null, locationAccuracy: null });
          }}
          afterFields={<p className="mt-3 text-meta text-ink-muted">You can add a category and more details from Transactions after saving.</p>}
          renderActions={({ blocked }) => <div className="flex items-center gap-3 border-t border-line bg-surface px-5 py-4">
            <Dialog.Close render={<Button type="button" variant="ghost" size="lg" disabled={save.isPending} />}>Back to chat</Dialog.Close>
            <Button type="submit" size="lg" className="ml-auto min-h-11" disabled={blocked}>{save.isPending ? <Loader2 className="animate-spin" /> : <Check />}{save.isPending ? "Saving…" : "Save transaction"}</Button>
          </div>}
        />
      </Dialog.Popup>
    </Dialog.Portal>
  </Dialog.Root>;
}
