import React, { useEffect, useRef, useState } from 'react'
import ForceGraph2D from 'react-force-graph-2d'
import { getSubgraph } from './api'

// Risk score exists only for transactions the model alerted on; a neighbour
// below the threshold is not in the alerts table and comes back null.
const colour = node =>
  node.is_alert ? '#c1121f'
  : node.risk_score == null ? '#9aa0a6'
  : `hsl(${28 - 28 * node.risk_score}, 85%, ${62 - 16 * node.risk_score}%)`

// Screen 3: the neighbourhood the GAT actually attended to for this case.
// No search, no filters, no layout controls.
export default function Subgraph({ caseId }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const box = useRef(null)
  const graph = useRef(null)
  const [width, setWidth] = useState(800)

  useEffect(() => {
    getSubgraph(caseId).then(setData).catch(e => setError(e.message))
  }, [caseId])

  useEffect(() => {
    if (box.current) setWidth(box.current.clientWidth)
  }, [data])

  if (error) return <section className="panel"><h3>Subgraph</h3><p className="error">{error}</p></section>
  if (!data) return <section className="panel"><h3>Subgraph</h3><p className="muted">Loading…</p></section>

  const alert = data.nodes.find(n => n.is_alert)
  // section 4 links temporal neighbours in either direction, so some of these
  // post-date the alert even though the evidence history no longer does. The
  // bundle is inconsistent about its time horizon -- see section 13.
  const ahead = data.nodes.filter(n => !n.is_alert && alert && n.timestamp > alert.timestamp)
  const aheadIds = new Set(ahead.map(n => n.txn_id))

  return (
    <section className="panel">
      <h3>Subgraph</h3>
      <p className="muted">
        {data.nodes.length} transactions the model attended to, {data.links.length} links.
        Grey means the transaction scored below the alert threshold, so it has no risk score.
      </p>
      {ahead.length > 0 && (
        <p className="warn">
          {ahead.length} of these occurred <em>after</em> the alerted transaction (dashed).
          The evidence history is restricted to prior transactions, but the GNN
          edge rule links neighbours in both directions — see §13.
        </p>
      )}
      <div ref={box} className="graph">
        <ForceGraph2D
          ref={graph}
          graphData={data}
          width={width}
          height={360}
          nodeId="txn_id"
          nodeLabel={n =>
            `${n.txn_id}\n${n.amount.toLocaleString()} ${n.currency}\n${n.timestamp}` +
            `\nrisk ${n.risk_score ?? 'below threshold'}` +
            `\nattention ${n.attention_weight ?? '—'}`}
          nodeColor={colour}
          nodeRelSize={5}
          linkWidth={l => 1 + 6 * (l.value ?? 0)}
          linkColor={() => '#b9bdc4'}
          linkLineDash={l => aheadIds.has(l.target?.txn_id ?? l.target) ? [4, 3] : null}
          cooldownTicks={80}
          onEngineStop={() => graph.current?.zoomToFit(400, 40)}
        />
      </div>
    </section>
  )
}
