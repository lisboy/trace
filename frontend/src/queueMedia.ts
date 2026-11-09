/** 排队消息的图片持久化。
 *
 * localStorage 只有 ~5MB，dataURL 截图动辄几百 KB，写进去会挤爆其他会话的队列，
 * 所以文字/附件仍存 localStorage（见 Chat.tsx 的 saveQueue），图片改存 IndexedDB。
 * 刷新页面后排队项还能拿回完整图片，不再提示「这条消息的图片在刷新后失效」。
 */
const DB_NAME = "ga_queue_media";
const STORE = "images";

function openDb(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, 1);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains(STORE)) db.createObjectStore(STORE);
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

/** 以会话为键整表覆盖：已发送/删除的排队项会一起被清掉，不会在库里越积越多。 */
export async function saveQueueImages(
  sessionId: string,
  items: { id: string; images: string[] }[],
): Promise<void> {
  try {
    const db = await openDb();
    await new Promise<void>((resolve, reject) => {
      const tx = db.transaction(STORE, "readwrite");
      tx.objectStore(STORE).put(
        items.map((x) => ({ id: x.id, images: x.images.filter(Boolean) })),
        sessionId,
      );
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error);
    });
    db.close();
  } catch {
    /* IndexedDB 不可用（隐私模式等）就退回旧行为：本次会话内仍可正常发送 */
  }
}

export async function loadQueueImages(sessionId: string): Promise<Record<string, string[]>> {
  try {
    const db = await openDb();
    const rows = await new Promise<any[]>((resolve, reject) => {
      const tx = db.transaction(STORE, "readonly");
      const req = tx.objectStore(STORE).get(sessionId);
      req.onsuccess = () => resolve(Array.isArray(req.result) ? req.result : []);
      req.onerror = () => reject(req.error);
    });
    db.close();
    const out: Record<string, string[]> = {};
    for (const row of rows) {
      if (row?.id) out[String(row.id)] = Array.isArray(row.images) ? row.images.filter(Boolean) : [];
    }
    return out;
  } catch {
    return {};
  }
}
