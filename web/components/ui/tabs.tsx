"use client";

// Radix Tabs: arrow keys move between tabs, Home and End jump, and each panel is labelled
// by its tab, so the run page is fully usable from the keyboard and a screen reader.
import * as RadixTabs from "@radix-ui/react-tabs";
import type { ReactNode } from "react";

export function Tabs({
  label,
  tabs,
  value,
  onValueChange,
}: {
  label: string;
  tabs: { id: string; title: ReactNode; body: ReactNode }[];
  value?: string;
  onValueChange?: (value: string) => void;
}) {
  return (
    <RadixTabs.Root
      defaultValue={tabs[0]?.id}
      {...(value !== undefined ? { value } : {})}
      {...(onValueChange !== undefined ? { onValueChange } : {})}
    >
      <RadixTabs.List
        aria-label={label}
        className="mb-4 flex flex-wrap gap-1 border-b border-border"
      >
        {tabs.map((tab) => (
          <RadixTabs.Trigger
            key={tab.id}
            value={tab.id}
            className="-mb-px border-b-2 border-transparent px-3 py-2 text-sm text-muted hover:text-text data-[state=active]:border-accent data-[state=active]:font-semibold data-[state=active]:text-text"
          >
            {tab.title}
          </RadixTabs.Trigger>
        ))}
      </RadixTabs.List>
      {tabs.map((tab) => (
        <RadixTabs.Content key={tab.id} value={tab.id} className="focus-visible:outline-none">
          {tab.body}
        </RadixTabs.Content>
      ))}
    </RadixTabs.Root>
  );
}
