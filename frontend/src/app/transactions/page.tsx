import type { Metadata } from "next";
import { TransactionsPage } from "@/components/money-pages";
import { WorkspaceShell } from "@/components/workspace";

export const metadata: Metadata = { title: "Transactions" };

export default function TransactionsRoute() {
  return <WorkspaceShell><TransactionsPage /></WorkspaceShell>;
}
