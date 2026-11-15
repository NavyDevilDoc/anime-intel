import { BrowserRouter, Routes, Route } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { HelmetProvider } from 'react-helmet-async'
import Layout from './components/Layout'
import ScrollToHash from './components/ScrollToHash'
import Search from './pages/Search'
import HowItWorks from './pages/HowItWorks'
import Privacy from './pages/Privacy'
import Contact from './pages/Contact'
import Changelog from './pages/Changelog'
import Admin from './pages/Admin'
import TopLists from './pages/TopLists'
import NotFound from './pages/NotFound'

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, staleTime: 30_000 } },
})

export default function App() {
  return (
    <HelmetProvider>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <ScrollToHash />
        <Routes>
          <Route element={<Layout />}>
            {/*
              key= on both Route elements forces React to treat the
              landing (`/`) and try (`/try`) mounts of <Search /> as
              distinct instances. Without this, React reconciles them as
              the same instance (same component type at the same tree
              position), so navigating between them re-renders without
              re-running useState initializers — which meant the
              seedQuery / activeTab / encyclopediaTitle initializers
              would silently miss URL param changes on route switch.
              Symptoms this fixes:
                - MarketingHero chip clicks arrived at /try with empty input
                - MarketingHero "Also try" SLT/Encyclopedia links landed
                  on Find a Show tab instead of the requested mode
              (Closes the "Landing hero tab routing bugs" parked item.)
            */}
            <Route index element={<Search key="index" />} />
            <Route path="try" element={<Search key="try" skipHero />} />
            <Route path="top" element={<TopLists />} />
            <Route path="how-it-works" element={<HowItWorks />} />
            <Route path="privacy" element={<Privacy />} />
            <Route path="contact" element={<Contact />} />
            <Route path="changelog" element={<Changelog />} />
            <Route path="admin" element={<Admin />} />
            <Route path="*" element={<NotFound />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
    </HelmetProvider>
  )
}
