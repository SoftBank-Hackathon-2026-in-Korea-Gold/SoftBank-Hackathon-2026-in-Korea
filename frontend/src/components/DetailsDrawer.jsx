import React, { useEffect, useRef } from 'react';
import { X } from 'lucide-react';

const TABS = [['deploy', '새 배포'], ['details', '배포 상세'], ['projects', '프로젝트'], ['nodes', '노드'], ['settings', '설정']];
const FOCUSABLE = 'button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex="0"]';

export default function DetailsDrawer({ panel, onPanel, onClose, children }) {
  const drawer = useRef(null);
  const close = useRef(null);
  const onCloseRef = useRef(onClose);
  const open = !!panel;
  useEffect(() => { onCloseRef.current = onClose; });
  useEffect(() => {
    if (!open) return undefined;
    const previous = document.activeElement;
    close.current?.focus();
    const handleKey = (e) => {
      if (e.key === 'Escape') {
        e.preventDefault(); e.stopPropagation(); onCloseRef.current();
      } else if (e.key === 'Tab') {
        const items = [...drawer.current.querySelectorAll(FOCUSABLE)].filter((el) => el.getClientRects().length);
        const first = items[0]; const last = items.at(-1);
        if (e.shiftKey && (document.activeElement === first || !drawer.current.contains(document.activeElement))) { e.preventDefault(); last?.focus(); }
        else if (!e.shiftKey && (document.activeElement === last || !drawer.current.contains(document.activeElement))) { e.preventDefault(); first?.focus(); }
      }
    };
    document.addEventListener('keydown', handleKey, true);
    return () => {
      document.removeEventListener('keydown', handleKey, true);
      if (previous?.isConnected && !previous.closest('[inert]')) previous.focus();
    };
  }, [open]);

  // Keep sections mounted when closed: form drafts and pending input survive panel navigation.
  return (
    <div className="drawer-layer" hidden={!open}>
      <button className="drawer-backdrop" aria-label="패널 닫기" tabIndex={-1} onClick={onClose} />
      <aside ref={drawer} className="details-drawer" role="dialog" aria-modal="true" aria-labelledby="drawer-title">
        <header className="drawer-header"><h2 id="drawer-title">설정 및 상세 정보</h2><button ref={close} className="dashboard-button icon-button" aria-label="상세 패널 닫기" onClick={onClose}><X size={18} /></button></header>
        <nav className="drawer-tabs" aria-label="상세 패널 메뉴">{TABS.map(([id, label]) => <button key={id} aria-current={panel === id ? 'page' : undefined} onClick={() => onPanel(id)}>{label}</button>)}</nav>
        <div className="drawer-body">{children}</div>
      </aside>
    </div>
  );
}
