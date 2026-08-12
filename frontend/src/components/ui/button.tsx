import { Button as ButtonPrimitive } from "@base-ui/react/button"
import { cva, type VariantProps } from "class-variance-authority"

import { cn } from "@/lib/utils"

/**
 * The seven states every control in this product implements: rest, hover,
 * press, focus, selected, pending and disabled.
 *
 * Two of them are load-bearing and were missing before.
 *
 * Press, because hover does not exist on a phone. Between a tap and the
 * server answering there is nothing else the interface can say, so the scale
 * is the only acknowledgement a touch user gets.
 *
 * And the hit area, which is drawn by `::after` rather than by height. A
 * control that must be 44px to be tappable does not have to *look* 44px —
 * that is what made every button in the app read as a slab, and why 33 call
 * sites had patched the primitive up to `h-11` by hand. The box stays at
 * 28/34/40 and the target is inflated invisibly around it.
 *
 * The size names are the ones call sites already pass; only their values
 * moved.
 */
const buttonVariants = cva(
  [
    "group/button relative inline-flex shrink-0 items-center justify-center gap-2",
    "rounded-md border border-transparent bg-clip-padding text-control font-medium tracking-[-0.005em] whitespace-nowrap select-none",
    "transition-[background-color,border-color,color,transform] duration-[110ms] ease-linear",
    "outline-none focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring",
    // The target, not the box. Invisible, and it never affects layout.
    "after:absolute after:-inset-x-1 after:-inset-y-[5px] after:content-['']",
    "active:not-aria-[haspopup]:scale-[.97]",
    "disabled:pointer-events-none disabled:opacity-45 disabled:after:hidden",
    "aria-invalid:border-danger aria-invalid:outline-danger",
    "[&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-3.5",
  ],
  {
    variants: {
      variant: {
        // Secondary is the only fill in the system, so a filled control can
        // never be misread as a statement about a number.
        default: "bg-secondary text-on-secondary hover:bg-secondary-hover disabled:bg-surface-sunken disabled:text-ink-muted disabled:opacity-100",
        outline: "border-line-strong bg-surface text-ink hover:bg-surface-sunken aria-expanded:bg-surface-sunken",
        secondary: "bg-surface-sunken text-ink-body hover:bg-line",
        ghost: "text-ink-muted hover:bg-surface-sunken hover:text-ink aria-expanded:bg-surface-sunken",
        // Destructive is a text-and-border treatment. `danger` — the filled
        // one — is reserved for a final confirm, where the sentence
        // explaining it has already been read.
        destructive: "border-danger-line bg-surface text-danger-ink hover:bg-danger-tint",
        danger: "bg-danger text-on-secondary hover:bg-danger-ink disabled:bg-surface-sunken disabled:text-ink-muted disabled:opacity-100",
        link: "text-secondary underline-offset-4 hover:underline after:hidden",
      },
      size: {
        sm: "h-[var(--h-compact)] rounded-sm px-2 text-meta [&_svg:not([class*='size-'])]:size-3",
        default: "h-[var(--h-control)] px-3",
        lg: "h-[var(--h-lg)] rounded-md px-3",
        // Icon-only controls get the widest inflation: smallest boxes, and
        // the ones most often reached for with a thumb.
        icon: "size-[var(--h-control)] after:-inset-1.5",
        "icon-sm": "size-[var(--h-compact)] rounded-sm after:-inset-2 [&_svg:not([class*='size-'])]:size-3.5",
        "icon-lg": "size-[var(--h-lg)] rounded-md after:-inset-1",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "default",
    },
  }
)

function Button({
  className,
  variant = "default",
  size = "default",
  ...props
}: ButtonPrimitive.Props & VariantProps<typeof buttonVariants>) {
  return (
    <ButtonPrimitive
      data-slot="button"
      className={cn(buttonVariants({ variant, size, className }))}
      {...props}
    />
  )
}

export { Button, buttonVariants }
