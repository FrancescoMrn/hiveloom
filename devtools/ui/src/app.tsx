import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { CSSProperties, PointerEvent as ReactPointerEvent } from 'react'
import { api } from './api'
import { ContextInspector } from './components/ContextInspector'
import type { InspectorView } from './components/ContextInspector'
import { CopilotCanvas } from './components/CopilotCanvas'
import { CopilotChat } from './components/CopilotChat'
import { CopilotRail } from './components/CopilotRail'
import { Settings, useProviders } from './components/Settings'
import { Notice } from './components/common'
import { workbenchModels } from './models'
import { applyTheme, loadPrefs, savePrefs } from './prefs'
import type { Prefs } from './prefs'
import type {
  Artifact,
  ConversationRecord,
  ConversationSummary,
  CopilotInfo,
  Harness,
  HarnessDetail,
  RunRow,
} from './types'
import { useCopilot } from './useCopilot'

/**
 * Chat is the front door; the contextual inspector is the source-of-truth door.
 * Both remain in one workbench and both address the same selected harness/run.
 */
export function App() {
  const [info, setInfo] = useState<CopilotInfo | null>(null)
  const [harnesses, setHarnesses] = useState<Harness[] | null>(null)
  const [conversations, setConversations] = useState<ConversationSummary[] | null>(null)
  const [conversation, setConversation] = useState<ConversationRecord | null>(null)
  const [selectedHarness, setSelectedHarness] = useState<string | null>(null)
  const [runs, setRuns] = useState<RunRow[] | null>(null)
  const [selectedRun, setSelectedRun] = useState<string | null>(null)
  const [artifact, setArtifact] = useState<Artifact | null>(null)
  const [inspectorOpen, setInspectorOpen] = useState(false)
  const [inspectorExpanded, setInspectorExpanded] = useState(false)
  const [mobileRail, setMobileRail] = useState(false)
  const [railCollapsed, setRailCollapsed] = useState(false)
  const [inspectorWidth, setInspectorWidth] = useState(720)
  const [inspectorView, setInspectorView] = useState<InspectorView>('interface')
  const [inspectorViewKey, setInspectorViewKey] = useState(0)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [settingsHarness, setSettingsHarness] = useState<HarnessDetail | null>(null)
  const [prefs, setPrefs] = useState(loadPrefs)
  const [copilotModel, setCopilotModel] = useState('')
  const [error, setError] = useState<string | null>(null)
  const workspaceElement = useRef<HTMLDivElement | null>(null)
  const selectionSaveQueue = useRef<Promise<void>>(Promise.resolve())
  const selectionSaveVersion = useRef(0)
  const providers = useProviders(selectedHarness)

  useEffect(() => {
    applyTheme(prefs.theme)
  }, [prefs.theme])

  const updatePrefs = (next: Prefs) => {
    setPrefs(next)
    savePrefs(next)
    applyTheme(next.theme)
  }

  const refreshHarnesses = useCallback(async () => {
    try {
      const rows = await api.harnesses()
      setHarnesses(rows)
      setSelectedHarness((current) =>
        current && rows.some((row) => row.id === current) ? current : null,
      )
      setError(null)
    } catch (exc) {
      setError(String(exc))
      setHarnesses([])
    }
  }, [])

  const refreshConversations = useCallback(async () => {
    try {
      setConversations(await api.conversations())
    } catch (exc) {
      setError(String(exc))
      setConversations([])
    }
  }, [])

  const openConversation = useCallback(async (id: string) => {
    const record = await api.conversation(id)
    setConversation(record)
    setSelectedHarness(record.selection.harness_id ?? null)
    setSelectedRun(record.selection.run_id ?? null)
    setArtifact(null)
    setInspectorView(record.selection.run_id ? 'trace' : 'interface')
    setInspectorViewKey((value) => value + 1)
    setInspectorOpen(Boolean(record.selection.harness_id))
    setRailCollapsed(Boolean(record.selection.harness_id))
    setMobileRail(false)
  }, [])

  const newConversation = useCallback(async () => {
    const created = await api.createConversation()
    setConversation(created)
    setSelectedHarness(null)
    setSelectedRun(null)
    setArtifact(null)
    setInspectorOpen(false)
    setInspectorExpanded(false)
    setRailCollapsed(false)
    setMobileRail(false)
    await refreshConversations()
  }, [refreshConversations])

  useEffect(() => {
    let live = true
    void api.copilot().then((value) => live && setInfo(value)).catch((exc) => live && setError(String(exc)))
    void refreshHarnesses()
    void (async () => {
      try {
        const conversationRows = await api.conversations()
        if (!live) return
        setConversations(conversationRows)
        if (conversationRows.length) {
          await openConversation(conversationRows[0].id)
        } else {
          await newConversation()
        }
      } catch (exc) {
        if (live) setError(String(exc))
      }
    })()
    return () => { live = false }
  }, [newConversation, openConversation, refreshHarnesses])

  useEffect(() => {
    let live = true
    if (!selectedHarness) {
      setSettingsHarness(null)
      return () => { live = false }
    }
    setSettingsHarness((current) => current?.id === selectedHarness ? current : null)
    void api.harness(selectedHarness)
      .then((detail) => live && setSettingsHarness(detail))
      .catch(() => live && setSettingsHarness(null))
    return () => { live = false }
  }, [selectedHarness])

  useEffect(() => {
    if (info?.model && !copilotModel) setCopilotModel(info.model)
  }, [copilotModel, info?.model])

  const refreshRuns = useCallback(async () => {
    if (!selectedHarness) {
      setRuns(null)
      return
    }
    try {
      setRuns(await api.runs(selectedHarness))
    } catch {
      setRuns([])
    }
  }, [selectedHarness])

  const refresh = useCallback(async () => {
    await Promise.all([refreshHarnesses(), refreshRuns(), refreshConversations()])
  }, [refreshConversations, refreshHarnesses, refreshRuns])

  const renameRun = useCallback(
    async (runId: string, alias: string) => {
      if (!selectedHarness) return
      try {
        await api.setRunAlias(selectedHarness, runId, alias)
        await refreshRuns()
      } catch {
        // The list simply keeps its current name; nothing to roll back.
      }
    },
    [refreshRuns, selectedHarness],
  )

  useEffect(() => {
    setRuns(null)
    void refreshRuns()
  }, [refreshRuns])

  const selection = useMemo(
    () => ({
      ...(selectedHarness ? { harness_id: selectedHarness } : {}),
      ...(selectedRun ? { run_id: selectedRun } : {}),
    }),
    [selectedHarness, selectedRun],
  )

  useEffect(() => {
    if (!conversation) return
    if (
      conversation.selection.harness_id === selection.harness_id
      && conversation.selection.run_id === selection.run_id
    ) return
    const conversationId = conversation.id
    const version = ++selectionSaveVersion.current
    selectionSaveQueue.current = selectionSaveQueue.current
      .catch(() => undefined)
      .then(async () => {
        const saved = await api.saveConversation(conversationId, { selection })
        if (version !== selectionSaveVersion.current) return
        setConversation((current) => current?.id === saved.id ? { ...current, selection: saved.selection } : current)
        void refreshConversations()
      })
      .catch((exc) => {
        if (version === selectionSaveVersion.current) setError(String(exc))
      })
  }, [conversation, refreshConversations, selection])

  const onCopilotFinished = useCallback(
    (artifacts: Artifact[]) => {
      const newest = artifacts.at(-1)
      if (newest) {
        setArtifact(newest)
        setInspectorOpen(true)
        setRailCollapsed(true)
      }
      const created = [...artifacts].reverse().find((item) => item.kind === 'harness_created')
      const data = record(created?.data)
      if (typeof data.id === 'string') {
        setSelectedHarness(data.id)
        setInspectorOpen(true)
        setRailCollapsed(true)
      }
      void refresh()
    },
    [refresh],
  )

  const conversationPersisted = useCallback(async () => {
    await refreshConversations()
    if (!conversation?.id) return
    try {
      const saved = await api.conversation(conversation.id)
      setConversation((current) => current?.id === saved.id ? saved : current)
    } catch {
      // The message remains visible; the next explicit load reports storage errors.
    }
  }, [conversation?.id, refreshConversations])

  const workspace = useCopilot(
    conversation?.id ?? null,
    conversation?.messages ?? [],
    selection,
    copilotModel && copilotModel !== info?.model ? copilotModel : '',
    onCopilotFinished,
    conversationPersisted,
  )

  const harness = (harnesses ?? []).find((item) => item.id === selectedHarness) ?? null
  const run = (runs ?? []).find((item) => item.run_id === selectedRun) ?? null
  const copilotModels = useMemo(() => {
    const reachable = workbenchModels(providers)
    const narrowed = prefs.seatModels.length
      ? reachable.filter((selector) => prefs.seatModels.includes(selector))
      : reachable
    return [...new Set([info?.model, copilotModel, ...narrowed].filter(Boolean) as string[])]
  }, [copilotModel, info?.model, prefs.seatModels, providers])

  const selectHarness = (id: string) => {
    setSelectedHarness(id || null)
    setSelectedRun(null)
    setArtifact(null)
    setInspectorOpen(Boolean(id))
    setInspectorView('interface')
    setInspectorViewKey((value) => value + 1)
    setRailCollapsed(Boolean(id))
    setMobileRail(false)
  }

  const selectRun = (id: string | null) => {
    setSelectedRun(id)
    setArtifact(null)
    setInspectorOpen(true)
    setInspectorView(id ? 'trace' : 'runs')
    setInspectorViewKey((value) => value + 1)
    setRailCollapsed(true)
    setMobileRail(false)
  }

  const deleteConversation = async (id: string) => {
    const target = conversations?.find((item) => item.id === id)
    if (!window.confirm(`Delete “${target?.title ?? 'this conversation'}”? This cannot be undone.`)) return
    try {
      await api.deleteConversation(id)
      const remaining = (conversations ?? []).filter((item) => item.id !== id)
      if (conversation?.id === id) {
        if (remaining.length) await openConversation(remaining[0].id)
        else await newConversation()
      }
      await refreshConversations()
    } catch (exc) {
      setError(String(exc))
    }
  }

  const showingInspector = inspectorOpen && Boolean(selectedHarness || artifact)

  const openSettings = () => setSettingsOpen(true)

  const beginResize = (event: ReactPointerEvent<HTMLDivElement>) => {
    const bounds = workspaceElement.current?.getBoundingClientRect()
    if (!bounds) return
    event.preventDefault()
    const resize = (move: PointerEvent) => {
      const maximum = Math.max(420, bounds.width - 320 - 7)
      setInspectorWidth(Math.min(maximum, Math.max(420, bounds.right - move.clientX)))
    }
    const finish = () => {
      window.removeEventListener('pointermove', resize)
      window.removeEventListener('pointerup', finish)
    }
    window.addEventListener('pointermove', resize)
    window.addEventListener('pointerup', finish, { once: true })
  }

  return (
    <div className={`app copilot-app${showingInspector ? ' has-inspector' : ''}${inspectorExpanded ? ' inspector-expanded' : ''}${railCollapsed ? ' rail-collapsed' : ''}`}>
      <CopilotRail
        harnesses={harnesses}
        conversations={conversations}
        selectedConversation={conversation?.id ?? null}
        selectedHarness={selectedHarness}
        runs={runs}
        selectedRun={selectedRun}
        busy={workspace.busy}
        collapsed={railCollapsed}
        onToggleCollapsed={() => setRailCollapsed((value) => !value)}
        onSettings={openSettings}
        mobileOpen={mobileRail}
        onCloseMobile={() => setMobileRail(false)}
        onNewConversation={() => void newConversation()}
        onSelectConversation={(id) => void openConversation(id)}
        onDeleteConversation={(id) => void deleteConversation(id)}
        onSelectHarness={selectHarness}
        onSelectRun={(id) => selectRun(id)}
        onRenameRun={(id, alias) => void renameRun(id, alias)}
      />

      <main className="main copilot-main">
        <header className="copilot-header">
          <div>
            <h1>{conversation?.title ?? 'Hiveloom copilot'}</h1>
            <span>Persistent conversation · deterministic harness evidence</span>
          </div>
          <div className="copilot-header-context">
            {harness ? (
              <>
                <button
                  className="copilot-context-chip"
                  onClick={() => {
                    setArtifact(null)
                    setInspectorView('interface')
                    setInspectorViewKey((value) => value + 1)
                    setInspectorOpen(true)
                    setRailCollapsed(true)
                  }}
                  title="Open harness workspace"
                >
                  <i className="ph ph-hexagon" />
                  {harness.name}
                  <i className="ph ph-arrow-square-out" />
                </button>
                <button className="icon-btn" onClick={() => selectHarness('')} title="Detach harness context">
                  <i className="ph ph-link-break" />
                </button>
              </>
            ) : (
              <span className="copilot-context-empty">No harness selected</span>
            )}
            {workspace.busy && <span className="live-chip"><span className="dot" /> working</span>}
          </div>
          <div className="copilot-mobile-actions">
            <button className="icon-btn" onClick={() => setMobileRail(true)} title="Open conversations">
              <i className="ph ph-sidebar-simple" />
            </button>
            <button className="icon-btn" disabled={workspace.busy} onClick={() => void newConversation()} title="New conversation">
              <i className="ph ph-chats-circle" />
            </button>
            <select aria-label="Harness context" value={selectedHarness ?? ''} onChange={(event) => selectHarness(event.target.value)}>
              <option value="">No harness</option>
              {(harnesses ?? []).filter((item) => !item.is_fork).map((item) => (
                <option key={item.id} value={item.id}>{item.name}</option>
              ))}
            </select>
          </div>
        </header>

        {error && (
          <div className="copilot-top-notice">
            <Notice
              icon="ph-warning-octagon"
              tone="err"
              title="Workbench problem"
              body={error}
              action={<button className="icon-btn" onClick={() => setError(null)}><i className="ph ph-x" /></button>}
            />
          </div>
        )}

        <div
          ref={workspaceElement}
          className="copilot-workspace"
          style={{ '--inspector-width': `${inspectorWidth}px` } as CSSProperties}
        >
          <CopilotChat
            info={info}
            harness={harness}
            run={run}
            workspace={workspace}
            models={copilotModels}
            model={copilotModel}
            onModel={setCopilotModel}
            onDetachRun={() => {
              setSelectedRun(null)
              if (inspectorOpen && !artifact) {
                setInspectorView('runs')
                setInspectorViewKey((value) => value + 1)
              }
            }}
            onArtifact={(next) => {
              setArtifact(next)
              setInspectorOpen(true)
              setRailCollapsed(true)
            }}
          />
          {showingInspector && !inspectorExpanded && (
            <div
              className="inspector-resizer"
              role="separator"
              tabIndex={0}
              aria-label="Resize harness workspace"
              aria-orientation="vertical"
              aria-valuenow={inspectorWidth}
              onPointerDown={beginResize}
              onKeyDown={(event) => {
                if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return
                event.preventDefault()
                setInspectorWidth((current) => Math.max(420, current + (event.key === 'ArrowLeft' ? 24 : -24)))
              }}
            />
          )}
          {showingInspector && artifact && (
            <CopilotCanvas
              artifact={artifact}
              onClose={() => {
                setArtifact(null)
                setInspectorOpen(false)
                setRailCollapsed(false)
              }}
              onChanged={refresh}
            />
          )}
          {showingInspector && !artifact && selectedHarness && (
            <ContextInspector
              harnessId={selectedHarness}
              harnesses={harnesses ?? []}
              runs={runs}
              runId={selectedRun}
              expanded={inspectorExpanded}
              requestedView={inspectorView}
              requestedViewKey={inspectorViewKey}
              onToggleExpanded={() => setInspectorExpanded((value) => !value)}
              onClose={() => {
                setInspectorOpen(false)
                setRailCollapsed(false)
              }}
              onSelectRun={selectRun}
              onOpenHarness={async (id) => selectHarness(id)}
              onRefresh={refresh}
            />
          )}
        </div>
      </main>
      {settingsOpen && (
        <Settings
          harness={settingsHarness}
          providers={providers}
          prefs={prefs}
          onPrefs={updatePrefs}
          onClose={() => setSettingsOpen(false)}
          onSaved={async () => {
            if (selectedHarness) setSettingsHarness(await api.harness(selectedHarness))
            await refresh()
          }}
          onOpenSpec={() => {
            if (!selectedHarness) return
            setArtifact(null)
            setInspectorView('spec')
            setInspectorViewKey((value) => value + 1)
            setInspectorOpen(true)
            setRailCollapsed(true)
          }}
        />
      )}
    </div>
  )
}

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {}
}
