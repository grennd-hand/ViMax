// Completed and immutable published histories can be reused briefly.
export function createRecordLoader(request, {ttl = 60_000, now = Date.now} = {}) {
  const cache = new Map();
  return {
    clear() { cache.clear(); },
    async load(id, {signal} = {}) {
      if (signal?.aborted) throw new DOMException('Aborted', 'AbortError');
      const cached = cache.get(id);
      if (cached && cached.expires > now()) return cached.value;
      const run = await request(`/api/simulations/${encodeURIComponent(id)}?view=workspace`, {signal});
      if (signal?.aborted) throw new DOMException('Aborted', 'AbortError');
      const saved = run.template || await request(`/api/templates/${encodeURIComponent(run.template_id)}`, {signal});
      if (signal?.aborted) throw new DOMException('Aborted', 'AbortError');
      if (!run.config || !Array.isArray(saved?.source?.messages) || !saved.analysis) throw new Error('这份剧本的关联母本不完整，请重新选择记录。');
      const messages = saved.source.messages;
      const evidenceRows = new Set(saved.analysis.evidence_rows || []);
      const template = {...saved,
        stats: {messages: messages.length, seat_messages: messages.filter(item => item.sender === '坐席').length, accounts: saved.source.accounts || [], image_codes: messages.filter(item => item.image_code).length},
        evidence: messages.filter(item => evidenceRows.has(item.source_row)).map(item => ({row: item.source_row, sender: item.sender, content: item.content})),
      };
      const {template: embedded, ...previous} = run;
      const value = {run: previous, template};
      if (run.publication?.read_only === true || ['completed', 'early_end'].includes(run.status)) {
        if (cache.size >= 20) cache.delete(cache.keys().next().value);
        cache.set(id, {value, expires: now() + ttl});
      }
      return value;
    },
  };
}
