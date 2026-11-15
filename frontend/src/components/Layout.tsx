import { Link, Outlet } from 'react-router-dom'

export default function Layout() {
  return (
    <div className="min-h-screen flex flex-col">
      <header className="bg-vault-surface border-b border-vault-border">
        <div className="max-w-6xl mx-auto px-4 py-3 flex items-center justify-between">
          <Link to="/" className="text-xl font-bold text-vault-text no-underline">
            Anime Intel
          </Link>
        </div>
      </header>

      <main className="flex-1 max-w-6xl w-full mx-auto px-4 py-6">
        <Outlet />
      </main>

      <footer className="border-t border-vault-border py-4 text-center text-sm text-vault-text-muted">
        <span>Data sourced from{' '}
          <a href="https://myanimelist.net" target="_blank" rel="noopener noreferrer" className="text-vault-accent hover:underline">
            MyAnimeList
          </a>
        </span>
        <span className="mx-2">·</span>
        <Link to="/how-it-works" className="text-vault-accent hover:underline no-underline">
          How It Works
        </Link>
        <span className="mx-2">·</span>
        <Link to="/privacy" className="text-vault-accent hover:underline no-underline">
          Privacy
        </Link>
        <span className="mx-2">·</span>
        <Link to="/contact" className="text-vault-accent hover:underline no-underline">
          Contact
        </Link>
        <span className="mx-2">·</span>
        <Link to="/changelog" className="text-vault-accent hover:underline no-underline">
          Changelog
        </Link>
      </footer>
    </div>
  )
}
