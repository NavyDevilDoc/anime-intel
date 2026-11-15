import { useState } from 'react'

/**
 * PosterImage — MAL CDN poster hotlink with attribution + graceful fallback.
 *
 * Landing Redesign PR #2. Currently used only by <TodaysSpotlight>.
 * Built as a shared primitive so it can be reused later (result cards,
 * encyclopedia hero, Controversy Landscape hover tooltips) — but those
 * consumers are not part of PR #2's scope and will be dedicated future
 * PRs.
 *
 * Storage model (see PR #148): the app stores only MAL's CDN URLs, not
 * image bytes. Rendering hotlinks cdn.myanimelist.net. Attribution
 * anchors this practice explicitly per plan.
 *
 * Feature flag: `VITE_COVER_ART_ENABLED`. When unset or !== 'true', the
 * fallback renders instead — this lets us ship the component with cover
 * art off by default, flip on in Vercel after eyeball verification.
 *
 * Failure modes handled:
 *   - Flag off              -> fallback
 *   - src missing / empty   -> fallback
 *   - MAL 404 / network err -> fallback (via onError handler)
 */

type Size = 'large' | 'medium'

interface Props {
  src: string | null | undefined
  alt: string
  size: Size
  /** MAL show ID. Used to build the attribution link; if omitted, the
   *  "© MAL" overlay is plain text with no href. */
  malId?: number
  /** Extra classes on the wrapper — parent controls final dimensions. */
  className?: string
  /** Custom fallback for the missing / disabled state. Defaults to a
   *  text-only card showing `alt` as the title. */
  fallback?: React.ReactNode
}

// Aspect-ratio + baseline sizing per size variant. Anime posters are 2:3
// portrait. Callers can override with `className` if a specific surface
// needs different dimensions.
const SIZE_CLASSES: Record<Size, string> = {
  large: 'w-full max-w-[280px] aspect-[2/3]',
  medium: 'w-full max-w-[160px] aspect-[2/3]',
}

export default function PosterImage({
  src,
  alt,
  size,
  malId,
  className = '',
  fallback,
}: Props) {
  const [failed, setFailed] = useState(false)
  const coverArtEnabled = import.meta.env.VITE_COVER_ART_ENABLED === 'true'

  const shouldFallback = !coverArtEnabled || !src || failed

  if (shouldFallback) {
    if (fallback !== undefined) {
      return <>{fallback}</>
    }
    return (
      <div
        data-testid="poster-fallback"
        role="img"
        aria-label={alt}
        className={`${SIZE_CLASSES[size]} ${className} flex items-center justify-center rounded bg-vault-surface border border-vault-border p-3 text-center text-sm text-vault-text-muted`}
      >
        {alt}
      </div>
    )
  }

  const attributionLink = malId
    ? `https://myanimelist.net/anime/${malId}`
    : undefined

  return (
    <div className={`relative group overflow-hidden rounded ${SIZE_CLASSES[size]} ${className}`}>
      <img
        src={src}
        alt={alt}
        loading="lazy"
        onError={() => setFailed(true)}
        className="w-full h-full object-cover"
      />
      {/* Attribution overlay — always visible at opacity-60 so MAL's
          hotlink contract is satisfied without depending on hover
          discoverability (dev-server eyeball found the hover-only
          variant was too subtle to notice). Hover brightens to
          opacity-100 as a subtle affordance. Anchors to the MAL show
          page when malId is provided so "© MAL" isn't just decorative. */}
      {attributionLink ? (
        <a
          href={attributionLink}
          target="_blank"
          rel="noopener noreferrer"
          data-testid="poster-attribution"
          className="absolute bottom-1 right-1 rounded bg-black/70 px-1.5 py-0.5 text-[10px] text-white opacity-60 group-hover:opacity-100 transition-opacity"
        >
          © MAL
        </a>
      ) : (
        <span
          data-testid="poster-attribution"
          className="absolute bottom-1 right-1 rounded bg-black/70 px-1.5 py-0.5 text-[10px] text-white opacity-60 group-hover:opacity-100 transition-opacity"
        >
          © MAL
        </span>
      )}
    </div>
  )
}
