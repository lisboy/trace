import { memo } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkBreaks from "remark-breaks";

/** 模型输出 / 富文本统一 Markdown 渲染（GFM 表格 + 单换行即换行）。
 *  memo 是有实际开销的：聊天页每来一个 ws 事件、每 4 秒校准一次执行态都会整页重渲染，
 *  不挡住的话就是把会话里每条回复的 Markdown 全部重新解析一遍（长会话明显发卡）。 */
const PLUGINS = [remarkGfm, remarkBreaks];

/** 链接：一律新标签页打开（联网搜索的来源是外部页面，同标签打开会把整个应用顶掉），
 *  noreferrer noopener 防止新页面拿到 window.opener。 */
const COMPONENTS = {
  a: ({ node: _node, ...props }: any) => (
    <a
      {...props}
      target="_blank"
      rel="noreferrer noopener"
      className="text-brand-700 underline decoration-brand-300 underline-offset-2 hover:text-brand-800"
    />
  ),
};

export default memo(function Markdown({ text, className = "" }: { text: string; className?: string }) {
  return (
    <div className={`md-body ${className}`}>
      <ReactMarkdown remarkPlugins={PLUGINS} components={COMPONENTS}>
        {text}
      </ReactMarkdown>
    </div>
  );
});
