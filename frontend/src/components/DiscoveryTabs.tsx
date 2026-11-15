/**
 * DiscoveryTabs — shared top-level tab bar for the four landing surfaces:
 *   Find a Show / Shows Like This / Encyclopedia / Top Lists
 *
 * Rendered by both `Search.tsx` (state-based tabs on /try) and
 * `TopLists.tsx` (URL-based active tab on /top). Presentational: this
 * component doesn't own navigation logic — the parent decides whether a
 * tab click is a state change or a `navigate()` call.
 *
 * Aesthetic matches the pre-existing tab bar tokens (px-5 py-2.5,
 * border-b-2 with vault-accent underline on active). No visual change
 * from the pre-Item-1b bar.
 */

const TABS = [
  'Find a Show',
  'Shows Like This',
  'Encyclopedia',
  'Top Lists',
] as const

export type Tab = typeof TABS[number]

interface Props {
  activeTab: Tab
  onTabChange: (tab: Tab) => void
}

export default function DiscoveryTabs({ activeTab, onTabChange }: Props) {
  return (
    <div
      role="tablist"
      aria-label="Discovery surface"
      className="flex border-b border-vault-border"
    >
      {TABS.map(tab => (
        <button
          key={tab}
          type="button"
          role="tab"
          aria-selected={activeTab === tab}
          onClick={() => onTabChange(tab)}
          className={`px-5 py-2.5 text-sm font-medium border-b-2 transition-colors cursor-pointer bg-transparent ${
            activeTab === tab
              ? 'border-vault-accent text-vault-accent'
              : 'border-transparent text-vault-text-muted hover:text-vault-text'
          }`}
        >
          {tab}
        </button>
      ))}
    </div>
  )
}
