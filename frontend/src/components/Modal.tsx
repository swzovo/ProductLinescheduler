import type { ReactNode } from "react";

export function Modal({
  title,
  children,
  onClose,
  width = "640px",
}: {
  title: string;
  children: ReactNode;
  onClose: () => void;
  width?: string;
}) {
  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={onClose}>
      <section
        className="modal"
        style={{ maxWidth: width }}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header className="modal-header">
          <div>
            <span className="eyebrow">操作面板</span>
            <h2>{title}</h2>
          </div>
          <button className="icon-button" onClick={onClose} aria-label="关闭">
            ×
          </button>
        </header>
        <div className="modal-content">{children}</div>
      </section>
    </div>
  );
}

