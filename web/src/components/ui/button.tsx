import { forwardRef } from "react";
import type { ButtonHTMLAttributes } from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "../../lib/utils";

const buttonVariants = cva(
  "inline-flex select-none items-center justify-center gap-2 rounded-lg border font-medium transition-colors disabled:pointer-events-none disabled:opacity-50",
  {
    variants: {
      variant: {
        default: "border-line-strong bg-surface2 text-ink hover:bg-surface3",
        primary: "border-transparent bg-accent text-[var(--accent-text)] shadow-sm hover:brightness-110",
        ghost: "border-transparent bg-transparent text-dim hover:bg-surface2 hover:text-ink",
        outline: "border-line bg-transparent text-ink hover:bg-surface2",
        danger: "border-transparent bg-[var(--danger-soft)] text-danger hover:brightness-110",
      },
      size: {
        default: "h-9 px-4 text-sm",
        sm: "h-8 px-3 text-[13px]",
        icon: "h-9 w-9",
      },
    },
    defaultVariants: { variant: "default", size: "default" },
  },
);

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement>, VariantProps<typeof buttonVariants> {}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(({ className, variant, size, type, ...props }, ref) => (
  <button ref={ref} type={type ?? "button"} className={cn(buttonVariants({ variant, size }), className)} {...props} />
));
Button.displayName = "Button";

export { buttonVariants };
