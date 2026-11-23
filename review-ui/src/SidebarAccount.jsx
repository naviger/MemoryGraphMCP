import { ChevronUp, LogOut, ShieldCheck, UserRound, X } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';

const AVATAR_SIZE = 256;
const INITIAL_CROP = { zoom: 1, horizontal: 50, vertical: 50 };
const IMAGE_TYPES = ['image/jpeg', 'image/png', 'image/webp'];

function existingAvatar(key) {
  try {
    const value = localStorage.getItem(key);
    return value?.startsWith('data:image/jpeg;base64,') ? value : null;
  } catch {
    return null;
  }
}

function Avatar({ name, src, large = false }) {
  const initials = name.trim().split(/\s+/).filter(Boolean).slice(0, 2)
    .map((part) => Array.from(part)[0]).join('').toUpperCase();
  return <span className={large ? 'account-avatar account-avatar-large' : 'account-avatar'} aria-hidden="true">
    {src ? <img src={src} alt="" /> : initials || <UserRound size={18} />}
  </span>;
}

function cropArea(image, crop) {
  const width = Math.max(AVATAR_SIZE, AVATAR_SIZE * image.naturalWidth / image.naturalHeight) * crop.zoom;
  const height = Math.max(AVATAR_SIZE, AVATAR_SIZE * image.naturalHeight / image.naturalWidth) * crop.zoom;
  return { width, height };
}

function AccountDialog({ name, session, avatar, saveAvatar, onClose }) {
  const dialog = useRef(null);
  const canvas = useRef(null);
  const selection = useRef(0);
  const drag = useRef(null);
  const [image, setImage] = useState(null);
  const [crop, setCrop] = useState(INITIAL_CROP);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const access = session.role === 'admin' ? 'Admin' : session.can_write ? 'Writer' : 'Reader';

  useEffect(() => {
    const element = dialog.current;
    element.showModal();
    return () => {
      selection.current++;
      element.close();
    };
  }, []);

  useEffect(() => {
    if (!image || !canvas.current) return;
    const context = canvas.current.getContext('2d');
    const { width, height } = cropArea(image, crop);
    context.fillStyle = '#ffffff';
    context.fillRect(0, 0, AVATAR_SIZE, AVATAR_SIZE);
    context.drawImage(image, -(width - AVATAR_SIZE) * crop.horizontal / 100,
      -(height - AVATAR_SIZE) * crop.vertical / 100, width, height);
  }, [image, crop]);

  async function chooseImage(event) {
    const file = event.target.files[0];
    event.target.value = '';
    if (!file) return;
    const request = ++selection.current;
    setError('');
    setLoading(false);
    if (!IMAGE_TYPES.includes(file.type) || file.size > 10 * 1024 * 1024) {
      setError('Choose a JPEG, PNG, or WebP image up to 10 MB.');
      return;
    }
    const url = URL.createObjectURL(file);
    setLoading(true);
    try {
      const candidate = new Image();
      candidate.src = url;
      await candidate.decode();
      if (request !== selection.current) return;
      if (!candidate.naturalWidth || !candidate.naturalHeight
        || candidate.naturalWidth * candidate.naturalHeight > 32_000_000) {
        setError('Choose an image with at most 32 megapixels.');
        return;
      }
      setCrop(INITIAL_CROP);
      setImage(candidate);
    } catch {
      if (request === selection.current) setError('This image could not be opened.');
    } finally {
      URL.revokeObjectURL(url);
      if (request === selection.current) setLoading(false);
    }
  }

  function startDrag(event) {
    if (event.button !== 0 || drag.current) return;
    const ratio = AVATAR_SIZE / event.currentTarget.getBoundingClientRect().width;
    drag.current = { x: event.clientX, y: event.clientY, crop, ratio, id: event.pointerId };
    event.currentTarget.setPointerCapture(event.pointerId);
  }

  function moveCrop(event) {
    const start = drag.current;
    if (!start || start.id !== event.pointerId) return;
    const { width, height } = cropArea(image, start.crop);
    const move = (current, delta, size) => size <= AVATAR_SIZE ? current
      : Math.min(100, Math.max(0, current - delta * start.ratio * 100 / (size - AVATAR_SIZE)));
    setCrop({ ...start.crop, horizontal: move(start.crop.horizontal, event.clientX - start.x, width),
      vertical: move(start.crop.vertical, event.clientY - start.y, height) });
  }

  function close() {
    dialog.current.close();
    onClose();
  }

  function save(value) {
    try {
      saveAvatar(value);
      close();
    } catch {
      setError('The avatar could not be saved in this browser.');
    }
  }

  return <dialog className="account-dialog" ref={dialog} aria-labelledby="account-title"
    onCancel={(event) => { event.preventDefault(); close(); }}>
    <header className="account-dialog-heading">
      <h2 id="account-title">Account details</h2>
      <button type="button" className="button icon outlined" aria-label="Close account details" onClick={close}>
        <X size={18} aria-hidden="true" />
      </button>
    </header>
    <dl className="account-details">
      <div><dt>Name</dt><dd>{name}</dd></div>
      <div><dt>Access</dt><dd><ShieldCheck size={16} aria-hidden="true" /> {access}</dd></div>
      <div><dt>Account ID</dt><dd>{session.subject}</dd></div>
    </dl>
    <section className="avatar-settings">
      <h3>Avatar</h3>
      {image ? <div className="avatar-editor">
        <canvas className="avatar-crop" ref={canvas} width={AVATAR_SIZE} height={AVATAR_SIZE}
          aria-label="Avatar crop preview" role="img" onPointerDown={startDrag} onPointerMove={moveCrop}
          onLostPointerCapture={() => { drag.current = null; }} />
        <div className="avatar-controls">
          <label>Zoom <input type="range" min="1" max="3" step="0.02" value={crop.zoom}
            onChange={(event) => setCrop({ ...crop, zoom: Number(event.target.value) })} /></label>
          <label>Horizontal position <input type="range" min="0" max="100" step="1" value={crop.horizontal}
            onChange={(event) => setCrop({ ...crop, horizontal: Number(event.target.value) })} /></label>
          <label>Vertical position <input type="range" min="0" max="100" step="1" value={crop.vertical}
            onChange={(event) => setCrop({ ...crop, vertical: Number(event.target.value) })} /></label>
        </div>
      </div> : <Avatar name={name} src={avatar} large />}
      <label className="avatar-upload">Upload an image
        <input type="file" accept={IMAGE_TYPES.join(',')} onChange={chooseImage} />
      </label>
      <p className="muted">JPEG, PNG, or WebP up to 10 MB. Your avatar is saved in this browser.</p>
      {loading && <p role="status">Opening image…</p>}
      {error && <p role="alert" className="status error">{error}</p>}
    </section>
    <footer className="account-actions">
      {avatar && !image && <button type="button" className="button outlined" disabled={loading}
        onClick={() => save(null)}>Remove avatar</button>}
      <button type="button" className="button outlined" onClick={close}>{image ? 'Cancel' : 'Done'}</button>
      {image && <button type="button" className="button primary" disabled={loading}
        onClick={() => save(canvas.current.toDataURL('image/jpeg', 0.88))}>Save avatar</button>}
    </footer>
  </dialog>;
}

export function SidebarAccount({ session, error, onSignOut }) {
  const name = session.display_name || 'Signed-in user';
  const key = `memorygraph.avatar.${session.subject}`;
  const [avatar, setAvatar] = useState(() => existingAvatar(key));
  const [expanded, setExpanded] = useState(false);
  const [details, setDetails] = useState(false);
  const [signingOut, setSigningOut] = useState(false);
  const region = useRef(null);
  const trigger = useRef(null);

  useEffect(() => {
    if (!expanded) return;
    function dismiss(event) {
      if (!region.current?.contains(event.target)) setExpanded(false);
    }
    function escape(event) {
      if (event.key === 'Escape') {
        setExpanded(false);
        trigger.current?.focus();
      }
    }
    document.addEventListener('pointerdown', dismiss);
    document.addEventListener('keydown', escape);
    return () => {
      document.removeEventListener('pointerdown', dismiss);
      document.removeEventListener('keydown', escape);
    };
  }, [expanded]);

  function saveAvatar(value) {
    if (value) localStorage.setItem(key, value);
    else localStorage.removeItem(key);
    setAvatar(value);
  }

  async function signOut() {
    setExpanded(false);
    setSigningOut(true);
    try {
      await onSignOut();
    } finally {
      setSigningOut(false);
      trigger.current?.focus();
    }
  }

  return <div className="sidebar-account" ref={region}>
    <button type="button" className="account-trigger" ref={trigger} aria-label={`Account menu for ${name}`}
      aria-expanded={expanded} disabled={signingOut} onClick={() => setExpanded(!expanded)}>
      <Avatar name={name} src={avatar} />
      <span className="account-name" title={name}>{name}</span>
      <ChevronUp size={16} aria-hidden="true" />
    </button>
    {expanded && <div className="account-flyout">
      <button type="button" className="nav-item" onClick={() => { setExpanded(false); setDetails(true); }}>
        <UserRound size={16} aria-hidden="true" /> Account details
      </button>
      <button type="button" className="nav-item" onClick={signOut}>
        <LogOut size={16} aria-hidden="true" /> {session.browser_session ? 'Sign out' : 'Disconnect'}
      </button>
    </div>}
    {error && <p className="account-error" role="alert">{error}</p>}
    {details && <AccountDialog name={name} session={session} avatar={avatar} saveAvatar={saveAvatar}
      onClose={() => { setDetails(false); trigger.current?.focus(); }} />}
  </div>;
}
