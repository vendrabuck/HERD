import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";

interface FloatingPanelProps {
  children: ReactNode;
  title: string;
  defaultPosition?: { x: number; y: number };
}

export function FloatingPanel({
  children,
  title,
  defaultPosition = { x: 16, y: 16 },
}: FloatingPanelProps) {
  const [position, setPosition] = useState(defaultPosition);
  const [collapsed, setCollapsed] = useState(false);
  const dragRef = useRef<{ startX: number; startY: number; startPosX: number; startPosY: number } | null>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  // Tracks any currently-installed document listeners so unmount mid-drag can detach them.
  const activeListenersRef = useRef<{
    move: (e: MouseEvent) => void;
    up: () => void;
  } | null>(null);

  const detachListeners = useCallback(() => {
    const active = activeListenersRef.current;
    if (!active) return;
    document.removeEventListener("mousemove", active.move);
    document.removeEventListener("mouseup", active.up);
    activeListenersRef.current = null;
  }, []);

  // Safety net: if the component unmounts while a drag is in progress (e.g., the
  // user navigates away mid-drag), detach any document listeners we installed.
  useEffect(() => {
    return () => {
      detachListeners();
    };
  }, [detachListeners]);

  const onMouseDown = useCallback(
    (e: React.MouseEvent) => {
      // Only drag from the title bar (left mouse button)
      if (e.button !== 0) return;
      e.preventDefault();
      dragRef.current = {
        startX: e.clientX,
        startY: e.clientY,
        startPosX: position.x,
        startPosY: position.y,
      };

      // Detach anything left over from an unusual prior interaction before installing new handlers.
      detachListeners();

      const onMouseMove = (ev: MouseEvent) => {
        if (!dragRef.current) return;
        const dx = ev.clientX - dragRef.current.startX;
        const dy = ev.clientY - dragRef.current.startY;
        setPosition({
          x: dragRef.current.startPosX + dx,
          y: dragRef.current.startPosY + dy,
        });
      };

      const onMouseUp = () => {
        dragRef.current = null;
        detachListeners();
      };

      activeListenersRef.current = { move: onMouseMove, up: onMouseUp };
      document.addEventListener("mousemove", onMouseMove);
      document.addEventListener("mouseup", onMouseUp);
    },
    [position, detachListeners],
  );

  return (
    <div
      ref={panelRef}
      className="absolute shadow-lg rounded-lg border border-gray-200 bg-white overflow-hidden"
      style={{
        left: position.x,
        top: position.y,
        zIndex: 10,
        pointerEvents: "auto",
      }}
    >
      {/* Title bar (drag handle) */}
      <div
        onMouseDown={onMouseDown}
        className="flex items-center justify-between px-3 py-1.5 bg-gray-100 border-b border-gray-200 cursor-move select-none"
      >
        <span className="text-xs font-semibold text-gray-700">{title}</span>
        <button
          onClick={() => setCollapsed((c) => !c)}
          className="text-xs text-gray-500 hover:text-gray-700 px-1"
          title={collapsed ? "Expand" : "Collapse"}
        >
          {collapsed ? "+" : "-"}
        </button>
      </div>

      {/* Content */}
      {!collapsed && children}
    </div>
  );
}
