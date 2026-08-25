import type { TransactionListItemOut } from "@/lib/protocol";

export type TransactionDayGroup = {
  id: string;
  transactions: TransactionListItemOut[];
};

/** Preserve ledger order while splitting contiguous local calendar days. */
export function groupTransactionsByDay(items: TransactionListItemOut[]): TransactionDayGroup[] {
  const groups: TransactionDayGroup[] = [];
  for (const transaction of items) {
    const day = new Date(transaction.transactionAt).toDateString();
    const previous = groups.at(-1);
    if (!previous || previous.id !== day) {
      groups.push({ id: day, transactions: [transaction] });
    } else {
      previous.transactions.push(transaction);
    }
  }
  return groups;
}
