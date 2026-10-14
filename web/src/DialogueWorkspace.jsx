import React, { useEffect, useMemo, useRef, useState } from 'react'
import { ArrowDown, ArrowRight, Check, CaretRight, PauseCircle, PlayCircle, FileXls, ChatCircleDots, ArrowClockwise, Sparkle, UploadSimple } from '@phosphor-icons/react'

async function api(path, options = {}) {
  const response = await fetch(path.replace(/^\/api(?=\/|$)/, '/dialogue-api'), options)
  const body = await response.json().catch(() => ({}))
  if (!response.ok) {
    const detail = body.detail
    const fields = {customer_count:'参与者数量',target_messages:'消息总数',language:'输出语言',primary_goal:'对话目标',goal_type:'目标类型'}
    const reasons = Array.isArray(detail) ? detail.map(item => {
      const name = fields[item.loc?.at(-1)] || '输入内容'
      if (item.type === 'missing') return `${name}不能为空`
      if (item.type === 'greater_than_equal') return `${name}不能小于${item.ctx?.ge}`
      if (item.type === 'less_than_equal') return `${name}不能大于${item.ctx?.le}`
      if (item.type?.includes('int')) return `${name}需要填写整数`
      return `${name}格式不正确，请检查后重试`
    }).join('；') : '请求未完成，请检查输入或本地服务'
    throw new Error(typeof detail === 'string' ? detail : reasons)
  }
  return body
}

const steps = ['导入母本', '确认目标', '设置角色', '模拟对话']

function Card({ title, eyebrow, children, className = '' }) {
  return <section className={`card ${className}`}><div className="card-head"><span>{eyebrow}</span><h2>{title}</h2></div>{children}</section>
}

function Input({ label, value, onChange, hint, ...props }) {
  return <label className="field"><span>{label}</span><input value={value} onChange={e => onChange(e.target.value)} {...props} />{hint && <small>{hint}</small>}</label>
}

function Bubble({ message, language }) {
  const fixed = message.sender === '坐席'
  return <div className={`message ${fixed ? 'message-fixed' : ''}`}>
    <div className="avatar">{fixed ? '席' : message.sender.startsWith('客户') ? '客' : '号'}</div>
    <div className="message-body"><div className="message-meta"><strong>{message.sender}</strong><span>#{message.seq}</span>{fixed && <em>引导坐席</em>}{message.stage && message.stage !== '自然群聊' && <em>{message.stage}</em>}</div>
      <div className="bubble">{message.image_code && <div className="image-placeholder">图片占位 <small>{message.image_code}</small></div>}{message.content && <p lang={language} dir="auto">{message.content}</p>}</div>
      {message.reply_to && <div className="reply-ref">回复 #{message.reply_to}</div>}
    </div>
  </div>
}

function CharacterMemory({ run, name, session }) {
  const currentEvidenceValid = item => item.evidence.every(quote => run.messages.some(message => message.seq === quote.seq && message.sender === quote.speaker && message.content === quote.quote))
  const recalled = (session.long_term_memory || []).filter(item => !item.from_current_run || currentEvidenceValid(item))
  const reflections = (run.memory_reflections || []).filter(item => item.role === name && item.valid !== false && currentEvidenceValid(item))
  const decisions = (run.public_decisions || []).filter(item => item.role === name)
  return <div className="character-memory">
    {decisions.length > 0 && <details><summary>当前有 {decisions.length} 项明确选择</summary>{decisions.map(item => <div key={item.subject}><p><strong>{item.subject}</strong>：{item.summary}</p><blockquote>#{item.evidence.seq} {item.evidence.content}</blockquote></div>)}</details>}
    {recalled.length > 0 && <details><summary>想起 {recalled.length} 条相关经历</summary>{recalled.map(item => <div key={item.id}><p>{item.insight}</p><small>{item.from_current_run ? '来自本轮' : '来自过去的剧本'}{item.peer_role ? ` · 与${item.peer_role}有关` : ''}</small><blockquote>{item.evidence.map(quote => <p key={quote.seq}>#{quote.seq} {quote.speaker}：{quote.quote}</p>)}</blockquote></div>)}</details>}
    {reflections.length > 0 && <details><summary>本轮形成 {reflections.length} 条认识</summary>{reflections.slice(-3).map(item => <div key={item.id}><p>{item.insight}</p><small>依据 {item.evidence.map(quote => `#${quote.seq}`).join('、')}</small></div>)}</details>}
  </div>
}

function EventPlan({run}) {
  if (!run.vimax_events?.length) return null
  return <Card eyebrow="本轮规划" title="对话安排"><p className="muted">以下是计划发生的交流。实际进展以群聊中说出和完成的内容为准。</p><details className="story-arc"><summary>查看 {run.vimax_events.length} 个事件</summary><ol>{run.vimax_events.map(event => <li key={event.index}><strong>{event.description}</strong><small>第 {event.message_start}—{event.message_end} 条 · {event.roles.join('、')}</small><p>起因：{event.cause}</p><p>{event.process_chain.join(' → ')}</p><p>变化：{event.outcome}</p></li>)}</ol></details></Card>
}

export default function DialogueWorkspace({ runId, startFresh = false, onRunChanged }) {
  const [step, setStep] = useState(0)
  const [template, setTemplate] = useState(null)
  const [goal, setGoal] = useState(null)
  const [customerCount, setCustomerCount] = useState(10)
  const [target, setTarget] = useState(60)
  const [language, setLanguage] = useState('zh-CN')
  const [dialogueModel, setDialogueModel] = useState('gpt-5.5')
  const [reasoningEffort, setReasoningEffort] = useState('')
  const [personas, setPersonas] = useState({})
  const [personaCards, setPersonaCards] = useState({})
  const [personaLibrary, setPersonaLibrary] = useState([])
  const [personaNames, setPersonaNames] = useState({})
  const [selectedPersonaIds, setSelectedPersonaIds] = useState({})
  const [personaSource, setPersonaSource] = useState('')
  const [personaStrength, setPersonaStrength] = useState(0.85)
  const [storyNotes, setStoryNotes] = useState('')
  const [storyWorld, setStoryWorld] = useState('')
  const [memoryEnabled, setMemoryEnabled] = useState(true)
  const [characterIds, setCharacterIds] = useState({})
  const [memoryNotice, setMemoryNotice] = useState('')
  const [personaLoading, setPersonaLoading] = useState(false)
  const [run, setRun] = useState(null)
  const [modelReady, setModelReady] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [dragging, setDragging] = useState(false)
  const bottom = useRef(null)
  const personaEdits = useRef({})

  useEffect(() => { api('/api/health').then(h => { setModelReady(h.model_configured); if (!runId && !window.localStorage.getItem('vimax-dialogue-run-id')) { if (h.model) setDialogueModel(h.model); setReasoningEffort(h.reasoning_effort || '') } }).catch(() => {}) }, [])
  useEffect(() => {
    if (startFresh && !runId) return
    let cancelled = false
    const id = runId || new URLSearchParams(window.location.search).get('run') || window.localStorage.getItem('vimax-dialogue-run-id')
    if (!id) return
    api(`/api/simulations/${id}`).then(async previous => {
      const saved = await api(`/api/templates/${previous.template_id}`)
      if (cancelled) return
      const messages = saved.source.messages
      const stats = { messages: messages.length, seat_messages: messages.filter(item => item.sender === '坐席').length,
        accounts: saved.source.accounts, image_codes: messages.filter(item => item.image_code).length }
      const evidence = messages.filter(item => saved.analysis.evidence_rows?.includes(item.source_row)).map(item => ({ row: item.source_row, sender: item.sender, content: item.content }))
      setTemplate({ ...saved, stats, evidence }); setGoal(saved.analysis)
      setCustomerCount(previous.config.customer_count); setTarget(previous.config.target_messages)
      setPersonas(previous.config.personas || {}); setDialogueModel(previous.config.dialogue_model || 'gpt-5.5')
      setReasoningEffort(previous.config.reasoning_effort || '')
      setPersonaCards(previous.config.persona_cards || {})
      personaEdits.current = Object.fromEntries(Object.entries(previous.config.persona_cards || {}).map(([role, card]) => [role, Object.fromEntries(Object.keys(card).map(field => [field, true]))]))
      setPersonaSource('saved'); setSelectedPersonaIds(previous.config.persona_sources || {})
      setPersonaStrength(previous.config.persona_strength ?? 0.85)
      setStoryNotes(previous.config.story_notes || '')
      setStoryWorld(previous.config.story_world === previous.template_id ? '' : previous.config.story_world || '')
      setMemoryEnabled(previous.config.memory_enabled ?? true)
      setCharacterIds(previous.config.character_ids || {})
      setLanguage(previous.config.language === '中文' ? 'zh-CN' : previous.config.language || 'zh-CN')
      setRun(previous); setStep(3)
    }).catch(() => { if (!cancelled) window.localStorage.removeItem('vimax-dialogue-run-id') })
    return () => { cancelled = true }
  }, [runId, startFresh])
  useEffect(() => {
    if (step !== 2 || !template?.id || Number(customerCount) < 1 || Number(customerCount) > 20 || !/^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$/.test(language.trim())) return
    let cancelled = false
    setPersonaLoading(true)
    const timer = setTimeout(() => {
      api(`/api/templates/${template.id}/personas`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ customer_count: Number(customerCount), language: language.trim() }) })
        .then(result => {
          if (cancelled) return
          setPersonas(result.personas)
          setPersonaSource(result.source || 'fallback')
          setSelectedPersonaIds(previous => Object.fromEntries(Object.entries(result.persona_sources || {}).map(([role, id]) => [role, personaEdits.current[role] ? previous[role] || '' : id])))
          setPersonaNames(previous => Object.fromEntries(Object.entries(result.persona_names || {}).map(([role, name]) => [role, personaEdits.current[role] ? previous[role] || '' : name])))
          setPersonaCards(previous => {
            const next = {}
            for (const [role, card] of Object.entries(result.persona_cards || {})) {
              next[role] = { ...card }
              for (const field of Object.keys(card)) {
                if (personaEdits.current[role]?.[field]) next[role][field] = previous[role]?.[field] ?? card[field]
              }
            }
            return next
          })
        })
        .catch(e => { if (!cancelled) setError(e.message) })
        .finally(() => { if (!cancelled) setPersonaLoading(false) })
    }, 300)
    return () => { cancelled = true; clearTimeout(timer) }
  }, [step, template?.id, customerCount, language])
  useEffect(() => {
    if (!run?.id || ['completed', 'early_end', 'failed'].includes(run.status)) return
    const source = new EventSource(`/dialogue-api/simulations/${run.id}/events`)
    source.onmessage = event => {
      const value = JSON.parse(event.data)
      setRun(value)
      if (['completed', 'early_end', 'failed'].includes(value.status)) onRunChanged?.(value.id)
      if (['completed', 'early_end', 'failed'].includes(value.status)) source.close()
    }
    // EventSource reconnects automatically after temporary connection loss.
    source.onerror = () => {}
    return () => source.close()
  }, [run?.id, run?.status])
  useEffect(() => { bottom.current?.scrollIntoView({ behavior: 'smooth', block: 'end' }) }, [run?.messages?.length])

  const allRoles = useMemo(() => ['坐席', ...(template?.stats?.accounts || []), ...Array.from({ length: Number(customerCount) || 0 }, (_, i) => `客户${i + 1}`)], [template, customerCount])
  const minimum = useMemo(() => template?.minimum_by_customer_count?.[String(customerCount)] || template?.minimum_for_one_customer || 0, [template, customerCount])
  const languageValid = /^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$/.test(language.trim())
  useEffect(() => {
    if (step !== 2 || !languageValid) return
    let cancelled = false
    api(`/api/persona-library?language=${encodeURIComponent(language.trim())}`)
      .then(result => { if (!cancelled) setPersonaLibrary(result.entries || []) })
      .catch(e => { if (!cancelled) setError(e.message) })
    return () => { cancelled = true }
  }, [step, language, languageValid])

  function applyLibraryCard(role, entry) {
    const card = { ...entry.card }
    personaEdits.current[role] = Object.fromEntries(Object.keys(card).map(field => [field, true]))
    setPersonaCards(previous => ({ ...previous, [role]: card }))
    setPersonas(previous => ({ ...previous, [role]: card.identity }))
    setPersonaNames(previous => ({ ...previous, [role]: entry.name }))
    setSelectedPersonaIds(previous => ({ ...previous, [role]: entry.id }))
    setPersonaSource('library')
  }

  async function continueCharacters() {
    if (!template) return
    setBusy(true); setError(''); setMemoryNotice('')
    try {
      const result = await api(`/api/character-memory?story_world=${encodeURIComponent(storyWorld.trim() || template.id)}`)
      const profiles = (result.profiles || []).filter(item => allRoles.includes(item.role_name))
      if (!profiles.length) { setMemoryNotice('这组还没有保存的人物，当前角色会从本次开始积累经历。'); return }
      for (const profile of profiles) {
        const role = profile.role_name
        personaEdits.current[role] = Object.fromEntries(Object.keys(profile.card).map(field => [field, true]))
      }
      setPersonaCards(previous => ({ ...previous, ...Object.fromEntries(profiles.map(item => [item.role_name, item.card])) }))
      setPersonas(previous => ({ ...previous, ...Object.fromEntries(profiles.map(item => [item.role_name, item.card.identity])) }))
      setCharacterIds(Object.fromEntries(profiles.map(item => [item.role_name, item.id])))
      setMemoryEnabled(true); setPersonaSource('continued')
      setMemoryNotice(`已沿用 ${profiles.length} 位人物；有 ${profiles.reduce((sum, item) => sum + item.reflection_count, 0)} 条通过检查的历史认识可供检索。`)
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  async function saveLibraryCard(role) {
    const name = (personaNames[role] || '').trim()
    const card = personaCards[role]
    if (!name || !card?.identity?.trim()) { setError('请先填写人格名称和稳定倾向'); return }
    setBusy(true); setError('')
    try {
      const selectedId = selectedPersonaIds[role]
      const updating = selectedId?.startsWith('custom-')
      const entry = await api(updating ? `/api/persona-library/${selectedId}` : '/api/persona-library', { method: updating ? 'PUT' : 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name, kind: role === '坐席' ? 'seat' : 'participant', language: language.trim(), card }) })
      setPersonaLibrary(previous => updating ? previous.map(item => item.id === entry.id ? entry : item) : [...previous, entry])
      setSelectedPersonaIds(previous => ({ ...previous, [role]: entry.id }))
      setPersonaSource('library')
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  async function deleteLibraryCard(role) {
    const id = selectedPersonaIds[role]
    if (!id?.startsWith('custom-')) return
    setBusy(true); setError('')
    try {
      await api(`/api/persona-library/${id}`, { method: 'DELETE' })
      setPersonaLibrary(previous => previous.filter(entry => entry.id !== id))
      setSelectedPersonaIds(previous => ({ ...previous, [role]: '' }))
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  function assignLibraryCards() {
    const seats = personaLibrary.filter(entry => entry.kind === 'seat')
    const participants = personaLibrary.filter(entry => entry.kind === 'participant')
    if (!seats.length || !participants.length) return
    const offset = Math.floor(Math.random() * participants.length)
    allRoles.forEach((role, index) => applyLibraryCard(role, role === '坐席' ? seats[offset % seats.length] : participants[(offset + index - 1) % participants.length]))
  }

  async function upload(file) {
    if (!file) return
    setBusy(true); setError('')
    try {
      const form = new FormData(); form.append('file', file)
      const result = await api('/api/templates/upload', { method: 'POST', body: form })
      window.localStorage.removeItem('vimax-dialogue-run-id')
      personaEdits.current = {}; setTemplate(result); setGoal(result.analysis); setTarget(Math.max(60, result.minimum_for_one_customer + 8)); setPersonas({}); setPersonaCards({}); setPersonaLibrary([]); setPersonaNames({}); setSelectedPersonaIds({}); setPersonaSource(''); setStoryNotes(''); setCharacterIds({}); setMemoryNotice(''); setRun(null); setStep(1)
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  async function confirmGoal() {
    setBusy(true); setError('')
    try {
      const result = await api(`/api/templates/${template.id}/goal`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({
        goal_type: goal.goal_type, primary_goal: goal.primary_goal, secondary_goals: goal.secondary_goals,
        completion_actions: goal.completion_actions, workflow_steps: goal.workflow_steps,
      }) })
      setGoal(result); setStep(2)
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  async function start(configOverride) {
    setBusy(true); setError('')
    try {
      const created = await api('/api/simulations', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(configOverride || {
        template_id: template.id, customer_count: Number(customerCount), target_messages: Number(target), personas, persona_cards: personaCards, persona_sources: selectedPersonaIds,
        persona_versions: Object.fromEntries(Object.entries(selectedPersonaIds).map(([role, id]) => [role, personaLibrary.find(entry => entry.id === id)?.version || 1])),
        persona_strength: personaStrength, language: language.trim(), dialogue_model: dialogueModel, reasoning_effort: reasoningEffort || null, story_notes: storyNotes,
        story_world: storyWorld.trim(), memory_enabled: memoryEnabled, character_ids: characterIds,
      }) })
      window.localStorage.setItem('vimax-dialogue-run-id', created.id)
      onRunChanged?.(created.id)
      setRun({ id: created.id, status: 'running', messages: [], revision: 0 }); setStep(3)
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  async function action(path, body) {
    setError('')
    try {
      const result = await api(`/api/simulations/${run.id}/${path}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined })
      if (path === 'retry') { window.localStorage.setItem('vimax-dialogue-run-id', result.id); setRun({ id: result.id, status: 'running', messages: [], revision: 0 }); return }
      const refreshed = await api(`/api/simulations/${run.id}`)
      setRun(refreshed)
    } catch (e) { setError(e.message) }
  }

  const finished = ['completed', 'early_end', 'failed'].includes(run?.status)
  const valid = finished && run.status !== 'failed' && !((run.validation || []).length || (run.errors || []).length)
  const goalMet = run?.outcome_review?.met !== false
  const reviewing = run?.status === 'running' && Boolean(run.review_progress?.phase)
  const runStateLabel = reviewing
    ? ({ planning: '正在规划群聊事件', writing: '正在写作完整对话', validate: '正在检查原值与条数', broadcast: '正在保存角色经历', review: '正在检查对话', repair: '正在修订问题', verify: '正在复查修订' })[run.review_progress?.phase] || '正在检查对话'
    : run?.status === 'early_end' && !goalMet ? '目标未达成'
      : ({ running: '正在生成', paused: '已暂停', completed: '已完成', early_end: '客户提前退出', failed: '生成失败' })[run?.status] || run?.status

  return <div className="dialogue-workspace">
    <nav className="dialogue-steps">{steps.map((name, index) => <button key={name} onClick={() => index <= step && setStep(index)} className={step === index ? "active" : ""} disabled={index > step}>{index + 1}. {name}</button>)}</nav>
    <main className="main"><header className="topbar"><div><span className="overline">群聊创作</span><h1>{steps[step]}</h1></div><span className="top-pill">{template?.filename || '尚未选择母本'}</span></header>
      {error && <div className="error-banner" role="alert">{error}</div>}

      {step === 0 && <div className="intro-layout"><div className="intro-copy"><span className="eyebrow">从一份母本开始</span><h2>提取目标，<br /><i>排演新的对话。</i></h2><p>上传九列格式的 Excel。系统会提取目标和关键代码，再让坐席与重新设置的参与者在同一个群聊中重新对话。</p><div className="intro-note"><Sparkle size={18} />坐席按原目标引导，链接与代码保持原值</div></div>
        <div className={`upload-zone ${dragging ? 'dragging' : ''}`} onDragOver={e => { e.preventDefault(); setDragging(true) }} onDragLeave={() => setDragging(false)} onDrop={e => { e.preventDefault(); setDragging(false); upload(e.dataTransfer.files[0]) }}>
          <UploadSimple size={40} weight="thin" /><h3>拖入母本 Excel</h3><p>支持与示例相同的九列结构，单文件不超过 10 MB</p><label className="button primary">{busy ? '正在读取…' : '选择 XLSX 文件'}<input type="file" accept=".xlsx" hidden onChange={e => upload(e.target.files[0])} /></label>
          <div className="format-preview"><FileXls size={17} />顺序 · 发送人 · 回复 · 消息类型 · 延时 · 内容 · 图片码 · 图片方式 · 替换日期</div>
        </div></div>}

      {step === 1 && goal && <div className="two-col"><Card eyebrow="01 / 目标判断" title="这份母本想完成什么？"><div className="analysis-notice">这是对母本文字的判断。请核对后确认；奖励或付款说法仅代表剧本内容。</div>
        <Input label="目标类型" value={goal.goal_type} onChange={v => setGoal({ ...goal, goal_type: v })} />
        <label className="field"><span>主要目标</span><textarea rows="4" value={goal.primary_goal} onChange={e => setGoal({ ...goal, primary_goal: e.target.value })} /></label>
        <label className="field"><span>次要目标（每行一个）</span><textarea rows="3" value={goal.secondary_goals.join('\n')} onChange={e => setGoal({ ...goal, secondary_goals: e.target.value.split('\n').filter(Boolean) })} /></label>
        <label className="field"><span>完成动作（每行一个）</span><textarea rows="4" value={goal.completion_actions.join('\n')} onChange={e => setGoal({ ...goal, completion_actions: e.target.value.split('\n').filter(Boolean) })} /></label>
        <label className="field"><span>操作顺序（每行一步）</span><textarea rows="5" value={(goal.workflow_steps || []).join('\n')} onChange={e => setGoal({ ...goal, workflow_steps: e.target.value.split('\n').filter(Boolean) })} /></label>
        <button className="button primary full" disabled={busy || !goal.primary_goal.trim()} onClick={confirmGoal}>确认目标并设置角色 <ArrowRight size={18} /></button></Card>
        <Card eyebrow="02 / 原表依据" title="判断来自这些位置"><div className="stat-strip"><div><strong>{template.stats.messages}</strong><span>原始消息</span></div><div><strong>{template.stats.seat_messages}</strong><span>原坐席行</span></div><div><strong>{template.stats.accounts.length}</strong><span>原有账号</span></div></div><div className="evidence-list">{(template.evidence || []).map(item => <div key={item.row}><span>第 {item.row} 行</span><p>{item.content}</p></div>)}</div><p className="muted">图片码 {template.stats.image_codes} 个；原表只用于提取目标、角色与必要内容，生成时重新安排对话。</p></Card></div>}

      {step === 2 && <div className="two-col config-layout"><Card eyebrow="03 / 模拟参数" title="安排参与者"><div className="stat-strip"><div><strong>1</strong><span>引导坐席</span></div><div><strong>{template.stats.accounts.length}</strong><span>母本账号</span></div><div><strong>{customerCount}</strong><span>参与者</span></div></div>
        <div className="input-grid"><Input label="参与者数量" type="number" min="1" max="20" value={customerCount} onChange={setCustomerCount} /><Input label="最终总消息条数" type="number" min={minimum} max="500" value={target} onChange={setTarget} /></div><Input label="对话输出语言" value={language} onChange={setLanguage} list="language-options" maxLength="35" hint="填写语言地区代码；角色发言使用该语言，母本链接和代码保持原值。" /><datalist id="language-options"><option value="zh-CN" label="简体中文" /><option value="zh-TW" label="繁體中文" /><option value="en-US" label="English (US)" /><option value="en-GB" label="English (UK)" /><option value="ja-JP" label="日本語" /><option value="ko-KR" label="한국어" /><option value="es-ES" label="Español" /><option value="fr-FR" label="Français" /><option value="de-DE" label="Deutsch" /><option value="pt-BR" label="Português" /><option value="ar-SA" label="العربية" /></datalist><label className="field"><span>对话生成模型</span><select value={dialogueModel} onChange={e => setDialogueModel(e.target.value)}><option value="gpt-6-sol">GPT-6 Sol</option><option value="gpt-5.6-luna">GPT-5.6 Luna</option><option value="gpt-5.5">GPT-5.5</option><option value="gpt-5.6-terra">GPT-5.6 Terra</option></select></label><label className="field"><span>推理强度</span><select value={reasoningEffort} onChange={e => setReasoningEffort(e.target.value)}><option value="">沿用模型设置</option><option value="low">低</option><option value="medium">中</option><option value="high">高</option></select></label><div className="analysis-notice">当前至少需要 {minimum} 条，母本账号不限定出场，发言者按上下文选择，以坐席内容目标为准。</div>
        <div className="memory-settings"><Input label="人物经历分组" value={storyWorld} onChange={value => { setStoryWorld(value); setCharacterIds({}); setMemoryNotice('') }} maxLength="100" placeholder="留空使用当前母本；也可填写同一群人物的名称" hint="同组人物可延续偏好与关系，不同组各自积累经历。" /><label className="memory-toggle"><input type="checkbox" checked={memoryEnabled} onChange={e => setMemoryEnabled(e.target.checked)} />沿用并积累人物经历</label><button className="button secondary full" type="button" disabled={busy || personaLoading || !memoryEnabled} onClick={continueCharacters}>沿用这组已保存的人物</button>{memoryNotice && <p className="muted">{memoryNotice}</p>}</div>
        <label className="field"><span>补充情节点（长篇可选，每行一件）</span><textarea rows="4" maxLength="3000" value={storyNotes} onChange={e => setStoryNotes(e.target.value)} placeholder="例如：一位晚到的成员拿不准时间安排；先试过的人分享自己为什么继续或停下。" /><small>长篇会先从母本规划事件线；这里填写的情节点优先纳入。链接、联系人和代码仍必须来自母本。</small></label><button className="button primary full" disabled={busy || personaLoading || !languageValid || Number(target) < minimum || Number(customerCount) < 1} onClick={() => start()}>开始模拟 <PlayCircle size={18} /></button></Card>
        <Card eyebrow="04 / 人格知识库" title="让每个角色说话不同"><p className="analysis-notice">先打招呼、闲聊暖场，再有人询问群用途，由坐席解释后进入正题。按人格自然接话，不要求每个人轮流报到。</p><p className="muted">{personaLoading ? '正在为每个角色设置人格…' : personaSource === 'library' ? '已从人格知识库选用；每项仍可修改。' : personaSource === 'fallback' ? '模型未及时给出有效建议，已填入可编辑的默认角色卡。' : '已设置角色卡；可从知识库换人设，或逐项修改。'}</p><button className="button secondary full" type="button" disabled={!personaLibrary.length} onClick={assignLibraryCards}>从 {personaLibrary.length} 种人格中重新分配</button><label className="field"><span>人格介入值 {Math.round(personaStrength * 100)}%</span><input type="range" min="0" max="100" step="5" value={Math.round(personaStrength * 100)} onChange={e => setPersonaStrength(Number(e.target.value) / 100)} /><small>影响角色何时接话、作什么选择及说话方式；口语习惯只偶尔出现，母本事实仍按原值校验。</small></label><div className="persona-list">{allRoles.map(role => <div className="persona-card" key={role}><strong>{role}</strong><label className="field"><span>从人格知识库选用</span><select value={selectedPersonaIds[role] || ''} onChange={e => { const entry = personaLibrary.find(item => item.id === e.target.value); if (entry) applyLibraryCard(role, entry) }}><option value="">自定义或选择人设…</option>{[['推荐 · 生活化表达', entry => entry.recommended], ['更多人物表达', entry => entry.inspiration && !entry.recommended], ['通用与自定义人格', entry => !entry.inspiration]].map(([label, matches]) => <optgroup key={label} label={label}>{personaLibrary.filter(entry => entry.kind === (role === '坐席' ? 'seat' : 'participant') && matches(entry)).map(entry => <option key={entry.id} value={entry.id}>{entry.name}</option>)}</optgroup>)}</select>{personaLibrary.find(entry => entry.id === selectedPersonaIds[role])?.source_url && <small>借鉴表达与反应，角色仍是当前群友。<a href={personaLibrary.find(entry => entry.id === selectedPersonaIds[role]).source_url} target="_blank" rel="noreferrer">查看人格来源</a></small>}</label>{[['identity', '稳定倾向'], ['priority', '最在意的事'], ['decision_rule', '何时继续或拒绝'], ['reaction_cues', '遇到具体情境时的反应'], ['voice', '说话节奏'], ['social_style', '接话方式'], ['speech_habits', '口语习惯 / 偶尔用的短语'], ['speech_avoid', '避免重复的说法']].map(([field, label]) => <label className="field" key={field}><span>{label}</span><input maxLength={field === 'reaction_cues' ? 300 : 180} placeholder={field === 'identity' ? '描述这个人，而不是业务流程' : '可留空'} value={personaCards[role]?.[field] ?? (field === 'identity' ? personas[role] || '' : '')} onChange={e => { personaEdits.current[role] = { ...personaEdits.current[role], [field]: true }; setPersonaCards(previous => ({ ...previous, [role]: { ...previous[role], [field]: e.target.value } })) }} /></label>)}<div className="persona-save"><input maxLength="60" value={personaNames[role] || ''} onChange={e => setPersonaNames(previous => ({ ...previous, [role]: e.target.value }))} placeholder="给这张人格卡命名" /><button className="button secondary" type="button" disabled={busy} onClick={() => saveLibraryCard(role)}>{selectedPersonaIds[role]?.startsWith('custom-') ? '更新库中人格' : '保存到库'}</button>{selectedPersonaIds[role]?.startsWith('custom-') && <button className="button ghost" type="button" disabled={busy} onClick={() => deleteLibraryCard(role)}>删除所选</button>}</div></div>)}</div></Card></div>}

      {step === 3 && run && <div className="simulation-layout"><Card eyebrow="05 / 群聊排演" title="模拟对话" className="chat-card"><div className="chat-toolbar"><span className={`run-state ${run.status}`}>{runStateLabel}</span><span>{run.config?.language || language} · {run.messages?.length || 0} / {run.config?.target_messages || target} 条</span></div><div className="run-model">模型：{run.config?.dialogue_model || dialogueModel} · 推理强度：{({low: '低', medium: '中', high: '高'})[run.config?.reasoning_effort] || '模型默认'}</div>
        {run.pause_reason && <div className="error-banner pause-banner">{run.pause_reason}</div>}
        <div className="chat-scroll">{!run.messages?.length && <div className="chat-empty">正在安排第一条消息…</div>}{(run.messages || []).map(message => <Bubble message={message} language={run.config?.language || language} key={message.seq} />)}<div ref={bottom} /></div>
        <div className="chat-actions">{run.status === 'running' && <button className="button ghost" onClick={() => action('pause')}><PauseCircle size={17} />暂停</button>}{run.status === 'paused' && <button className="button ghost" onClick={() => action('resume')}><PlayCircle size={17} />继续</button>}{run.status === 'failed' && <button className="button ghost" onClick={() => action('resume')}><PlayCircle size={17} />{run.cursor < (run.schedule?.length || 0) ? '从失败处继续' : '重新复核'}</button>}{finished && <button className="button ghost" onClick={() => action('retry')}><ArrowClockwise size={17} />重试生成</button>}</div></Card>
        <div className="right-rail"><EventPlan run={run} />{run.story_arc?.length > 0 && <Card eyebrow="剧情规划" title="本轮事件线"><p className="muted">{['model', 'model-revised', 'model-topics', 'cached-model-topics'].includes(run.story_arc_source) ? '根据母本规划' : run.story_arc_source === 'topic-poor' ? '话题多样性不足' : run.story_arc_source === 'hybrid' ? '母本与通用事件结合' : '使用通用事件线'} · {run.story_arc.length} 个情节点。事件只在角色公开说出后才算发生。</p><details className="story-arc"><summary>查看全部情节点</summary><ol>{run.story_arc.map((beat, index) => <li key={index} className={!(run.scene_progress || []).some(item => item.id === index && item.state === "resolved") && index === run.story_arc.findIndex((beat, id) => !(run.scene_progress || []).some(item => item.id === id && item.state === "resolved") && (beat.kind !== "closure" || run.cursor >= run.schedule.length - 3)) ? 'current' : ''}>{beat.event}{beat.characters?.length > 0 && <small>建议：{beat.characters.join('、')}</small>}</li>)}</ol></details></Card>}<Card eyebrow="06 / 人物会话" title="独立角色会话"><p className="muted">每个角色保留自己的会话记忆，共同读取群聊记录。客户按人格和对话自行决定是否继续、拒绝或离开。</p>{run.config?.memory_enabled && <p className="muted">{({ semantic: '已启用多语言语义检索', lexical: '当前使用文字检索', empty: '人物从本轮开始积累经历' })[run.memory_status?.mode] || '正在读取相关人物经历'} · 本轮想起 {run.memory_status?.recalled || 0} 条。历史动作不计入本轮进度。</p>}<div className="agent-session-list">{Object.entries(run.agent_sessions || {}).map(([name, session]) => <div className="agent-session-item" key={name}><div className="agent-session-head"><strong>{name}</strong><span>{session.turns} 次发言</span></div><details><summary>本轮记住 {session.memory_events?.length || 0} 件事</summary>{(session.memory_events || []).slice(-5).map((event, index) => <p key={`${event.seq}-${event.kind}-${index}`}>#{event.seq} {({ own_action: '自己做过', own_decision: '自己的决定', said_to: `对${event.peer}说`, heard_from: `${event.peer}对我说` })[event.kind] || '经历'}：{event.text}</p>)}</details><CharacterMemory run={run} name={name} session={session} /></div>)}</div></Card>
          <Card eyebrow="07 / 检查与导出" title="生成结果"><div className={`result-banner ${valid && goalMet ? 'good' : ''}`}>{run.status === 'completed' ? '成功路径已完成' : run.status === 'early_end' ? goalMet ? '客户提前结束分支' : '本轮目标未达成' : run.status === 'failed' ? '生成未通过' : '模拟进行中'}</div>{!goalMet && run.outcome_review?.reason && <p className="validation-error">{run.outcome_review.reason}</p>}{[...(run.validation || []), ...(run.errors || [])].map((item, i) => <p className="validation-error" key={i}>{item}</p>)}<div className="download-row"><a className={`button secondary ${valid ? '' : 'disabled'}`} href={valid ? `/dialogue-api/simulations/${run.id}/export/xlsx` : undefined}><ArrowDown size={17} />下载 Excel</a><a className={`button secondary ${valid ? '' : 'disabled'}`} href={valid ? `/dialogue-api/simulations/${run.id}/export/json` : undefined}><ArrowDown size={17} />下载 JSON</a></div></Card></div></div>}
    </main>
  </div>
}
