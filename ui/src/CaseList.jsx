import React, { useEffect, useState } from 'react'
import { CASES_SHOWN, getCases } from './api'

// created_at is a timestamptz, sent in UTC. Cut to its first 16 characters it
// read as local time -- eight hours out in Malaysia -- and looked like the
// queue's timestamps, which are the dataset's own clock. Shown in the viewer's
// zone, and labelled with it.
const opened = iso => new Date(iso).toLocaleString(undefined, {
  year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', timeZoneName: 'short',
})

export const chip = status => `chip chip-${status.replace('_', '-')}`

// Cases already opened. Not one of the four screens in section 7, but a run
// parked at the human gate waits hours or days -- without this it is reachable
// only by whoever kept the link.
export default function CaseList({ onOpen }) {
  const [cases, setCases] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => { getCases().then(setCases).catch(e => setError(e.message)) }, [])

  if (error) return <p className="error">Could not load cases: {error}</p>
  if (!cases) return <p className="muted">Loading cases…</p>
  if (!cases.length) return <p className="muted">No cases yet. Open one from the alert queue.</p>

  const waiting = cases.filter(c => c.status === 'awaiting_review').length

  return (
    <>
      <div className="head">
        <div>
          <h2>Cases</h2>
          <p className="muted">
            {cases.length >= CASES_SHOWN ? `Showing the newest ${cases.length}` : `${cases.length} opened`}
            {waiting > 0 && `, ${waiting} waiting on a decision`}.
          </p>
        </div>
      </div>
      <div className="scroller">
      <table>
        <thead>
          <tr>
            <th>Case</th><th>Alert</th><th>Status</th>
            <th>Suggested typology</th><th>Narrative</th><th>Opened</th><th></th>
          </tr>
        </thead>
        <tbody>
          {cases.map(c => (
            <tr key={c.case_id}>
              <td className="mono">{c.case_id.slice(0, 8)}</td>
              <td className="mono">{c.alert_id}</td>
              <td><span className={chip(c.status)}>{c.status.replace('_', ' ')}</span></td>
              <td>{c.typology ?? <span className="muted">—</span>}</td>
              <td>
                {c.has_narrative
                  ? <span className={c.verified ? 'ok' : 'error'}>
                      {c.verified ? 'verified' : 'unverified'}
                    </span>
                  : <span className="muted">none</span>}
              </td>
              <td className="muted">{opened(c.created_at)}</td>
              <td><button onClick={() => onOpen(c.case_id)}>Open</button></td>
            </tr>
          ))}
        </tbody>
      </table>
      </div>
    </>
  )
}
