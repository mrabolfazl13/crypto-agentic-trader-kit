import type { HTMLAttributes } from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "../../lib/utils";

const badgeVariants = cva(
  "inline-flex items-center gap-1 whitespace-nowrap rounded-full border px-2 text-[12px] font-semibold leading-none",
  {
    variants: {
      tone: {
        neutral: "border-line-strong bg-surface3 text-dim",
        pos: "border-transparent bg-[var(--pos-soft)] text-pos",
        neg: "border-transparent bg-[var(--danger-soft)] text-danger",
        warn: "border-transparent bg-[var(--warn-soft)] text-warn",
        info: "border-transparent bg-[var(--info-soft)] text-info",
        accent: "border-transparent bg-[var(--accent-soft,var(--info-soft))] text-[var(--accent)]",
      },
      size: {
        default: "h-[22px]",
        sm: "h-5 px-1.5 text-[11px]",
      },
    },
    defaultVariants: { tone: "neutral", size: "default" },
  },
);

export interface BadgeProps extends HTMLAttributes<HTMLSpanElement>, VariantProps<typeof badgeVariants> {}

export function Badge({ className, tone, size, ...props }: BadgeProps) {
  return <span className={cn(badgeVariants({ tone, size }), className)} {...props} />;
}
