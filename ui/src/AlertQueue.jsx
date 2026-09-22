import React, { useEffect, useState } from 'react'
import { getAlerts } from './api'

const money = (amount, currency) =>
  `${amount.toLocaleString(undefined, { maximumFractionDigits: 2 })} ${currency}`

// Every alert is above the 0.7682 decision threshold and the top of the queue is
// tighter still -- 0.9920 to 0.9995 across 50 rows. Drawn from zero, or even
// from the threshold, the bar is 97-100% full on every row and says nothing.
// It is scaled to the range actually on screen, and the caption says so, because
// a bar that looks like information and is not is worse than no bar.
const scale = (score, lo, hi) => (hi - lo < 1e-9 ? 1 : 0.06 + 0.94 * (score - lo) / (hi - lo))

// Screen 1: the queue, highest model risk first. The API already filters to the
// test split -- train-period scores are in-sample and not honest.
export default function AlertQueue({ onOpen }) {
  const [alerts, setAlerts] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    getAlerts().then(setAlerts).catch(e => setError(e.message))
  }, [])

  if (error) return <p className="error">Could not load alerts: {error}</p>
  if (!alerts) return <p className="muted">Loading the queue…</p>

  const scores = alerts.map(a => a.risk_score)
  const [lo, hi] = [Math.min(...scores), Math.max(...scores)]

  return (
    <>
      <h2>Alert queue</h2>
      <p className="muted">
        {alerts.length} alerts, highest GNN risk score first. Test split only.
        Scores here span {lo.toFixed(4)}–{hi.toFixed(4)}, so the bar is scaled to
        that range, not to zero.
      </p>
      <div className="scroller">
      <table>
        <thead>
          <tr>
            <th>Risk score</th><th>Transaction</th><th>From</th><th>To</th>
            <th className="num">Amount</th><th>When</th><th></th>
          </tr>
        </thead>
        <tbody>
          {alerts.map(a => (
            <tr key={a.alert_id}>
              <td>
                <div className="risk">
                  <span className="track"><span className="fill" style={{ width: `${scale(a.risk_score, lo, hi) * 100}%` }} /></span>
                  <span className="score">{a.risk_score.toFixed(4)}</span>
                </div>
              </td>
              <td className="mono">{a.alert_id}</td>
              <td className="mono">{a.src_account}</td>
              <td className="mono">
                {a.dst_account}
                {a.src_account === a.dst_account && <span className="tag">same account</span>}
              </td>
              <td className="num">{money(a.amount, a.currency)}</td>
              <td className="muted when">{a.timestamp.replace('T', ' ').slice(0, 16)}</td>
              <td><button onClick={() => onOpen(a.alert_id)}>Investigate</button></td>
            </tr>
          ))}
        </tbody>
      </table>
      </div>
    </>
  )
}
