import * as React from "react"

import { cn } from "@/lib/utils"

function Textarea({ className, ...props }: React.ComponentProps<"textarea">) {
  return (
    <textarea
      data-slot="textarea"
      className={cn(
        "manual-field flex field-sizing-content min-h-16 w-full rounded-lg border border-line-strong bg-transparent px-3 py-2 text-control text-ink transition-colors duration-[110ms] ease-linear outline-none placeholder:text-ink-muted disabled:cursor-not-allowed disabled:opacity-50 aria-invalid:border-danger",
        className
      )}
      {...props}
    />
  )
}

export { Textarea }
