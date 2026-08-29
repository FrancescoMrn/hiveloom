import logo from '../../../../docs/assets/logo.png'
import type { ConversationSummary, Harness, RunRow } from '../types'
import { runLabel } from '../runs'
import { statusColor, when } from './common'

export function CopilotRail({
  harnesses,
  conversations,
  selectedConversation,
  selectedHarness,
  runs,
  selectedRun,
  busy,
  collapsed,
  onToggleCollapsed,
  onSettings,
  onNewConversation,
  onSelectConversation,
  onDeleteConversation,
  mobileOpen,
  onCloseMobile,
  onSelectHarness,
  onSelectRun,
  onRenameRun,
}: {
  harnesses: Harness[] | null
  conversations: ConversationSummary[] | null
  selectedConversation: string | null
  selectedHarness: string | null
  runs: RunRow[] | null
  selectedRun: string | null
  busy: boolean
  collapsed: boolean
  onToggleCollapsed: () => void
  onSettings: () => void
  onNewConversation: () => void
  onSelectConversation: (id: string) => void
  onDeleteConversation: (id: string) => void
  mobileOpen: boolean
  onCloseMobile: () => void
  onSelectHarness: (id: string) => void
  onSelectRun: (id: string) => void
  onRenameRun: (id: string, alias: string) => void
}) {
  return (
    <aside
      className="side copilot-rail"
      data-mobile-open={mobileOpen ? '1' : '0'}
      data-collapsed={collapsed ? '1' : '0'}
    >
      <header>
        <img className="brand-mark" src={logo} alt="" width={22} height={22} />
        <span className="copilot-brand">hiveloom</span>
        <button
          className="icon-btn rail-collapse"
          onClick={onToggleCollapsed}
          title={collapsed ? 'Expand navigation' : 'Collapse navigation'}
        >
          <i className={`ph ${collapsed ? 'ph-caret-double-right' : 'ph-caret-double-left'}`} />
        </button>
        <button className="icon-btn rail-mobile-close" onClick={onCloseMobile} title="Close navigation">
          <i className="ph ph-x" />
        </button>
      </header>

      <button className="new-run-btn" disabled={busy} onClick={onNewConversation}>
        <i className="ph ph-chats-circle" />
        <span>New conversation</span>
      </button>

      <div className="scroll">
        <div className="v-label rail-heading">Conversations</div>
        <div className="conversation-list">
          {conversations === null ? (
            <div className="rail-note">Loading conversations…</div>
          ) : conversations.length === 0 ? (
            <div className="rail-note">Your conversations will appear here.</div>
          ) : (
            conversations.map((conversation) => (
              <div className="conversation-row-wrap" key={conversation.id}>
                <button
                  className="conversation-row"
                  data-on={selectedConversation === conversation.id ? '1' : '0'}
                  disabled={busy}
                  onClick={() => onSelectConversation(conversation.id)}
                  title={conversation.preview || conversation.title}
                >
                  <i className="ph ph-chat-circle" />
                  <span>
                    <strong className="ellipsis">{conversation.title}</strong>
                    <small>{conversation.message_count} messages · {when(conversation.updated_at)}</small>
                  </span>
                </button>
                <button
                  className="conversation-delete"
                  disabled={busy}
                  onClick={() => onDeleteConversation(conversation.id)}
                  title="Delete conversation"
                  aria-label={`Delete ${conversation.title}`}
                >
                  <i className="ph ph-trash" />
                </button>
              </div>
            ))
          )}
        </div>

        <div className="v-label rail-heading">Harnesses</div>
        {harnesses === null ? (
          <div className="rail-note">Loading harnesses…</div>
        ) : harnesses.length === 0 ? (
          <div className="rail-empty">
            No harnesses yet. Ask the copilot to create one.
          </div>
        ) : (
          harnesses
            .filter((item) => !item.is_fork)
            .map((item) => {
              const selected = selectedHarness === item.id
              return (
                <div className="copilot-harness-group" key={item.id}>
                  <button
                    className="copilot-harness-row"
                    data-on={selected ? '1' : '0'}
                    aria-pressed={selected}
                    title={`Open and use ${item.name}`}
                    onClick={() => onSelectHarness(item.id)}
                  >
                    <span className="dot" style={{ background: harnessColor(item) }} />
                    <span className="copilot-harness-copy">
                      <strong className="ellipsis">{item.name}</strong>
                      <small className="mono">
                        {item.stats?.total_runs
                          ? `${Math.round(item.stats.success_rate * 100)}% · ${item.stats.total_runs} runs`
                          : 'No runs yet'}
                      </small>
                    </span>
                    <span className="copilot-harness-use">
                      <i className="ph ph-play" /> Use
                    </span>
                  </button>

                  {selected && (
                    <div className="copilot-run-list">
                      {runs === null ? (
                        <div className="rail-note">Loading runs…</div>
                      ) : runs.length === 0 ? (
                        <div className="rail-note">No recorded runs.</div>
                      ) : (
                        runs.slice(0, 8).map((run) => (
                          <button
                            key={run.run_id}
                            className="copilot-run-row"
                            data-on={selectedRun === run.run_id ? '1' : '0'}
                            onClick={() => onSelectRun(run.run_id)}
                            title={[run.status, run.run_id, run.task].filter(Boolean).join(' · ')}
                          >
                            <span
                              className="dot"
                              style={{ background: statusColor(run.status) }}
                            />
                            <span className="ellipsis">{runLabel(run)}</span>
                            <span
                              role="button"
                              className="copilot-run-rename"
                              title="Rename this run"
                              onClick={(event) => {
                                event.stopPropagation()
                                const next = window.prompt(
                                  'Name this run (empty restores the auto name)',
                                  run.alias ?? '',
                                )
                                if (next !== null) onRenameRun(run.run_id, next.trim())
                              }}
                            >
                              <i className="ph ph-pencil-simple" />
                            </span>
                            <small>{when(run.started_at).split(',')[0]}</small>
                          </button>
                        ))
                      )}
                    </div>
                  )}
                </div>
              )
            })
        )}
      </div>

      <footer className="copilot-rail-footer">
        <button onClick={onSettings} title="Settings">
          <i className="ph ph-gear-six" />
          <span>Settings</span>
        </button>
      </footer>
    </aside>
  )
}

function harnessColor(item: Harness): string {
  if (!item.ok) return 'var(--err)'
  if (!item.stats?.total_runs) return 'var(--mut)'
  if (item.stats.success_rate >= 0.8) return 'var(--ok)'
  if (item.stats.success_rate < 0.5) return 'var(--err)'
  return 'var(--warn)'
}
