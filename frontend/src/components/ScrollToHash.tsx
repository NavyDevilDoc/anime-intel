import { useEffect } from 'react'
import { useLocation } from 'react-router-dom'

/**
 * ScrollToHash — global scroll behavior for anchor navigation.
 *
 * React Router v6 doesn't auto-scroll to hash fragments on navigation.
 * When a <Link to="/how-it-works#ranking"> fires, React Router updates
 * the URL and mounts the target route, but the browser doesn't scroll
 * to the #ranking element — leaving users at whatever scroll position
 * they were at (usually the top after a route change, so anchor links
 * appear broken).
 *
 * This component watches location.hash and calls scrollIntoView on the
 * target after each hash change. Handles both same-page hash links
 * (in-page anchors) and cross-page hash links (like the Landing Redesign
 * PR #3 stat cards that deep-link into HowItWorks anchors).
 *
 * Mounted once in App.tsx inside BrowserRouter. Renders nothing.
 */
export default function ScrollToHash() {
  const { hash } = useLocation()

  useEffect(() => {
    if (!hash) return
    // Effect runs after render, so the target element should be in the
    // DOM by the time we query for it. If a page ever lazy-loads its
    // target (async data), a setTimeout wrap would give the mount time
    // to complete — not needed for the current all-static HowItWorks
    // deep links.
    const el = document.querySelector(hash)
    if (el) {
      el.scrollIntoView({ behavior: 'smooth', block: 'start' })
    }
  }, [hash])

  return null
}
