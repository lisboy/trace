import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import type { RunQuestion } from "../types";

/**
 * 提问机制 UI（组长「拿不准就问」）。
 *
 * 两个区块，都放在输入框上方（和待办清单同区）：
 *  1) 待回答面板：问题 + 互斥选项 + 自由输入 + 「跳过 / 提交」；
 *     提交后后端会**唤醒同一个 run**（它一直原地暂停等你回答），组长带着答案接着做——
 *     不结束本轮、不起新 run、待办清单和轨迹都保留。
 *  2) 已回答记录（可折叠）：「提问 · 2/2 已回答」，点开能看到每题当时选了哪个方案 —— 留痕可追溯。
 */
export default function AskQuestion({
  sessionId,
  refreshKey,
  onAnswered,
}: {
  sessionId: string;
  refreshKey?: number;
  onAnswered?: () => void;
}) {
  const [items, setItems] = useState<RunQuestion[]>([]);
  const [picked, setPicked] = useState<Record<string, number | null>>({});
  const [texts, setTexts] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState("");
  const [showLog, setShowLog] = useState(false);

  useEffect(() => {
    let alive = true;
    api
      .questions(sessionId)
      .then((r: any) => {
        if (alive) setItems(r?.items || []);
      })
      .catch(() => {
        /* 忽略：失败不影响对话本身 */
      });
    return () => {
      alive = false;
    };
  }, [sessionId, refreshKey]);

  const pending = useMemo(() => items.filter((q) => q.status === "pending"), [items]);
  const answered = useMemo(() => items.filter((q) => q.status !== "pending"), [items]);

  async function submit(q: RunQuestion, skip = false) {
    setErr("");
    const idx = skip ? -1 : picked[q.id] ?? null;
    const text = (texts[q.id] || "").trim();
    if (!skip && idx === null && !text) {
      setErr("请选择一个方案，或直接写出你的决定");
      return;
    }
    setBusy(q.id);
    try {
      await api.answerQuestion(q.id, skip
        ? { option_index: -1, text: text || undefined }
        : { option_index: idx, text: text || undefined });
      setItems((cur) => cur.map((x) =>
        x.id === q.id
          ? { ...x, status: skip ? "skipped" : "answered",
              answer_index: skip ? -1 : idx, answer_text: text || x.options[idx ?? -1]?.label }
          : x));
      onAnswered?.();
    } catch (e: any) {
      setErr(e?.message || "提交失败，请重试");
    } finally {
      setBusy(null);
    }
  }

  if (!items.length) return null;

  return (
    <div className="mb-2 space-y-2">
      {pending.map((q) => (
        <div key={q.id} className="rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-sm">
          <div className="mb-1 flex items-center gap-2 text-amber-800">
            <span className="rounded bg-amber-200 px-1.5 py-0.5 text-[11px] font-medium">
              提问 · 等待回答
            </span>
            <span className="text-[11px] text-amber-600">组长已在本轮原地暂停，回答后接着做（计划与对话都不会中断）</span>
          </div>
          <div className="whitespace-pre-wrap [overflow-wrap:anywhere] text-slate-800">{q.question}</div>
          <div className="mt-2 space-y-1">
            {q.options.map((o, i) => (
              <label
                key={i}
                className={`flex cursor-pointer items-start gap-2 rounded border px-2 py-1.5 ${
                  picked[q.id] === i
                    ? "border-brand-500 bg-white ring-1 ring-brand-500"
                    : "border-slate-200 bg-white/70 hover:border-slate-300"
                }`}
              >
                <input
                  type="radio"
                  className="mt-1"
                  name={`q-${q.id}`}
                  checked={picked[q.id] === i}
                  onChange={() => setPicked((cur) => ({ ...cur, [q.id]: i }))}
                />
                <span className="leading-snug text-slate-800">
                  {o.label}
                  {o.detail ? (
                    <span className="ml-1 text-[11px] text-slate-500">（{o.detail}）</span>
                  ) : null}
                </span>
              </label>
            ))}
          </div>
          {q.allow_text && (
            <input
              value={texts[q.id] || ""}
              onChange={(e) => setTexts((cur) => ({ ...cur, [q.id]: e.target.value }))}
              placeholder="输入你的答案（也可以补充要求）"
              className="mt-2 w-full rounded border border-slate-300 bg-white px-2 py-1.5 text-sm outline-none focus:border-brand-500"
            />
          )}
          {err && <div className="mt-1 text-xs text-red-600">{err}</div>}
          <div className="mt-2 flex items-center gap-2">
            <button
              onClick={() => submit(q)}
              disabled={busy === q.id}
              className="ga-btn ga-btn-primary ga-btn-sm"
            >
              {busy === q.id ? "提交中…" : "提交并继续"}
            </button>
            <button
              onClick={() => submit(q, true)}
              disabled={busy === q.id}
              className="ga-btn ga-btn-secondary ga-btn-sm"
            >
              跳过（按你的判断做）
            </button>
          </div>
        </div>
      ))}

      {answered.length > 0 && (
        <div className="rounded-lg border border-slate-200 bg-slate-50 text-xs">
          <button
            onClick={() => setShowLog((v) => !v)}
            className="flex w-full items-center justify-between px-3 py-1.5 text-slate-600 hover:text-slate-800"
          >
            <span>
              提问 · {answered.length}/{items.length} 已回答
            </span>
            <span className="text-slate-400">{showLog ? "▲" : "▼"}</span>
          </button>
          {showLog && (
            <div className="space-y-1.5 border-t border-slate-200 px-3 py-2">
              {answered.map((q) => (
                <div key={q.id} className="leading-snug">
                  <div className="text-slate-700">{q.question}</div>
                  <div className="text-emerald-700">
                    → {q.answer_text || q.options?.[q.answer_index ?? -1]?.label || "（已跳过）"}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
