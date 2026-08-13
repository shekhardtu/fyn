import type { Metadata } from "next";
import { CategoriesPage } from "@/components/money-pages";
import { WorkspaceShell } from "@/components/workspace";

export const metadata: Metadata = { title: "Categories" };

export default function CategoriesRoute() {
  return <WorkspaceShell><CategoriesPage /></WorkspaceShell>;
}
