import React, { useEffect, useState } from 'react'
import { getAlerts } from './api'

const money = (amount, currency) =>
  `${amount.toLocaleString(undefined, { maximumFractionDigits: 2 })} ${currency}`

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

  return (
    <>
      <h2>Alert queue</h2>
      <p className="muted">
        {alerts.length} alerts, highest GNN risk score first. Test split only.
      </p>
      <table>
        <thead>
          <tr>
            <th>Risk</th><th>Transaction</th><th>From</th><th>To</th>
            <th className="num">Amount</th><th>When</th><th></th>
          </tr>
        </thead>
        <tbody>
          {alerts.map(a => (
            <tr key={a.alert_id}>
              <td><span className="score">{a.risk_score.toFixed(4)}</span></td>
              <td className="mono">{a.alert_id}</td>
              <td className="mono">{a.src_account}</td>
              <td className="mono">
                {a.dst_account}
                {a.src_account === a.dst_account && <span className="tag">same account</span>}
              </td>
              <td className="num">{money(a.amount, a.currency)}</td>
              <td className="muted">{a.timestamp.replace('T', ' ')}</td>
              <td><button onClick={() => onOpen(a.alert_id)}>Investigate</button></td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  )
}
