import { useRef, useEffect } from "react";

interface ModalProps {
  open: boolean;
  onClose: () => void;
  title: string;
  children: React.ReactNode;
}

export function Modal({ open, onClose, title, children }: ModalProps) {
  const dialogRef = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (open && !dialog.open) {
      dialog.showModal();
    } else if (!open && dialog.open) {
      dialog.close();
    }
  }, [open]);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    const handleCancel = (e: Event) => {
      e.preventDefault();
      onClose();
    };
    dialog.addEventListener("cancel", handleCancel);
    return () => dialog.removeEventListener("cancel", handleCancel);
  }, [onClose]);

  return (
    <dialog
      ref={dialogRef}
      className="rounded-lg shadow-xl border border-gray-200 p-0 backdrop:bg-black/40 max-w-lg w-full"
      aria-labelledby="modal-title"
    >
      <div className="flex items-center justify-between px-6 py-4 border-b border-gray-200">
        <h2 id="modal-title" className="text-lg font-semibold text-gray-900">
          {title}
        </h2>
        <button
          onClick={onClose}
          aria-label="Close dialog"
          className="text-gray-400 hover:text-gray-600 text-xl leading-none px-1"
        >
          &times;
        </button>
      </div>
      <div className="p-6">{children}</div>
    </dialog>
  );
}
