import { formatInstant } from "@/lib/format";
import { cn } from "@/lib/utils";

export function MessageDeliveryTime({ deliveredAt, className }: { deliveredAt: string; className?: string }) {
  const localTime = formatInstant(deliveredAt);
  if (!localTime) return null;

  return <time
    dateTime={deliveredAt}
    aria-label={`Delivered ${localTime}, local time`}
    className={cn("money text-meta text-ink-muted", className)}
  >
    Delivered {localTime}
  </time>;
}
