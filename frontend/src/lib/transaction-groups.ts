import type { TransactionListItemOut } from "@/lib/protocol";
import { calendarDayKey } from "@/lib/format";

export type TransactionDayGroup = {
  id: string;
  transactions: TransactionListItemOut[];
};

/** Preserve ledger order while splitting contiguous profile-calendar days. */
export function groupTransactionsByDay(items: TransactionListItemOut[], timeZone?: string): TransactionDayGroup[] {
  const groups: TransactionDayGroup[] = [];
  for (const transaction of items) {
    const day = calendarDayKey(transaction.transactionAt, timeZone) ?? transaction.transactionAt;
    const previous = groups.at(-1);
    if (!previous || previous.id !== day) {
      groups.push({ id: day, transactions: [transaction] });
    } else {
      previous.transactions.push(transaction);
    }
  }
  return groups;
}
