import * as DialogPrimitive from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import type { HTMLAttributes } from "react";
import { cn } from "../../lib/utils";

export const Dialog = DialogPrimitive.Root;
export const DialogTrigger = DialogPrimitive.Trigger;
export const DialogClose = DialogPrimitive.Close;

export function DialogContent({ className, children, ...props }: DialogPrimitive.DialogContentProps) {
  return (
    <DialogPrimitive.Portal>
      <DialogPrimitive.Overlay className="fixed inset-0 z-40 bg-[rgba(4,7,12,.66)] backdrop-blur-[2px]" />
      <DialogPrimitive.Content
        className={cn(
          "fixed left-1/2 top-1/2 z-50 flex max-h-[86vh] w-[min(680px,calc(100vw-32px))] -translate-x-1/2 -translate-y-1/2 flex-col gap-4 overflow-auto rounded-xl border border-line-strong bg-surface p-5 shadow-[var(--shadow)]",
          className,
        )}
        {...props}
      >
        {children}
        <DialogPrimitive.Close
          aria-label="بستن"
          className="absolute left-3 top-3 inline-flex h-8 w-8 items-center justify-center rounded-md border border-line text-dim hover:bg-surface2 hover:text-ink"
        >
          <X size={16} />
        </DialogPrimitive.Close>
      </DialogPrimitive.Content>
    </DialogPrimitive.Portal>
  );
}

export function DialogHeader({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("flex flex-col gap-1 pe-10", className)} {...props} />;
}

export function DialogTitle({ className, ...props }: DialogPrimitive.DialogTitleProps) {
  return <DialogPrimitive.Title className={cn("text-[16px] font-bold", className)} {...props} />;
}

export function DialogDescription({ className, ...props }: DialogPrimitive.DialogDescriptionProps) {
  return <DialogPrimitive.Description className={cn("m-0 text-[13px] text-dim", className)} {...props} />;
}
