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

// 100, not 50: the deployed seed is the top 50 test alerts *plus* the eval
// fixtures (scripts/make_seed.py), 51 today, and at 50 the lowest-ranked
// fixture -- 4987170, the smurfing case -- could not be opened from the queue.
export const getAlerts = (limit = 100) => json(`/alerts?limit=${limit}`)
// The API's ceiling. At 50, older cases -- ones still awaiting review
// included -- dropped off the list without a word; CaseList now says when it
// is showing only the newest.
export const CASES_SHOWN = 500
export const getCases = (limit = CASES_SHOWN) => json(`/cases?limit=${limit}`)
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
