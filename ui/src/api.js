const json = async (path, init) => {
  const response = await fetch(`/api${path}`, init)
  if (!response.ok) {
    // FastAPI puts the reason in `detail`. Shown raw, a reviewer read
    // `404 {"detail":"no such alert"}`; a bare status said nothing at all.
    const text = await response.text()
    let detail = text
    try { detail = JSON.parse(text).detail ?? text } catch { /* not JSON */ }
    throw new Error(`${response.status}: ${detail || response.statusText}`)
  }
  return response.json()
}

export const getAlerts = (limit = 50) => json(`/alerts?limit=${limit}`)
export const getCases = (limit = 50) => json(`/cases?limit=${limit}`)
export const getCase = id => json(`/cases/${id}`)
export const getSubgraph = id => json(`/cases/${id}/subgraph`)
export const createCase = alert_id =>
  json('/cases', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ alert_id }),
  })
export const decide = (id, decision) =>
  json(`/cases/${id}/decision`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ decision }),
  })
