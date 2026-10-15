import React from 'react'
import {renderToStaticMarkup} from 'react-dom/server'
import {describe, expect, it, vi} from 'vitest'
import {archiveLabel, ArchiveReviewNotice, RunActions} from './DialogueWorkspace'

const paused = {status: 'paused', cursor: 150, publication: {
  read_only: true, version: 'V06', human_verdict: 'alternative',
  human_summary: '两份全文审阅认为可作表达备选', program_review_state: 'deferred',
}}

describe('archived evolution histories', () => {
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
