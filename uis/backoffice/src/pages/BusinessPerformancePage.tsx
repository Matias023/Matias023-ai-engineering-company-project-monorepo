import { Link } from 'react-router-dom'
import { useEffect, useMemo, useState } from 'react'
import {
  getWeeklyWarehouseClientPerformance,
  type WeeklyWarehouseClientPerformance,
} from '../reporting/reportingApi'
import '../App.css'

const numberFormat = new Intl.NumberFormat()
const rateFormat = new Intl.NumberFormat(undefined, {
  style: 'percent', maximumFractionDigits: 2,
})

export default function BusinessPerformancePage() {
  const [rows, setRows] = useState<WeeklyWarehouseClientPerformance[] | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [retryKey, setRetryKey] = useState(0)

  useEffect(() => {
    let cancelled = false
    async function load() {
      setLoading(true)
      try {
        const result = await getWeeklyWarehouseClientPerformance()
        if (!cancelled) {
          setRows(result)
          setError(null)
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : 'Unable to load business performance.')
        }
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    void load()
    return () => { cancelled = true }
  }, [retryKey])

  const summary = useMemo(() => {
    if (!rows?.length) return null
    const outbound = rows.reduce((total, row) => total + row.outbound_orders_count, 0)
    const discrepancyEvents = rows.reduce((total, row) => total + row.discrepancy_events_count, 0)
    return {
      inbound: rows.reduce((total, row) => total + row.inbound_units_count, 0),
      outbound,
      stockouts: rows.reduce((total, row) => total + row.stockout_events_count, 0),
      discrepancyRate: outbound === 0 ? 0 : discrepancyEvents / outbound,
    }
  }, [rows])

  return (
    <main className="page">
      <section className="header">
        <div>
          <p className="eyebrow">TrackFlow Business Performance</p>
          <h1>Weekly warehouse performance</h1>
          <p className="subtitle">A weekly view of inbound, outbound, stockouts and inventory discrepancy by warehouse and client.</p>
          <p className="subtitle">Reporting week: <strong>{rows?.[0]?.week_start ?? 'Latest completed reporting week'}</strong></p>
        </div>
        <Link to="/suppliers" className="secondary-button nav-link">
          🏠 Volver al inicio
        </Link>
      </section>

      {loading && <section className="state-card" role="status">Loading weekly performance…</section>}
      {!loading && error && (
        <section className="state-card error" role="alert">
          <p>Business performance could not be loaded: {error}</p>
          <button type="button" className="secondary-button" onClick={() => setRetryKey((key) => key + 1)}>Retry</button>
        </section>
      )}
      {!loading && !error && rows?.length === 0 && (
        <section className="state-card">No weekly performance results are available yet.</section>
      )}
      {!loading && !error && rows && rows.length > 0 && summary && (
        <>
          <section className="summary-grid" aria-label="Weekly business KPIs">
            <article className="summary-card"><h2>Inbound Units Count</h2><p>{numberFormat.format(summary.inbound)}</p></article>
            <article className="summary-card"><h2>Outbound Orders Count</h2><p>{numberFormat.format(summary.outbound)}</p></article>
            <article className="summary-card"><h2>Stockout Events Count</h2><p>{numberFormat.format(summary.stockouts)}</p></article>
            <article className="summary-card"><h2>Discrepancy Rate</h2><p>{rateFormat.format(summary.discrepancyRate)}</p></article>
          </section>
          <section className="state-card" aria-label="Performance by warehouse and client">
            <h2>Results by warehouse and client</h2>
            <div className="table-container">
              <table>
                <thead><tr><th>Week</th><th>Warehouse</th><th>Client</th><th>Inbound units</th><th>Outbound orders</th><th>Stockout events</th><th>Discrepancy rate</th></tr></thead>
                <tbody>{rows.map((row) => (
                  <tr key={row.id}>
                    <td>{row.week_start}</td><td>{row.warehouse}</td><td>{row.client_id}</td>
                    <td>{numberFormat.format(row.inbound_units_count)}</td>
                    <td>{numberFormat.format(row.outbound_orders_count)}</td>
                    <td>{numberFormat.format(row.stockout_events_count)}</td>
                    <td>{rateFormat.format(row.discrepancy_rate)} <span className="subtitle">({numberFormat.format(row.discrepancy_events_count)} supporting events)</span></td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
          </section>
        </>
      )}
    </main>
  )
}
