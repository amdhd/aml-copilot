import React, { useState } from 'react'
import { decide } from './api'
import Subgraph from './Subgraph.jsx'

const label = { transaction: 'Transaction', alert: 'Alerted transaction',
                guidance: 'Regulatory guidance', gnn_attention: 'GNN attention',
                gnn_subgraph: 'GNN subgraph' }

// Screen 2: the drafted narrative, every sentence carrying the evidence it
// rests on. Hovering a citation resolves it against the bundle the pipeline
// actually assembled -- the same ids the verifier checked.
function Citation({ id, fact }) {
  const [open, setOpen] = useState(false)
  return (
    <span className="cite" onMouseEnter={() => setOpen(true)} onMouseLeave={() => setOpen(false)}>
      {id}
      {open && (
        <span className="popover">
          <strong>{label[fact?.kind] ?? 'Unresolved'}</strong>
          {fact
            ? Object.entries(fact)
                .filter(([k]) => k !== 'kind')
                .map(([k, v]) => (
                  <span key={k} className="row">
                    <span className="k">{k}</span>
                    <span className="v">{String(v).slice(0, 160)}</span>
                  </span>
                ))
            : <span className="row error">This id is not in the evidence bundle.</span>}
        </span>
      )}
    </span>
  )
}

export default function CaseView({ kase, onBack, onRefresh }) {
  const [busy, setBusy] = useState(false)
  const evidence = kase.evidence ?? {}
  const narrative = kase.narrative ?? []

  // Screen 4: approve or reject resumes the run parked at the human gate.
  const submit = async decision => {
    setBusy(true)
    try {
      await decide(kase.case_id, decision)
      // The API returns as soon as the job is queued. The worker then resumes
      // the run from its Postgres checkpoint, so the status changes a moment
      // later rather than in the response -- poll until it does.
      for (let i = 0; i < 10; i++) {
        await new Promise(resolve => setTimeout(resolve, 600))
        const next = await onRefresh()
        if (next && next.status !== 'awaiting_review') break
      }
    } finally { setBusy(false) }
  }

  return (
    <>
      <button className="link" onClick={onBack}>← queue</button>
      <h2>Alert {kase.alert_id}</h2>
      <p className="muted mono">{kase.case_id} · {kase.status}</p>

      <section className="panel">
        <h3>Suggested typology</h3>
        <p className="typology">{kase.typology ?? '—'}</p>
        {/* No count here. This said "3 of 8" and went stale the next eval run;
            the figure moves by +-1 between runs of identical input (README), so
            it belongs with the eval results, not in the UI. Confidence is not
            calibrated, and its ordering changes between runs. It is
            shown because hiding it would be worse, and labelled so nobody
            triages on it. */}
        <p className="warn">
          A suggestion, not a finding. The classifier is wrong on a large share
          of the labelled eval fixtures (current figure in the README), and its
          confidence{kase.confidence != null && ` (${kase.confidence})`} is not
          calibrated — wrong answers have come back more confident than right
          ones. Do not use it to triage.
        </p>
        {kase.reasoning && <p className="muted">{kase.reasoning}</p>}
      </section>

      <section className="panel">
        <h3>Draft SAR narrative</h3>
        {kase.verified != null && (
          <p className={kase.verified ? 'ok' : 'error'}>
            {kase.verified
              ? 'Every citation resolved to an assembled fact; no account named outside the bundle; no sentence rests on guidance alone.'
              : 'Verification failed — see the case record.'}
          </p>
        )}
        {narrative.length === 0 && <p className="muted">No narrative was drafted for this case.</p>}
        {narrative.map((s, i) => (
          <p key={i} className="sentence">
            {s.text}{' '}
            {/* The space matters: adjacent JSX elements leave no text node, so
                without it a long citation list is one unbreakable run and the
                page scrolls sideways. */}
            {s.evidence_ids.map(id => (
              <React.Fragment key={id}>
                <Citation id={id} fact={evidence[id]} />{' '}
              </React.Fragment>
            ))}
          </p>
        ))}
      </section>

      <Subgraph caseId={kase.case_id} />

      <section className="panel">
        <h3>Decision</h3>
        {kase.status === 'awaiting_review' ? (
          <>
            <p className="muted">Nothing is filed. The run is paused in Postgres until you decide.</p>
            <button disabled={busy} onClick={() => submit('approved')}>Approve</button>{' '}
            <button disabled={busy} onClick={() => submit('rejected')}>Reject</button>
          </>
        ) : (
          <p className="muted">This case is {kase.status}; the gate has already been answered.</p>
        )}
      </section>
    </>
  )
}
