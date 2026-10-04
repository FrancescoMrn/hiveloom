/**
 * One control for choosing a model: a trigger showing the current selection,
 * and a popover listing the models of the providers this workbench was set up
 * with (Settings → Models), grouped by provider and searchable.
 *
 * Nothing else is listed: not a provider without its key, not a superseded
 * model, not an offline stand-in a harness's extension registers. Those can
 * still be the *current* value — a harness keeps naming its own model — and
 * then they are shown as such, under "In use", rather than silently replaced.
 */
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { CSSProperties, KeyboardEvent as ReactKeyboardEvent } from 'react'
import { createPortal } from 'react-dom'
import type { WorkbenchDirectory, WorkbenchModel } from '../types'

type Row = {
  key: string
  /** The selector this row commits: `provider/model-id`, or '' for the default. */
  value: string
  label: string
  caption?: string
  badge?: string
}

type Group = { key: string; heading: string; note?: string; rows: Row[] }

const SEARCH_THRESHOLD = 5

function priceCaption(model: WorkbenchModel): string {
  const { input_cost_per_mtok: input, output_cost_per_mtok: output } = model
  if (input === null || output === null) return ''
  if (!input && !output) return 'free'
  const fmt = (value: number) => `$${value < 1 ? value.toFixed(2).replace(/0$/, '') : value.toFixed(value % 1 ? 2 : 0)}`
  return `${fmt(input)} / ${fmt(output)}`
}

/** Ordered-subsequence match, so `op55` finds `claude-opus-5-5`. */
function matches(query: string, text: string): boolean {
  const needle = query.toLowerCase().replace(/\s+/g, '')
  const hay = text.toLowerCase()
  let at = 0
  for (const char of needle) {
    at = hay.indexOf(char, at)
    if (at < 0) return false
    at += 1
  }
  return true
}

export function ModelPicker({
  directory,
  value,
  onChange,
  defaultLabel,
  heldNote,
  heldBadge,
  variant = 'field',
  placeholder = 'Choose a model',
  ariaLabel = 'Model',
}: {
  directory: WorkbenchDirectory | null
  value: string
  onChange: (selector: string) => void
  /** When set, a first row that commits '' — "use the default". */
  defaultLabel?: string
  /**
   * Why the current value is not offered, under "In use". Derived from the
   * directory unless given (a harness's offline model says so itself).
   */
  heldNote?: string
  heldBadge?: string
  variant?: 'field' | 'chip'
  placeholder?: string
  ariaLabel?: string
}) {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [highlight, setHighlight] = useState(0)
  const [position, setPosition] = useState<CSSProperties>({})
  const trigger = useRef<HTMLButtonElement | null>(null)
  const menu = useRef<HTMLDivElement | null>(null)
  const search = useRef<HTMLInputElement | null>(null)

  const groups = useMemo<Group[]>(() => {
    const out: Group[] = []
    for (const provider of directory?.providers ?? []) {
      if (!provider.key_set || !provider.models.length) continue
      out.push({
        key: provider.name,
        heading: provider.label || provider.name,
        rows: provider.models.map((model) => ({
          key: `${provider.name}/${model.id}`,
          value: `${provider.name}/${model.id}`,
          label: model.id,
          caption: priceCaption(model),
        })),
      })
    }
    if (value && !out.some((group) => group.rows.some((row) => row.value === value))) {
      const provider = value.split('/')[0]
      const setUp = !!directory?.providers.some((row) => row.name === provider && row.key_set)
      out.unshift({
        key: '__held',
        heading: 'In use',
        note: heldNote ?? (setUp ? 'not one of the models you offer' : 'its provider is not set up here'),
        rows: [{ key: value, value, label: value, badge: heldBadge ?? (setUp ? 'not in your list' : 'not set up') }],
      })
    }
    return out
  }, [directory, value, heldNote, heldBadge])

  const total = groups.reduce((sum, group) => sum + group.rows.length, 0)
  const showSearch = total >= SEARCH_THRESHOLD

  const visible = useMemo<Group[]>(() => {
    const q = query.trim()
    if (!q) return groups
    return groups
      .map((group) => {
        // A provider name matches as typed ("openai" lists OpenAI); the
        // fuzzy match is for model ids only, so `op55` cannot match a row
        // through the letters of "Anthropic".
        const whole = group.heading.toLowerCase().includes(q.toLowerCase())
        return { ...group, rows: whole ? group.rows : group.rows.filter((row) => matches(q, row.label)) }
      })
      .filter((group) => group.rows.length)
  }, [groups, query])

  const flat = useMemo(() => {
    const rows = visible.flatMap((group) => group.rows)
    return defaultLabel && !query.trim() ? [{ key: '__default', value: '', label: defaultLabel }, ...rows] : rows
  }, [visible, defaultLabel, query])

  const place = () => {
    const box = trigger.current?.getBoundingClientRect()
    if (!box) return
    const width = Math.min(440, Math.max(box.width, 320), window.innerWidth - 24)
    const left = Math.min(Math.max(12, box.left), window.innerWidth - width - 12)
    const below = window.innerHeight - box.bottom
    const style: CSSProperties = { left, width, maxHeight: 380 }
    if (below < 300 && box.top > below) {
      style.bottom = window.innerHeight - box.top + 6
      style.maxHeight = Math.min(380, box.top - 18)
    } else {
      style.top = box.bottom + 6
      style.maxHeight = Math.min(380, below - 18)
    }
    setPosition(style)
  }

  useLayoutEffect(() => {
    if (!open) return
    place()
    const index = flat.findIndex((row) => row.value === value)
    setHighlight(index < 0 ? 0 : index)
    // Focus after the portal mounts.
    requestAnimationFrame(() => (search.current ?? menu.current)?.focus())
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  useEffect(() => {
    if (!open) return
    const away = (event: MouseEvent) => {
      const target = event.target as Node
      if (menu.current?.contains(target) || trigger.current?.contains(target)) return
      setOpen(false)
    }
    // Scrolling the page would detach the popover from its trigger; scrolling
    // inside the list must not close it.
    const scrolled = (event: Event) => {
      if (menu.current?.contains(event.target as Node)) return
      setOpen(false)
    }
    window.addEventListener('mousedown', away)
    window.addEventListener('resize', place)
    window.addEventListener('scroll', scrolled, true)
    return () => {
      window.removeEventListener('mousedown', away)
      window.removeEventListener('resize', place)
      window.removeEventListener('scroll', scrolled, true)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  useEffect(() => setHighlight(0), [query])

  useEffect(() => {
    menu.current?.querySelector('[data-highlighted="1"]')?.scrollIntoView({ block: 'nearest' })
  }, [highlight])

  const close = () => {
    setOpen(false)
    setQuery('')
    trigger.current?.focus()
  }

  const commit = (row: Row | { value: string }) => {
    onChange(row.value)
    close()
  }

  const onKeyDown = (event: ReactKeyboardEvent) => {
    if (event.key === 'Escape') {
      event.preventDefault()
      if (query) setQuery('')
      else close()
    } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault()
      if (!flat.length) return
      const step = event.key === 'ArrowDown' ? 1 : -1
      setHighlight((current) => (current + step + flat.length) % flat.length)
    } else if (event.key === 'Enter') {
      event.preventDefault()
      const row = flat[highlight]
      if (row) commit(row)
    } else if (event.key === 'Tab') {
      setOpen(false)
      setQuery('')
    }
  }

  const current = groups.flatMap((group) => group.rows).find((row) => row.value === value)
  const triggerLabel = value ? (current?.label ?? value) : (defaultLabel ?? placeholder)
  const triggerProvider = value && variant === 'field'
    ? groups.find((group) => group.rows.some((row) => row.value === value))?.heading
    : undefined

  let index = defaultLabel && !query.trim() ? 1 : 0

  return (
    <div className="model-picker" data-variant={variant}>
      <button
        ref={trigger}
        type="button"
        className="model-picker-trigger"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={`${ariaLabel}: ${triggerLabel}`}
        onClick={() => (open ? close() : setOpen(true))}
        onKeyDown={(event) => {
          if (!open && (event.key === 'ArrowDown' || event.key === 'ArrowUp')) {
            event.preventDefault()
            setOpen(true)
          }
        }}
      >
        <i className="ph ph-brain" />
        {triggerProvider && <span className="model-picker-provider">{triggerProvider}</span>}
        <span className="model-picker-value">{directory === null ? 'Loading models…' : triggerLabel}</span>
        {current?.badge && variant === 'field' && <span className="model-picker-badge">{current.badge}</span>}
        <i className="ph ph-caret-down model-picker-caret" data-open={open ? '1' : '0'} />
      </button>
      {open &&
        createPortal(
          <div
            ref={menu}
            className="model-picker-menu"
            style={position}
            tabIndex={-1}
            onKeyDown={onKeyDown}
            role="dialog"
            aria-label={`Choose ${ariaLabel.toLowerCase()}`}
          >
            {showSearch && (
              <div className="model-picker-search">
                <i className="ph ph-magnifying-glass" />
                <input
                  ref={search}
                  value={query}
                  placeholder="Search models…"
                  aria-label="Search models"
                  aria-activedescendant={flat[highlight] ? `mp-${flat[highlight].key}` : undefined}
                  onChange={(event) => setQuery(event.target.value)}
                />
                {query && (
                  <button type="button" onClick={() => { setQuery(''); search.current?.focus() }} title="Clear search">
                    <i className="ph ph-x" />
                  </button>
                )}
              </div>
            )}
            <div className="model-picker-list" role="listbox" aria-label={ariaLabel}>
              {defaultLabel && !query.trim() && (
                <button
                  type="button"
                  id="mp-__default"
                  role="option"
                  aria-selected={value === ''}
                  className="model-picker-row"
                  data-highlighted={highlight === 0 ? '1' : '0'}
                  onMouseEnter={() => setHighlight(0)}
                  onClick={() => commit({ value: '' })}
                >
                  <span className="model-picker-name">{defaultLabel}</span>
                  <span className="model-picker-check">{value === '' && <i className="ph ph-check" />}</span>
                </button>
              )}
              {visible.map((group) => (
                <div key={group.key} className="model-picker-group">
                  <div className="model-picker-heading">
                    {group.heading}
                    {group.note && <span> · {group.note}</span>}
                  </div>
                  {group.rows.map((row) => {
                    const at = index++
                    return (
                      <button
                        type="button"
                        key={row.key}
                        id={`mp-${row.key}`}
                        role="option"
                        aria-selected={row.value === value}
                        className="model-picker-row"
                        data-highlighted={highlight === at ? '1' : '0'}
                        onMouseEnter={() => setHighlight(at)}
                        onClick={() => commit(row)}
                      >
                        <span className="model-picker-name mono">{row.label}</span>
                        {row.badge && <span className="model-picker-badge">{row.badge}</span>}
                        {row.caption && <span className="model-picker-caption mono">{row.caption}</span>}
                        <span className="model-picker-check">{row.value === value && <i className="ph ph-check" />}</span>
                      </button>
                    )
                  })}
                </div>
              ))}
              {!flat.length && (
                <div className="model-picker-empty" role="status">
                  {query.trim() ? 'No matching models.' : 'No models yet.'}
                </div>
              )}
            </div>
            {!(directory?.providers ?? []).some((provider) => provider.key_set) && (
              <div className="model-picker-foot">
                <i className="ph ph-plugs" />
                <span>No provider is set up yet — add one in Settings → Models.</span>
              </div>
            )}
          </div>,
          document.body,
        )}
    </div>
  )
}
