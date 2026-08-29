import { formatInstant } from "@/lib/format";
import { cn } from "@/lib/utils";
import { useUserDefaults } from "@/components/user-defaults";

export function MessageDeliveryTime({ deliveredAt, className }: { deliveredAt: string; className?: string }) {
  const { timeZone } = useUserDefaults();
  const localTime = formatInstant(deliveredAt, timeZone);
  if (!localTime) return null;

  return <time
    dateTime={deliveredAt}
    aria-label={`Delivered ${localTime}, ${timeZone ? `${timeZone} time` : "local time"}`}
    className={cn("money text-meta text-ink-muted", className)}
  >
    Delivered {localTime}
  </time>;
}
