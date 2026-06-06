import { useState } from "react";

import { Modal } from "@/components/ui/Modal";
import { useVersionDiff } from "@/api/topologies";
import type { ModifiedItem, TopologyVersion } from "@/types/topology.types";

interface VersionDiffDialogProps {
  open: boolean;
  topologyId: string;
  versionA: TopologyVersion | null;
  versionB: TopologyVersion | null;
  onClose: () => void;
}

function Section({
  title,
  count,
  children,
}: {
  title: string;
  count: number;
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(count > 0);
  return (
    <div className="border rounded mb-3">
      <button
        onClick={() => setOpen((x) => !x)}
        className="w-full flex items-center justify-between px-3 py-2 text-sm font-medium bg-gray-50 hover:bg-gray-100"
      >
        <span>
          {title} <span className="text-gray-500">({count})</span>
        </span>
        <span className="text-gray-400">{open ? "-" : "+"}</span>
      </button>
      {open && count > 0 && <div className="px-3 py-2 text-xs">{children}</div>}
    </div>
  );
}

function ItemRow({ label, payload }: { label: string; payload: unknown }) {
  const [expanded, setExpanded] = useState(false);
  return (
    <div className="py-1 border-b last:border-b-0">
      <button
        onClick={() => setExpanded((x) => !x)}
        className="text-left w-full text-gray-700 hover:text-gray-900"
      >
        {expanded ? "-" : "+"} {label}
      </button>
      {expanded && (
        <pre className="mt-1 p-2 bg-gray-50 rounded text-[10px] overflow-x-auto">
          {JSON.stringify(payload, null, 2)}
        </pre>
      )}
    </div>
  );
}

function ModifiedRow({ item }: { item: ModifiedItem }) {
  const [expanded, setExpanded] = useState(false);
  return (
    <div className="py-1 border-b last:border-b-0">
      <button
        onClick={() => setExpanded((x) => !x)}
        className="text-left w-full text-gray-700 hover:text-gray-900"
      >
        {expanded ? "-" : "+"} {item.id}
      </button>
      {expanded && (
        <div className="grid grid-cols-2 gap-2 mt-1">
          <div>
            <div className="text-[10px] uppercase text-gray-500">before</div>
            <pre className="p-2 bg-red-50 rounded text-[10px] overflow-x-auto">
              {JSON.stringify(item.before, null, 2)}
            </pre>
          </div>
          <div>
            <div className="text-[10px] uppercase text-gray-500">after</div>
            <pre className="p-2 bg-green-50 rounded text-[10px] overflow-x-auto">
              {JSON.stringify(item.after, null, 2)}
            </pre>
          </div>
        </div>
      )}
    </div>
  );
}

export function VersionDiffDialog({
  open,
  topologyId,
  versionA,
  versionB,
  onClose,
}: VersionDiffDialogProps) {
  const { data, isLoading, isError } = useVersionDiff(
    topologyId,
    versionA?.id,
    versionB?.id,
  );

  const title =
    versionA && versionB
      ? `Diff v${versionA.version_number} to v${versionB.version_number}`
      : "Version diff";

  return (
    <Modal open={open} onClose={onClose} title={title} className="max-w-3xl">
      {isLoading && <p className="text-sm text-gray-500">Computing diff...</p>}
      {isError && <p className="text-sm text-red-600">Failed to compute diff.</p>}
      {data && (
        <div className="text-sm">
          <Section title="Nodes added" count={data.nodes_added.length}>
            {data.nodes_added.map((n) => {
              const id = String((n as { id?: unknown }).id ?? "");
              return <ItemRow key={`na-${id}`} label={id} payload={n} />;
            })}
          </Section>
          <Section title="Nodes removed" count={data.nodes_removed.length}>
            {data.nodes_removed.map((n) => {
              const id = String((n as { id?: unknown }).id ?? "");
              return <ItemRow key={`nr-${id}`} label={id} payload={n} />;
            })}
          </Section>
          <Section title="Nodes modified" count={data.nodes_modified.length}>
            {data.nodes_modified.map((item) => (
              <ModifiedRow key={`nm-${item.id}`} item={item} />
            ))}
          </Section>
          <Section title="Edges added" count={data.edges_added.length}>
            {data.edges_added.map((e) => {
              const id = String((e as { id?: unknown }).id ?? "");
              return <ItemRow key={`ea-${id}`} label={id} payload={e} />;
            })}
          </Section>
          <Section title="Edges removed" count={data.edges_removed.length}>
            {data.edges_removed.map((e) => {
              const id = String((e as { id?: unknown }).id ?? "");
              return <ItemRow key={`er-${id}`} label={id} payload={e} />;
            })}
          </Section>
          <Section title="Edges modified" count={data.edges_modified.length}>
            {data.edges_modified.map((item) => (
              <ModifiedRow key={`em-${item.id}`} item={item} />
            ))}
          </Section>
        </div>
      )}
    </Modal>
  );
}
