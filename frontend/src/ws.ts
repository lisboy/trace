import { api } from "./api";
import { useAuth } from "./store";
import type { WSEvent } from "./types";

/**
 * 项目级 WebSocket 事件通道（设计 §8.1 / §10.7）。
 *
 * - 连接后先由调用方按需 backfillRun 补拉历史事件（DB 权威源），
 * - 之后实时接收新事件；断线自动重连并把最新 seq 交给上层去重。
 *
 * 注意：seq 由后端按「项目」单调递增，run 维度通过 run_id 字段区分。
 */
export class ProjectSocket {
  private ws: WebSocket | null = null;
  private url: string;
  private handlers: ((e: WSEvent) => void)[] = [];
  private statusCb?: (ok: boolean) => void;
  private closed = false;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  wantedSeq = 0;

  constructor(private projectId: string) {
    const token = useAuth.getState().token;
    const teamId = useAuth.getState().currentTeamId;
    const proto = location.protocol === "https:" ? "wss" : "ws";
    this.url =
      `${proto}://${location.host}/ws/projects/${projectId}` +
      `?token=${encodeURIComponent(token || "")}` +
      `&team_id=${encodeURIComponent(teamId || "")}`;
  }

  on(cb: (e: WSEvent) => void) {
    this.handlers.push(cb);
  }
  onConnection(cb: (ok: boolean) => void) {
    this.statusCb = cb;
  }

  start() {
    this.connect();
  }

  private emit(e: WSEvent) {
    if (e.seq > this.wantedSeq) this.wantedSeq = e.seq;
    this.handlers.forEach((h) => h(e));
  }

  private connect() {
    if (this.closed) return;
    const ws = new WebSocket(this.url);
    this.ws = ws;
    ws.onopen = () => this.statusCb?.(true);
    ws.onmessage = (ev) => {
      try {
        const d = JSON.parse(ev.data);
        if (d.type === "connected") return;
        this.emit({
          seq: d.seq ?? 0,
          type: d.type,
          run_id: d.run_id ?? null,
          task_id: d.task_id ?? null,
          agent: d.agent ?? null,
          payload: d.payload ?? {},
        });
      } catch {
        /* ignore malformed */
      }
    };
    ws.onclose = () => {
      this.statusCb?.(false);
      if (!this.closed)
        this.reconnectTimer = setTimeout(() => this.connect(), 2000);
    };
    ws.onerror = () => ws.close();
  }

  /** 从 REST 补拉某 run 的历史事件（连接前后均可调用）。 */
  async backfillRun(runId: string) {
    try {
      const evs = await api.events(runId, 0);
      evs.forEach((e: any) => {
        this.emit({
          seq: e.seq ?? 0,
          type: e.type,
          run_id: runId,
          task_id: null,
          agent: e.payload?.agent_key ?? null,
          payload: e.payload ?? {},
        });
      });
    } catch {
      /* ignore */
    }
  }

  close() {
    this.closed = true;
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.ws?.close();
  }
}
