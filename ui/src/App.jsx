import React, { useCallback, useEffect, useState } from 'react'
import AlertQueue from './AlertQueue.jsx'
import CaseList from './CaseList.jsx'
import CaseView from './CaseView.jsx'
import { createCase, getCase } from './api'

// A case runs asynchronously: POST returns immediately, the worker takes
// 10-20s, then the run parks at the human gate. Poll until it settles.
const SETTLED = ['awaiting_review', 'approved', 'rejected', 'done', 'failed']

// A case parked at the human gate waits hours or days (section 8), so it has to
// be reachable by link -- not only in the session that opened it.
const caseFromUrl = () => new URLSearchParams(window.location.search).get('case')
const putCaseInUrl = id => {
  const url = new URL(window.location)
  id ? url.searchParams.set('case', id) : url.searchParams.delete('case')
  window.history.replaceState({}, '', url)
}

export default function App() {
  const [caseId, setCaseId] = useState(caseFromUrl)
  // Two destinations, both real. Nothing here leads to a page that does not exist.
  const [view, setView] = useState('queue')
  const [kase, setKase] = useState(null)
  const [error, setError] = useState(null)

  const refresh = useCallback(async () => {
    if (!caseId) return null
    try {
      const next = await getCase(caseId)
      setKase(next)
      return next
    } catch (e) { setError(e.message); return null }
  }, [caseId])

  useEffect(() => { refresh() }, [refresh])
  useEffect(() => { putCaseInUrl(caseId) }, [caseId])

  useEffect(() => {
    if (!kase || SETTLED.includes(kase.status)) return
    const timer = setTimeout(refresh, 2000)
    return () => clearTimeout(timer)
  }, [kase, refresh])

  const open = async alertId => {
    setError(null); setKase(null)
    try { setCaseId((await createCase(alertId)).case_id) } catch (e) { setError(e.message) }
  }

  return (
    <main>
      <header>
        <h1>AML Investigation Copilot</h1>
        <p className="muted">
          The system gathers and drafts; the analyst decides. Nothing is auto-filed.
        </p>
      </header>
      {error && <p className="error">{error}</p>}
      {!caseId && (
        <nav className="tabs">
          <button className={view === 'queue' ? 'tab on' : 'tab'} onClick={() => setView('queue')}>
            Alert queue
          </button>
          <button className={view === 'cases' ? 'tab on' : 'tab'} onClick={() => setView('cases')}>
            Cases
          </button>
        </nav>
      )}
      {!caseId && view === 'queue' && <AlertQueue onOpen={open} />}
      {!caseId && view === 'cases' && <CaseList onOpen={setCaseId} />}
      {caseId && !kase && <p className="muted">Opening case…</p>}
      {caseId && kase && !SETTLED.includes(kase.status) && (
        <p className="muted">Case is {kase.status}; the worker is running it…</p>
      )}
      {caseId && kase && SETTLED.includes(kase.status) && (
        <CaseView
          kase={kase}
          onRefresh={refresh}
          onBack={() => { setCaseId(null); setKase(null); setView('cases') }}
        />
      )}
    </main>
  )
}
