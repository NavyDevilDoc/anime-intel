import { useState, type FormEvent } from 'react'
import { Helmet } from 'react-helmet-async'
import { api, ApiError } from '../lib/api'

export default function Contact() {
  const [name, setName] = useState('')
  const [email, setEmail] = useState('')
  const [message, setMessage] = useState('')
  const [status, setStatus] = useState<'idle' | 'sending' | 'sent' | 'error'>('idle')
  const [error, setError] = useState('')

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault()
    setStatus('sending')
    setError('')

    try {
      await api.post('/api/contact', {
        name: name.trim(),
        email: email.trim(),
        message: message.trim(),
      })
      setStatus('sent')
      setName('')
      setEmail('')
      setMessage('')
    } catch (err) {
      setStatus('error')
      setError(err instanceof ApiError ? err.message : 'Failed to send message. Please try again.')
    }
  }

  if (status === 'sent') {
    return (
      <div className="max-w-2xl mx-auto py-12 text-center space-y-4">
        <h1 className="text-2xl font-bold">Message Sent</h1>
        <p className="text-vault-text-muted">
          Thanks for reaching out! We'll get back to you as soon as possible.
        </p>
        <button
          onClick={() => setStatus('idle')}
          className="px-5 py-2 rounded bg-vault-accent text-white font-medium hover:bg-vault-accent-hover transition-colors border-none cursor-pointer"
        >
          Send Another
        </button>
      </div>
    )
  }

  return (
    <div className="max-w-2xl mx-auto space-y-6">
      <Helmet>
        <title>Contact — Anime Intel</title>
        <meta name="description" content="Questions, feedback, or feature requests for Anime Intel? Get in touch." />
        <link rel="canonical" href="https://animeintel.app/contact" />
      </Helmet>
      <div>
        <h1 className="text-2xl font-bold">Contact Us</h1>
        <p className="text-vault-text-muted text-sm mt-1">
          Questions, feedback, or feature requests? We'd love to hear from you.
        </p>
      </div>

      <form onSubmit={handleSubmit} className="space-y-4">
        <div>
          <label htmlFor="contact-name" className="block text-sm text-vault-text-muted mb-1">Name</label>
          <input
            id="contact-name"
            type="text"
            value={name}
            onChange={e => setName(e.target.value)}
            required
            maxLength={100}
            className="w-full px-3 py-2 rounded bg-vault-surface border border-vault-border text-vault-text focus:outline-none focus:border-vault-accent"
          />
        </div>

        <div>
          <label htmlFor="contact-email" className="block text-sm text-vault-text-muted mb-1">Email</label>
          <input
            id="contact-email"
            type="email"
            value={email}
            onChange={e => setEmail(e.target.value)}
            required
            maxLength={200}
            className="w-full px-3 py-2 rounded bg-vault-surface border border-vault-border text-vault-text focus:outline-none focus:border-vault-accent"
          />
        </div>

        <div>
          <label htmlFor="contact-message" className="block text-sm text-vault-text-muted mb-1">Message</label>
          <textarea
            id="contact-message"
            value={message}
            onChange={e => setMessage(e.target.value)}
            required
            minLength={10}
            maxLength={2000}
            rows={5}
            className="w-full px-3 py-2 rounded bg-vault-surface border border-vault-border text-vault-text focus:outline-none focus:border-vault-accent resize-y"
          />
          <p className="text-xs text-vault-text-muted mt-1">{message.length} / 2,000</p>
        </div>

        {error && (
          <div className="bg-red-950 border border-red-800 rounded px-4 py-3 text-red-200 text-sm">
            {error}
          </div>
        )}

        <button
          type="submit"
          disabled={status === 'sending' || !name.trim() || !email.trim() || message.trim().length < 10}
          className="w-full py-2.5 rounded bg-vault-accent text-white font-medium hover:bg-vault-accent-hover disabled:opacity-50 transition-colors border-none cursor-pointer"
        >
          {status === 'sending' ? 'Sending...' : 'Send Message'}
        </button>
      </form>
    </div>
  )
}
