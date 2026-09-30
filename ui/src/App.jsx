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
      setError(null)            // a failure that has since recovered is not news
      return next
    } catch (e) { setError(e.message); return null }
  }, [caseId])

  useEffect(() => { refresh() }, [refresh])
  useEffect(() => { putCaseInUrl(caseId) }, [caseId])

  // An interval, not a timeout re-armed by each new `kase`: a refresh that
  // failed left `kase` unchanged, so the timeout was never armed again and the
  // page said "the worker is running it" after the worker had finished.
  const settled = kase != null && SETTLED.includes(kase.status)
  useEffect(() => {
    if (!caseId || settled) return
    const timer = setInterval(refresh, 2000)
    return () => clearInterval(timer)
  }, [caseId, settled, refresh])

  const open = async alertId => {
    setError(null); setKase(null)
    try { setCaseId((await createCase(alertId)).case_id) } catch (e) { setError(e.message) }
  }

  // Back to the tab the case was opened from. A case page with no way off it
  // left the URL bar as the only exit.
  const back = () => { setCaseId(null); setKase(null); setError(null) }
  const backLink = <button className="link" onClick={back}>← {view === 'cases' ? 'cases' : 'queue'}</button>

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
          <button className={view === 'queue' ? 'tab on' : 'tab'} onClick={() => { setView('queue'); setError(null) }}>
            Alert queue
          </button>
          <button className={view === 'cases' ? 'tab on' : 'tab'} onClick={() => { setView('cases'); setError(null) }}>
            Cases
          </button>
        </nav>
      )}
      {!caseId && view === 'queue' && <AlertQueue onOpen={open} />}
      {!caseId && view === 'cases' && <CaseList onOpen={setCaseId} />}
      {caseId && !settled && backLink}
      {caseId && !kase && !error && <p className="muted">Opening case…</p>}
      {caseId && kase && !settled && (
        <p className="muted">Case is {kase.status}; the worker is running it…</p>
      )}
      {caseId && settled && <CaseView kase={kase} onRefresh={refresh} backLink={backLink} />}
    </main>
  )
}
