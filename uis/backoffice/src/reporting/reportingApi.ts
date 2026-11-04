import { authFetch } from '../api'

export type WeeklyWarehouseClientPerformance = {
  id: string
  warehouse: string
  client_id: string
  week_start: string
  inbound_units_count: number
  outbound_orders_count: number
  stockout_events_count: number
  discrepancy_events_count: number
  discrepancy_rate: number
  computed_at: string
}

export async function getWeeklyWarehouseClientPerformance(
  weekStart?: string,
): Promise<WeeklyWarehouseClientPerformance[]> {
  const params = new URLSearchParams()
  if (weekStart) params.set('week_start', weekStart)
  const query = params.toString()
  const response = await authFetch(
    `/api/reporting/weekly-warehouse-client-performance${query ? `?${query}` : ''}`,
    { requireAuth: true },
  )
  if (!response.ok) {
    let message = 'Could not load weekly performance. Please try again.'
    try {
      const body = await response.json() as { detail?: unknown }
      if (typeof body.detail === 'string' && body.detail) message = body.detail
    } catch {
      // Keep a safe, user-facing fallback if the response is not JSON.
    }
    throw new Error(message)
  }
  return response.json() as Promise<WeeklyWarehouseClientPerformance[]>
}
