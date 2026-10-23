import * as TooltipPrimitive from "@radix-ui/react-tooltip";
import { cn } from "../../lib/utils";

export const TooltipProvider = TooltipPrimitive.Provider;
export const Tooltip = TooltipPrimitive.Root;
export const TooltipTrigger = TooltipPrimitive.Trigger;

export function TooltipContent({ className, sideOffset = 6, ...props }: TooltipPrimitive.TooltipContentProps) {
  return (
    <TooltipPrimitive.Portal>
      <TooltipPrimitive.Content
        sideOffset={sideOffset}
        className={cn(
          "z-50 max-w-[280px] rounded-lg border border-line-strong bg-surface3 px-2.5 py-1.5 text-[12px] leading-6 text-ink shadow-[var(--shadow)]",
          className,
        )}
        {...props}
      />
    </TooltipPrimitive.Portal>
  );
}
