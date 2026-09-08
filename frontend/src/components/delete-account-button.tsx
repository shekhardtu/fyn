import { AlertDialog } from "@base-ui/react/alert-dialog";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Loader2, Trash2 } from "lucide-react";
import { useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { toast } from "@/components/ui/toast";
import { deleteAccount } from "@/lib/api";
import type { OverviewAccountOut, OverviewOut } from "@/lib/generated/contracts";

export function DeleteAccountButton({ account }: { account: OverviewAccountOut }) {
  const [open, setOpen] = useState(false);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const queryClient = useQueryClient();
  const remove = useMutation({
    mutationFn: () => deleteAccount(account.id),
    onSuccess: () => {
      setOpen(false);
      queryClient.setQueriesData<OverviewOut>({ queryKey: ["overview"] }, (data) => data && {
        ...data, accounts: data.accounts.filter((item) => item.id !== account.id),
      });
      for (const key of ["overview", "accounts", "transactions", "transaction-revisions", "dashboard", "dashboards"]) {
        void queryClient.invalidateQueries({ queryKey: [key] });
      }
      toast.add({ title: "Account deleted", type: "success", timeout: 4000 });
    },
  });

  return <AlertDialog.Root open={open} onOpenChange={(nextOpen, event) => {
    if (remove.isPending) { event.cancel(); return; }
    remove.reset();
    setOpen(nextOpen);
  }}>
    <AlertDialog.Trigger render={<Button type="button" variant="ghost" size="icon" className="ml-auto size-11 shrink-0 hover:bg-danger-tint hover:text-danger-ink" aria-label={`Delete account ${account.name}`} title="Delete account" />}><Trash2 aria-hidden /></AlertDialog.Trigger>
    <AlertDialog.Portal>
      <AlertDialog.Backdrop className="fixed inset-0 z-[60] bg-black/50" />
      <AlertDialog.Popup initialFocus={cancelRef} className="fixed top-1/2 left-1/2 z-[61] max-h-[calc(100dvh-2rem)] w-[calc(100%-2rem)] max-w-md -translate-x-1/2 -translate-y-1/2 overflow-y-auto rounded-2xl border border-line bg-surface p-5 shadow-xl outline-none sm:p-6">
        <AlertDialog.Title className="font-heading text-title font-semibold text-ink">Delete account?</AlertDialog.Title>
        <AlertDialog.Description className="mt-2 text-note leading-6 break-words text-ink-muted">
          Delete <span className="font-semibold text-ink">{account.name}</span> and its recorded balance history? Your transactions will stay, with their link to this account removed. This can’t be undone.
        </AlertDialog.Description>
        {remove.isError ? <p role="alert" className="mt-3 text-note text-danger-ink">{remove.error.message}</p> : null}
        <div className="mt-5 flex flex-wrap justify-end gap-3">
          <AlertDialog.Close render={<Button ref={cancelRef} type="button" variant="outline" className="min-h-11" disabled={remove.isPending} />}>Cancel</AlertDialog.Close>
          <Button type="button" variant="danger" className="min-h-11" disabled={remove.isPending} onClick={() => remove.mutate()}>
            {remove.isPending ? <Loader2 className="animate-spin" aria-hidden /> : <Trash2 aria-hidden />}
            {remove.isPending ? "Deleting…" : "Delete account"}
          </Button>
        </div>
      </AlertDialog.Popup>
    </AlertDialog.Portal>
  </AlertDialog.Root>;
}
