import React from 'react'
import {renderToStaticMarkup} from 'react-dom/server'
import {describe, expect, it, vi} from 'vitest'
import {archiveLabel, ArchiveReviewNotice, RunActions, CreationOptions, MemoryOptions, VariationNotice} from './DialogueWorkspace'

const paused = {status: 'paused', cursor: 150, publication: {
  read_only: true, version: 'V06', human_verdict: 'alternative',
  human_summary: '两份全文审阅认为可作表达备选', program_review_state: 'deferred',
}}

it('explains independent random creation and reports actual corpus availability', () => {
  const html = renderToStaticMarkup(<CreationOptions mode="independent_random" randomizePersonas={true}
    onModeChange={() => {}} onRandomizeChange={() => {}} corpus={{available: true, conversations: 10000}} />)
  expect(html).toContain('独立随机')
  expect(html).toContain('10,000')
  expect(html).toContain('不沿用历史台词和故事')
  const missing = renderToStaticMarkup(<CreationOptions mode="independent_random" randomizePersonas={true}
    onModeChange={() => {}} onRandomizeChange={() => {}} corpus={{available: false}} />)
  expect(missing).toContain('尚未配置')
})

it('shows the new run actual style and sample count without relabeling old histories', () => {
  expect(renderToStaticMarkup(<VariationNotice run={paused} />)).toBe('')
  const html = renderToStaticMarkup(<VariationNotice run={{creative_variation: {style_label: '碎句接话 · 温和分歧'}, corpus_reference: {samples: [{}, {}, {}]}}} />)
  expect(html).toContain('碎句接话 · 温和分歧')
  expect(html).toContain('3 段公开短对话')
})

describe('creation memory options', () => {
  const props = {storyWorld: '既有群人物', memoryEnabled: true, onStoryWorldChange: vi.fn(),
    onMemoryChange: vi.fn(), onContinue: vi.fn(), busy: false, personaLoading: false, notice: '已沿用旧人物'}

  it('disables inherited bindings and clearly starts independent runs with new people', () => {
    const html = renderToStaticMarkup(<MemoryOptions {...props} mode="independent_random" />)
    expect(html).toContain('本份从新人物开始')
    expect(html).toContain('不读取过去人物的偏好、关系或经历')
    expect(html).toContain('每份自动新建人物经历分组')
    expect(html).not.toContain('既有群人物')
    expect(html).not.toContain('已沿用旧人物')
    const controls = html.match(/<(?:input|button)\b[^>]*>/g)
    expect(controls).toHaveLength(3)
    for (const control of controls) expect(control).toContain('disabled=""')
    expect(controls.find(control => control.includes('type="checkbox"'))).not.toContain('checked')
  })

  it('keeps fixed-mode bindings and history settings available', () => {
    const html = renderToStaticMarkup(<MemoryOptions {...props} mode="fixed" />)
    expect(html).toContain('value="既有群人物"')
    expect(html).toContain('已沿用旧人物')
    expect(html).toContain('checked=""')
    expect(html).not.toContain('disabled=""')
    expect(html).not.toContain('本份从新人物开始')
    expect(props.storyWorld).toBe('既有群人物')
    expect(props.memoryEnabled).toBe(true)
  })

  it('keeps fixed-mode continue disabled while busy or memory is turned off', () => {
    for (const state of [{busy: true}, {personaLoading: true}, {memoryEnabled: false}]) {
      const html = renderToStaticMarkup(<MemoryOptions {...props} {...state} mode="fixed" />)
      expect(html.match(/<button\b[^>]*>/)[0]).toContain('disabled=""')
    }
  })
})

describe('archived evolution histories', () => {
  it('uses explicit review labels and marks a user choice only for the boolean flag', () => {
    const run = {...paused, publication: {...paused.publication, method: 'independent_generation',
      series: '独立随机-20261015', display_label: '独立01', human_verdict: 'candidate',
      review_label: '完整双审候选', program_review_state: 'reviewed', selected: true}}
    const label = archiveLabel(run)
    const html = renderToStaticMarkup(<span>{label}</span>)
    expect(label).toBe('独立01 · 完整双审候选 · 模型已全文复核')
    expect(html).toContain('完整双审候选')
    expect(html).not.toContain('人工候选')
    expect(html).not.toContain('用户已选')
    for (const user_selected of [false, 'true', 1, undefined]) {
      expect(archiveLabel({...run, publication: {...run.publication, user_selected}})).not.toContain('用户已选')
    }
    const selected = {...run, publication: {...run.publication, user_selected: true}}
    expect(renderToStaticMarkup(<span>{archiveLabel(selected)}</span>)).toContain('完整双审候选 · 用户已选')
  })
  it('does not infer complete review for a new series without a review label', () => {
    const run = {...paused, publication: {...paused.publication, series: '独立随机-20261015', human_verdict: 'candidate'}}
    expect(renderToStaticMarkup(<span>{archiveLabel(run)}</span>)).toContain('待完整审阅')
    expect(archiveLabel(run)).not.toContain('人工候选')
    const byMethod = {...run, publication: {...run.publication, series: undefined},
      version_record: {method: 'independent_generation'}}
    expect(archiveLabel(byMethod)).toContain('待完整审阅')
  })
  it('keeps original candidate labels for legacy histories', () => {
    const run = {...paused, publication: {...paused.publication, human_verdict: 'candidate', selected: true}}
    expect(archiveLabel(run)).toBe('V06 · 人工候选 · 模型全文复核未执行')
  })
  it('separates human alternatives from deferred program review and unknown goal', () => {
    expect(archiveLabel(paused)).toBe('V06 · 表达备选 · 模型全文复核未执行')
    const html = renderToStaticMarkup(<ArchiveReviewNotice run={paused} />)
    expect(html).toContain(paused.publication.human_summary)
    expect(html).toContain('目标达成情况尚无完整模型复核结论')
    expect(html).not.toContain('成功路径已完成')
  })
  it('renders failed full drafts as failed even if a human liked the content', () => {
    const run = {...paused, status: 'failed', publication: {...paused.publication, human_verdict: 'candidate', program_review_state: 'reviewed'}}
    expect(archiveLabel(run)).toContain('模型已全文复核 · 程序未通过')
  })
  it('cannot trigger same-run resume or retry from archived paused/failed records', () => {
    const action = vi.fn()
    for (const status of ['paused', 'failed']) {
      const html = renderToStaticMarkup(<RunActions run={{...paused, status}} scheduleLength={150} action={action} />)
      expect(html).toContain('历史台词只读')
      expect(html).not.toContain('<button')
    }
    expect(action).not.toHaveBeenCalled()
  })
  it('keeps normal paused generation controls', () => {
    const html = renderToStaticMarkup(<RunActions run={{status: 'paused'}} scheduleLength={150} action={() => {}} />)
    expect(html).toContain('<button')
    expect(html).toContain('继续')
  })
  it('displays exact revision labels and hash-bound correction notes without replacing human history', () => {
    const revision = {...paused, publication: {...paused.publication, version: 'V32-R001'}}
    expect(archiveLabel(revision)).toContain('V32-R001')
    const original = {...paused, publication: {...paused.publication, version: 'V32',
      human_verdict: 'candidate', display_note: '原稿仅为时间配置备选', representative_version: 'V32-R001'}}
    const html = renderToStaticMarkup(<ArchiveReviewNotice run={original} />)
    expect(html).toContain('原稿仅为时间配置备选')
    expect(html).toContain('本编号正式内容代表：V32-R001')
    expect(original.publication.human_verdict).toBe('candidate')
  })
})
