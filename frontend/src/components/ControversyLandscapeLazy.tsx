import { Component, lazy, Suspense, type ReactNode } from 'react'

/**
 * ControversyLandscapeLazy — Landing Redesign PR #4 lazy-load wrapper.
 *
 * Why this exists:
 *   - The real <ControversyLandscape> depends on @observablehq/plot and
 *     its transitive D3 modules (~100 KB gzipped). We don't want that
 *     bundle in the initial landing paint — users who never scroll past
 *     the hero shouldn't pay for a scatter plot they never see.
 *   - React.lazy + Vite = automatic code-splitting. The Plot bundle lands
 *     in its own chunk; it downloads only when this component mounts.
 *   - If the chunk ever fails to load (CDN hiccup, bad network, 404 on
 *     deploy mismatch), the error boundary catches it so the whole
 *     landing doesn't crash — the viz just doesn't render.
 *
 * Used by Search.tsx (landing surface) and HowItWorks.tsx (secondary
 * placement in the Scoring section). Same lazy chunk serves both.
 */

const LazyLandscape = lazy(() => import('./ControversyLandscape'))

interface State {
  hasError: boolean
}

class LandscapeErrorBoundary extends Component<{ children: ReactNode }, State> {
  state: State = { hasError: false }

  static getDerivedStateFromError(): State {
    return { hasError: true }
  }

  componentDidCatch(error: unknown) {
    // Logged but swallowed — the landscape is a decorative surface, not
    // a critical path. A chunk-load failure shouldn't block the rest of
    // the landing.
    console.warn('ControversyLandscape failed to load:', error)
  }

  render() {
    if (this.state.hasError) return null
    return this.props.children
  }
}

export default function ControversyLandscapeLazy() {
  return (
    <LandscapeErrorBoundary>
      <Suspense fallback={null}>
        <LazyLandscape />
      </Suspense>
    </LandscapeErrorBoundary>
  )
}
