/** 行级 diff 工具：把「改之前 / 改之后」两段文本变成可渲染的行序列。
 *
 * 用途：
 *  - 工具调用步骤里直接给出编辑前后的差异（edit_file 的 old_string/new_string）
 *  - 后端 /runs/{id}/changes 返回的 diff 用同一套类型渲染
 * 纯前端计算，不依赖 git：工作区里有没有 .git 都能看到改动内容。
 */

export type DiffRow = {
  kind: "ctx" | "add" | "del";
  old_no: number | null;
  new_no: number | null;
  text: string;
};

const split = (s: string): string[] =>
  s.length ? s.replace(/\r\n/g, "\n").replace(/\r/g, "\n").split("\n") : [];

/** 简易 LCS：先裁掉公共前后缀，中间段再做 DP（限制规模，避免长文件卡死）。 */
export function lineDiff(before: string, after: string): DiffRow[] {
  const a = split(before);
  const b = split(after);
  let head = 0;
  while (head < a.length && head < b.length && a[head] === b[head]) head++;
  let tail = 0;
  while (tail < a.length - head && tail < b.length - head &&
         a[a.length - 1 - tail] === b[b.length - 1 - tail]) tail++;
  const am = a.slice(head, a.length - tail);
  const bm = b.slice(head, b.length - tail);

  const rows: DiffRow[] = [];
  for (let k = 0; k < head; k++) {
    rows.push({ kind: "ctx", old_no: k + 1, new_no: k + 1, text: a[k] });
  }
  const LIMIT = 600;
  if (am.length && bm.length && am.length <= LIMIT && bm.length <= LIMIT) {
    const n = am.length, m = bm.length;
    const dp: Uint32Array[] = Array.from({ length: n + 1 }, () => new Uint32Array(m + 1));
    for (let i = n - 1; i >= 0; i--) {
      for (let j = m - 1; j >= 0; j--) {
        dp[i][j] = am[i] === bm[j]
          ? dp[i + 1][j + 1] + 1
          : Math.max(dp[i + 1][j], dp[i][j + 1]);
      }
    }
    let i = 0, j = 0;
    while (i < n && j < m) {
      if (am[i] === bm[j]) {
        rows.push({ kind: "ctx", old_no: head + i + 1, new_no: head + j + 1, text: am[i] });
        i++; j++;
      } else if (dp[i + 1][j] >= dp[i][j + 1]) {
        rows.push({ kind: "del", old_no: head + i + 1, new_no: null, text: am[i] });
        i++;
      } else {
        rows.push({ kind: "add", old_no: null, new_no: head + j + 1, text: bm[j] });
        j++;
      }
    }
    while (i < n) { rows.push({ kind: "del", old_no: head + i + 1, new_no: null, text: am[i] }); i++; }
    while (j < m) { rows.push({ kind: "add", old_no: null, new_no: head + j + 1, text: bm[j] }); j++; }
  } else {
    am.forEach((t, k) => rows.push({ kind: "del", old_no: head + k + 1, new_no: null, text: t }));
    bm.forEach((t, k) => rows.push({ kind: "add", old_no: null, new_no: head + k + 1, text: t }));
  }
  for (let k = 0; k < tail; k++) {
    const ai = a.length - tail + k;
    const bi = b.length - tail + k;
    rows.push({ kind: "ctx", old_no: ai + 1, new_no: bi + 1, text: a[ai] });
  }
  return rows;
}

export function diffStats(rows: DiffRow[]): { added: number; deleted: number } {
  let added = 0, deleted = 0;
  for (const r of rows) {
    if (r.kind === "add") added++;
    else if (r.kind === "del") deleted++;
  }
  return { added, deleted };
}

/** 从工具参数里取出「前后文本」，供步骤级 diff 预览使用。 */
export function diffFromToolArgs(
  tool: string,
  args?: Record<string, unknown>,
): { before: string; after: string; path: string; label: string } | null {
  if (!args) return null;
  const path = String(args.path ?? "");
  if (tool === "edit_file") {
    const old = args.old_string, neu = args.new_string;
    if (typeof old !== "string" || typeof neu !== "string") return null;
    return { before: old, after: neu, path, label: "替换前 / 替换后" };
  }
  if (tool === "write_file") {
    const content = args.content;
    if (typeof content !== "string") return null;
    return { before: "", after: content, path, label: "写入内容" };
  }
  if (tool === "delete_file") {
    return { before: String(args.path ?? ""), after: "", path, label: "删除文件" };
  }
  return null;
}
