import { clsx, type ClassValue } from "clsx"
import { extendTailwindMerge } from "tailwind-merge"

/**
 * The type scale has to be declared to tailwind-merge, not just to Tailwind.
 *
 * `text-*` is two utilities wearing one prefix — a font size and a text colour
 * — and tailwind-merge tells them apart by recognising the value. It knows
 * `text-sm`; it does not know `text-control`, so it filed every size in this
 * scale under colour and then dropped it as redundant the moment a real colour
 * followed in the same `className`. `text-control text-ink-muted` came out as
 * `text-ink-muted`, and the control silently fell back to the inherited 16px.
 *
 * Naming the sizes here is what makes the scale actually reach the screen.
 */
const twMerge = extendTailwindMerge({
  extend: {
    classGroups: {
      "font-size": [{ text: ["display", "title", "body", "control", "note", "meta"] }],
    },
  },
})

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}
