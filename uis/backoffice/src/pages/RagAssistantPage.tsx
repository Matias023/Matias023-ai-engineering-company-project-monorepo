// TrackFlow WebSocket Chat F4B
import { useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { Link } from 'react-router-dom'
import { useAuth } from '../auth/AuthContext'
import {
  appendStreamingToken,
  reconcileSnapshot,
  storageScope,
} from '../rag/chatProtocol'
import './RagAssistantPage.css'

type ChatMessage = {
  message_id: string
  role: 'user' | 'assistant'
  content: string
  status: string
  sequence: number
}

type ChatEvent = {
  event: string
  data: Record<string, unknown>
}

const SESSION_KEY = 'trackflow_chat_session_v1'
const CLIENT_KEY = 'trackflow_chat_client_v1'

const examples = [
  '¿Cómo se gestionan las devoluciones internacionales?',
  '¿Qué transportista ofrece mejor cobertura en Aragón rural?',
  '¿Cuál es la política de devoluciones de TrackFlow?',
]

function getOrCreateClientId(key: string): string {
  const existing = sessionStorage.getItem(key)
  if (existing) return existing

  const generated = typeof crypto.randomUUID === 'function'
    ? crypto.randomUUID()
    : `client_${Date.now()}_${Math.random().toString(36).slice(2)}`

  sessionStorage.setItem(key, generated)
  return generated
}

function readEvent(raw: string): ChatEvent | null {
  try {
    const parsed: unknown = JSON.parse(raw)

    if (
      typeof parsed !== 'object' ||
      parsed === null ||
      !('event' in parsed) ||
      !('data' in parsed) ||
      typeof parsed.event !== 'string' ||
      typeof parsed.data !== 'object' ||
      parsed.data === null ||
      Array.isArray(parsed.data)
    ) {
      return null
    }

    return parsed as ChatEvent
  } catch {
    return null
  }
}

function readMessages(value: unknown): ChatMessage[] {
  if (!Array.isArray(value)) return []

  return value.flatMap((item: unknown) => {
    if (
      typeof item !== 'object' ||
      item === null ||
      !('message_id' in item) ||
      !('role' in item) ||
      !('content' in item) ||
      !('status' in item) ||
      typeof item.message_id !== 'string' ||
      (item.role !== 'user' && item.role !== 'assistant') ||
      typeof item.content !== 'string' ||
      typeof item.status !== 'string'
    ) {
      return []
    }

    return [{
      message_id: item.message_id,
      role: item.role,
      content: item.content,
      status: item.status,
      sequence: 'sequence' in item && typeof item.sequence === 'number'
        ? item.sequence
        : 0,
    } satisfies ChatMessage]
  })
}

export default function RagAssistantPage() {
  const { token } = useAuth()

  const [sessionVersion, setSessionVersion] = useState(0)
  const [question, setQuestion] = useState('')
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [preview, setPreview] = useState('')
  const [connected, setConnected] = useState(false)
  const [ready, setReady] = useState(false)
  const [generating, setGenerating] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const socketRef = useRef<WebSocket | null>(null)
  const sessionRef = useRef<string | null>(null)
  const retryRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const bottomRef = useRef<HTMLDivElement | null>(null)
  const generatingRef = useRef(false)
  const streamingSnapshotRef = useRef(false)

  function updateGenerating(value: boolean) {
    generatingRef.current = value
    setGenerating(value)
  }

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, preview])

  useEffect(() => {
    if (!token) return

    let disposed = false
    let attempts = 0

    const scope = storageScope(token)
    const clientKey = `${CLIENT_KEY}_${scope}`
    const sessionKey = `${SESSION_KEY}_${scope}`
    const clientId = getOrCreateClientId(clientKey)
    sessionRef.current = sessionStorage.getItem(sessionKey)

    function connect() {
      if (disposed || !token) return

      const scheme = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
      const endpoint = new URL(
        `${scheme}//${window.location.host}/api/agent/chat/ws`,
      )

      endpoint.searchParams.set('client_id', clientId)

      if (sessionRef.current) {
        endpoint.searchParams.set('session_id', sessionRef.current)
      }

      const ws = new WebSocket(endpoint.toString())
      socketRef.current = ws

      ws.onopen = () => {
        if (disposed || socketRef.current !== ws) return

        // Autenticacion inicial fuera de la URL del handshake.
        ws.send(JSON.stringify({
          event: 'authenticate',
          data: { token },
        }))

        attempts = 0
        setConnected(true)
        setError(null)
      }

      ws.onmessage = (event: MessageEvent<string>) => {
        if (disposed || socketRef.current !== ws) return

        const packet = readEvent(event.data)
        if (!packet) return

        const data = packet.data
        const incomingSession = data.session_id

        if (typeof incomingSession !== 'string') return

        if (sessionRef.current && sessionRef.current !== incomingSession) {
          setError('El servidor devolvió una sesión diferente.')
          ws.close()
          return
        }

        if (packet.event === 'session_snapshot') {
          sessionRef.current = incomingSession
          sessionStorage.setItem(sessionKey, incomingSession)

          const history = readMessages(data.messages)
          setMessages(previous => reconcileSnapshot(previous, history))
          setPreview('')
          setReady(true)

          const last = history[history.length - 1]
          streamingSnapshotRef.current = (
            last?.role === 'assistant' && last.status === 'streaming'
          )
          updateGenerating(
            last?.role === 'assistant' && last.status === 'streaming',
          )
          return
        }

        if (packet.event === 'token_chunk') {
          if (
            typeof data.token !== 'string' ||
            typeof data.sequence !== 'number'
          ) return
          updateGenerating(true)

          if (streamingSnapshotRef.current) {
            setMessages(previous => {
              const last = previous[previous.length - 1]

              if (
                !last ||
                last.role !== 'assistant' ||
                last.status !== 'streaming'
              ) {
                return previous
              }

              return [
                ...previous.slice(0, -1),
                ...appendStreamingToken(
                  [last],
                  data.token as string,
                  data.sequence as number,
                ),
              ]
            })
          } else {
            setPreview(previous => previous + data.token)
          }

          return
        }

        if (packet.event === 'generation_completed') {
          streamingSnapshotRef.current = false
          updateGenerating(false)
          setPreview('')
          return
        }

        if (packet.event === 'generation_interrupted') {
          streamingSnapshotRef.current = false
          updateGenerating(false)
          setPreview('')
          setError('Generación interrumpida. Puedes enviar otra pregunta.')
        }
      }

      ws.onerror = () => {
        if (!disposed) setError('No se pudo establecer la conexión WebSocket.')
      }

      ws.onclose = (event: CloseEvent) => {
        if (disposed || socketRef.current !== ws) return

        socketRef.current = null
        setConnected(false)
        setReady(false)

        if (event.code === 1008) {
          setError('Sesión rechazada. Comprueba tu autenticación.')
          return
        }

        attempts += 1
        const delay = Math.min(1000 * (2 ** (attempts - 1)), 10000)

        retryRef.current = setTimeout(connect, delay)
      }
    }

    connect()

    return () => {
      disposed = true

      if (retryRef.current !== null) {
        clearTimeout(retryRef.current)
        retryRef.current = null
      }

      socketRef.current?.close()
      socketRef.current = null
    }
  }, [token, sessionVersion])

  function sendMessage(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()

    const text = question.trim()
    const ws = socketRef.current
    const sessionId = sessionRef.current

    if (
      !text ||
      generatingRef.current ||
      !ready ||
      !sessionId ||
      ws?.readyState !== WebSocket.OPEN
    ) {
      return
    }

    setError(null)
    setPreview('')
    streamingSnapshotRef.current = false
    updateGenerating(true)

    try {
      ws.send(JSON.stringify({
        event: 'user_message',
        data: { session_id: sessionId, text },
      }))
      setQuestion('')
      setMessages(previous => [
        ...previous,
        {
          message_id: `pending_${Date.now()}`,
          role: 'user',
          content: text,
          status: 'pending',
          sequence: 0,
        },
      ])
    } catch {
      updateGenerating(false)
      setError('No fue posible enviar el mensaje.')
    }
  }

  function interruptGeneration() {
    const ws = socketRef.current
    const sessionId = sessionRef.current

    if (
      !generatingRef.current ||
      !sessionId ||
      ws?.readyState !== WebSocket.OPEN
    ) {
      return
    }

    ws.send(JSON.stringify({
      event: 'interrupt_requested',
      data: {
        session_id: sessionId,
        new_input: '',
      },
    }))
  }

  function startNewSession() {
    if (generatingRef.current) return

    if (token) {
      sessionStorage.removeItem(
        `${SESSION_KEY}_${storageScope(token)}`,
      )
    }
    sessionRef.current = null
    streamingSnapshotRef.current = false

    setMessages([])
    setPreview('')
    setReady(false)
    setConnected(false)
    setError(null)

    // Cambiar la versión desmonta el socket anterior y conecta
    // una sesión nueva sin reutilizar su session_id.
    setSessionVersion(previous => previous + 1)
  }

  return (
    <main className="page rag-page">
      <section className="header">
        <div>
          <p className="eyebrow">TrackFlow · First Line CX</p>
          <h1>Asistente de soporte</h1>
          <p className="subtitle">
            Conversación en tiempo real sobre seguimiento, devoluciones
            y documentación oficial.
          </p>
        </div>
        <Link to="/suppliers" className="secondary-button nav-link">
          🏠 Volver al inicio
        </Link>
      </section>

      <section className="state-card rag-panel">
        <div className="chat-toolbar">
          <h2>Conversación</h2>
          <span role="status" className="chat-connection">
            {connected && ready ? '🟢 Conectado' : '🟠 Reconectando...'}
          </span>
          <button
            type="button"
            className="secondary-button"
            onClick={startNewSession}
            disabled={generating}
          >
            Nueva conversación
          </button>
        </div>

        <div className="chat-history" aria-label="Historial del chat">
          {messages.length === 0 && !preview && (
            <p className="subtitle">
              Escribe tu primera pregunta para comenzar.
            </p>
          )}

          {messages.map(message => (
            <article
              key={message.message_id}
              className={`chat-message chat-${message.role}`}
            >
              <strong>
                {message.role === 'user' ? 'Tú' : 'Asistente'}
              </strong>
              <p className="rag-answer">{message.content}</p>
              {message.status === 'interrupted' && (
                <small>Generación interrumpida</small>
              )}
              {message.status === 'streaming' && (
                <small>Respuesta todavía no verificada</small>
              )}
            </article>
          ))}

          {preview && (
            <article className="chat-message chat-assistant">
              <strong>Asistente · generando</strong>
              <p className="rag-answer">{preview}</p>
              <small>⚠️ Texto provisional, pendiente de validación</small>
            </article>
          )}
          <div ref={bottomRef} />
        </div>

        {(!token || error) && (
          <p className="chat-error" role="alert">
            {!token
              ? 'Debes iniciar sesión para utilizar el chat.'
              : error}
          </p>
        )}

        <form onSubmit={sendMessage}>
          <label htmlFor="rag-question">Tu mensaje</label>
          <textarea
            id="rag-question"
            className="rag-input"
            value={question}
            onChange={event => setQuestion(event.target.value)}
            maxLength={1000}
            rows={3}
            placeholder="Escribe una consulta sobre TrackFlow..."
            disabled={!ready}
          />

          <div className="rag-actions">
            <span className="rag-counter">
              {question.length}/1000 caracteres
            </span>

            <div className="chat-buttons">
              {generating && (
                <button
                  type="button"
                  className="secondary-button"
                  onClick={interruptGeneration}
                  disabled={!connected}
                >
                  Interrumpir
                </button>
              )}
              <button
                type="submit"
                className="primary-button"
                disabled={!ready || generating || !question.trim()}
              >
                {generating ? 'Generando...' : 'Enviar'}
              </button>
            </div>
          </div>
        </form>

        <div className="rag-examples">
          <h3>Preguntas de ejemplo</h3>
          <div className="rag-example-list">
            {examples.map(example => (
              <button
                key={example}
                type="button"
                className="secondary-button"
                onClick={() => setQuestion(example)}
                disabled={!ready || generating}
              >
                {example}
              </button>
            ))}
          </div>
        </div>
      </section>
    </main>
  )
}
