/** Client-generated id for canvas-local objects (nodes, edges, session lines). */
export function genId(): string {
  return self.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2, 11)}`;
}
