import type { ButtonHTMLAttributes } from "react";

type Variant = "primary" | "secondary" | "danger" | "ghost";

const STYLES: Record<Variant, string> = {
  primary: "bg-accent text-accent-text hover:opacity-90",
  secondary: "bg-surface text-text border border-border hover:bg-raised",
  danger: "bg-bad-bg text-bad border border-bad hover:opacity-90",
  ghost: "text-text hover:bg-surface",
};

export function Button({
  variant = "secondary",
  className = "",
  type = "button",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant }) {
  return (
    <button
      type={type}
      className={`inline-flex items-center gap-2 rounded-md px-3 py-1.5 text-sm font-medium disabled:cursor-not-allowed disabled:opacity-50 ${STYLES[variant]} ${className}`}
      {...props}
    />
  );
}
