import React from "react";

/** 渲染兜底：任何页面组件抛错时只替换内容区，并给出可见的报错/重试，而不是整页黑屏。
 *  背景：2026-10-29 会话切换时 RunTraceInline 触发了 React #310（Hook 数量不一致），
 *  没有边界时 React 会卸载整棵树，用户看到的就是一片黑。 */
export default class ErrorBoundary extends React.Component<
  { children: React.ReactNode; resetKey?: string },
  { error: Error | null; attempt: number }
> {
  state = { error: null as Error | null, attempt: 0 };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    // 留在控制台方便排查，但不再让页面整体崩掉
    console.error("[ui] 页面渲染出错：", error, info.componentStack);
  }

  componentDidUpdate(prev: { resetKey?: string }) {
    // 切页（hash 变了）自动清掉上一次的错误，避免错误界面卡住不动
    if (this.state.error && prev.resetKey !== this.props.resetKey) {
      this.setState({ error: null, attempt: this.state.attempt + 1 });
    }
  }

  private retry = () => {
    this.setState((s) => ({ error: null, attempt: s.attempt + 1 }));
  };

  render() {
    const { error, attempt } = this.state;
    if (error) {
      return (
        <div className="flex h-full flex-col items-center justify-center gap-3 px-6 text-slate-500">
          <div className="text-sm">这个页面渲染出错了（已拦截，不会再整页黑屏）</div>
          <pre className="max-h-40 max-w-[80%] overflow-auto whitespace-pre-wrap rounded bg-slate-100 px-3 py-2 text-[11px] leading-relaxed text-slate-500">
            {String(error?.message || error)}
          </pre>
          <div className="flex gap-2">
            <button
              type="button"
              onClick={this.retry}
              className="rounded bg-slate-800 px-3 py-1.5 text-xs text-white hover:bg-slate-700"
            >
              重试渲染
            </button>
            <button
              type="button"
              onClick={() => window.location.reload()}
              className="rounded border border-slate-300 px-3 py-1.5 text-xs text-slate-600 hover:bg-slate-50"
            >
              刷新页面
            </button>
            <button
              type="button"
              onClick={() => {
                window.location.hash = "#/projects";
              }}
              className="rounded border border-slate-300 px-3 py-1.5 text-xs text-slate-600 hover:bg-slate-50"
            >
              回到项目页
            </button>
          </div>
        </div>
      );
    }
    // key 变化 → 子树整体重挂载，把出错的组件状态清干净
    return <React.Fragment key={attempt}>{this.props.children}</React.Fragment>;
  }
}
