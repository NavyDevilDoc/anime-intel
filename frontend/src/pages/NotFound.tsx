import { Helmet } from 'react-helmet-async'
import { Link } from 'react-router-dom'

export default function NotFound() {
  return (
    <div className="flex flex-col items-center justify-center py-20 text-center">
      <Helmet>
        <title>Page Not Found — Anime Intel</title>
        <meta name="robots" content="noindex" />
      </Helmet>
      <h1 className="text-6xl font-bold text-vault-accent mb-4">404</h1>
      <p className="text-xl text-vault-text mb-2">Page not found</p>
      <p className="text-vault-text-muted mb-8">
        The page you're looking for doesn't exist or has been moved.
      </p>
      <Link
        to="/"
        className="px-5 py-2 rounded bg-vault-accent text-white font-medium no-underline hover:bg-vault-accent-hover transition-colors"
      >
        Back to Search
      </Link>
    </div>
  )
}
