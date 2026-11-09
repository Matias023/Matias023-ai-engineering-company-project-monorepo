import { authFetch } from './api'

export type RFPTicket = {
  ticket_id: string
  status: string
  created_at: string
  client_name: string | null
  departments_needed: string[]
}

export type RFPSection = {
  department_id: string
  key_aspects: string[]
  draft_content: string | null
  evaluation_results: {
    generation_status?: string
    review_status?: string | null
    evidence?: string[]
    unconfirmed?: string[]
    verified_commercial_metrics?: {
      monthly_capacity?: number
      monthly_volume?: number
      returns_turnaround_hours?: number
      source_reference?: string
      confirmed_at?: string
      [key: string]: unknown
    }
  }
  approval_status: string | null
}

export type RFPDetail = RFPTicket & {
  client_country: string | null
  sections: RFPSection[]
}

async function readJson<T>(response: Response): Promise<T> {
  if (!response.ok) {
    throw new Error(`RFP API error: HTTP ${response.status}`)
  }
  return response.json() as Promise<T>
}

export async function getRFPTickets(): Promise<RFPTicket[]> {
  const response = await authFetch('/api/rfp/tickets')
  const data = await readJson<{ tickets: RFPTicket[] }>(response)
  return data.tickets
}

export async function getRFPDetail(
  ticketId: string,
): Promise<RFPDetail> {
  const response = await authFetch(
    `/api/rfp/tickets/${encodeURIComponent(ticketId)}`,
  )
  return readJson<RFPDetail>(response)
}


export async function startRFPGeneration(
  ticketId: string,
): Promise<void> {
  const response = await authFetch(
    `/api/rfp/tickets/${encodeURIComponent(ticketId)}/generate`,
    { method: 'POST' },
  )
  await readJson<unknown>(response)
}

export type RFPApprovalDecision = 'approve' | 'request_changes'

export async function startRFPApproval(
  ticketId: string,
  departmentId: string,
): Promise<void> {
  const response = await authFetch(
    `/api/rfp/tickets/${encodeURIComponent(ticketId)}/departments/${encodeURIComponent(departmentId)}/approval/start`,
    { method: 'POST' },
  )
  await readJson<unknown>(response)
}

export async function submitRFPApproval(
  ticketId: string,
  departmentId: string,
  decision: RFPApprovalDecision,
  feedback?: string,
): Promise<void> {
  const response = await authFetch(
    `/api/rfp/tickets/${encodeURIComponent(ticketId)}/departments/${encodeURIComponent(departmentId)}/approval/decision`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ decision, feedback: feedback || null }),
    },
  )
  await readJson<unknown>(response)
}


export async function retryFailedRFPGeneration(
  ticketId: string,
  departmentId: string,
): Promise<void> {
  const response = await authFetch(
    `/api/rfp/tickets/${encodeURIComponent(ticketId)}/departments/${encodeURIComponent(departmentId)}/generation/retry`,
    { method: 'POST' },
  )
  await readJson<unknown>(response)
}

export async function regenerateRFPDepartment(
  ticketId: string,
  departmentId: string,
): Promise<void> {
  const response = await authFetch(
    `/api/rfp/tickets/${encodeURIComponent(ticketId)}/departments/${encodeURIComponent(departmentId)}/regenerate`,
    { method: 'POST' },
  )
  await readJson<unknown>(response)
}


export type RFPFinalizationResult = {
  ticket_id: string
  status: string
  download_url: string
}

export async function finalizeRFP(
  ticketId: string,
): Promise<RFPFinalizationResult> {
  const response = await authFetch(
    `/api/rfp/tickets/${encodeURIComponent(ticketId)}/finalize`,
    { method: 'POST' },
  )

  return readJson<RFPFinalizationResult>(response)
}

export async function downloadFinalRFP(
  ticketId: string,
): Promise<Blob> {
  const response = await authFetch(
    `/api/rfp/tickets/${encodeURIComponent(ticketId)}/final/download`,
  )

  if (!response.ok) {
    throw new Error(
      `Error al descargar RFP: HTTP ${response.status}`
    )
  }

  return response.blob()
}


export type RFPCommercialConfirmationResult = {
  ticket_id: string
  department_id: string
  metric: string
  value: number
  status: string
}

export async function confirmRFPCommercialMetric(
  ticketId: string,
  departmentId: string,
  value: number,
  sourceReference: string,
): Promise<RFPCommercialConfirmationResult> {
  const response = await authFetch(
    `/api/rfp/tickets/${encodeURIComponent(ticketId)}/departments/${encodeURIComponent(departmentId)}/commercial-metrics`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        value,
        source_reference: sourceReference,
      }),
    },
  )

  return readJson<RFPCommercialConfirmationResult>(response)
}


export type RFPUploadResult = {
  ticket_id: string
  status: string
  task_id: string
}

export async function uploadRFP(file: File): Promise<RFPUploadResult> {
  const form = new FormData()
  form.append('file', file)

  const response = await authFetch('/api/rfp/intake', {
    method: 'POST',
    body: form,
  })

  return readJson<RFPUploadResult>(response)
}
