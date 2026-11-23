import { CircleHelp } from 'lucide-react';
import { createContext, useContext, useEffect, useId, useLayoutEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { helpHref, helpTopics } from './helpTopics.js';

export const HelpNavigation = createContext(null);

export function ContextHelp({ topic, beforeNavigate }) {
  const navigation = useContext(HelpNavigation);
  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState(null);
  const button = useRef(null);
  const panel = useRef(null);
  const id = useId();
  const info = helpTopics[topic];
  const href = helpHref(topic);
  const scopedHref = navigation?.projectKey ? `${href}?project=${encodeURIComponent(navigation.projectKey)}` : href;

  useEffect(() => setOpen(false), [topic, navigation?.page]);

  useLayoutEffect(() => {
    if (!open) return;
    function place() {
      const anchor = button.current?.getBoundingClientRect();
      if (!anchor || !panel.current) return;
      const { width, height } = panel.current.getBoundingClientRect();
      if (!anchor.width || anchor.bottom < 0 || anchor.top > window.innerHeight) {
        setOpen(false);
        return;
      }
      const left = Math.max(8, Math.min(anchor.left, window.innerWidth - width - 8));
      const top = anchor.bottom + height + 16 <= window.innerHeight
        ? anchor.bottom + 8 : Math.max(8, anchor.top - height - 8);
      setPosition({ left, top });
    }
    function dismiss(event) {
      if (!button.current?.contains(event.target) && !panel.current?.contains(event.target)) setOpen(false);
    }
    function keydown(event) {
      if (event.key === 'Escape') {
        event.preventDefault();
        event.stopPropagation();
        setOpen(false);
        button.current?.focus();
      }
    }
    place();
    window.addEventListener('resize', place);
    window.addEventListener('scroll', place, true);
    document.addEventListener('pointerdown', dismiss);
    document.addEventListener('keydown', keydown, true);
    return () => {
      window.removeEventListener('resize', place);
      window.removeEventListener('scroll', place, true);
      document.removeEventListener('pointerdown', dismiss);
      document.removeEventListener('keydown', keydown, true);
    };
  }, [open, topic]);

  const positioned = Boolean(position);
  useEffect(() => {
    if (open && positioned) panel.current?.querySelector('a')?.focus({ preventScroll: true });
  }, [open, positioned]);

  if (!info) return null;
  return <>
    <button type="button" ref={button} className="context-help-button" aria-label={`Help for ${info.title}`}
      aria-expanded={open} aria-controls={open ? id : undefined} onClick={() => {
        if (!open) setPosition(null);
        setOpen(!open);
      }}>
      <CircleHelp size={17} aria-hidden="true" />
    </button>
    {open && createPortal(
      <div ref={panel} id={id} className="context-help-popover" role="dialog" aria-label={`Help for ${info.title}`}
        style={position ?? { left: 8, top: 8, visibility: 'hidden' }}>
        <strong>{info.title}</strong>
        <p>{info.brief}</p>
        <a href={scopedHref} onClick={(event) => {
          if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) return;
          setOpen(false);
          beforeNavigate?.();
          navigation?.navigate(event, href);
        }}>More <span className="sr-only">about {info.title}</span></a>
      </div>,
      button.current?.closest('dialog') || document.body,
    )}
  </>;
}
