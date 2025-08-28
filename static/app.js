(() => {
  const $ = (sel) => document.querySelector(sel);

  const statusEl = $('#status');
  const msgsEl = $('#messages');
  const roomForm = $('#roomForm');
  const roomInput = $('#roomInput');
  const chatForm = $('#chatForm');
  const msgInput = $('#messageInput');
  const sendBtn = $('#sendBtn');

  const userListEl = $('#userList');
  const refreshUsersBtn = $('#refreshUsers');

  

  // Add a collapse/expand toggle for users panel (mobile-friendly)
  const usersPanel = document.querySelector('.users');
  const usersHead = document.querySelector('.users-head');
  const toggleBtn = document.createElement('button');
  toggleBtn.className = 'users-toggle';
  toggleBtn.type = 'button';
  toggleBtn.textContent = 'کاربران ⌄';
  usersHead.insertBefore(toggleBtn, usersHead.firstChild);

  function updateToggleLabel() {
    toggleBtn.textContent = usersPanel.classList.contains('collapsed') ? 'کاربران ⌃' : 'کاربران ⌄';
  }
  toggleBtn.addEventListener('click', () => {
    usersPanel.classList.toggle('collapsed');
    updateToggleLabel();
  });
  updateToggleLabel();

const incomingEl = $('#incoming');
  const callerNameEl = $('#callerName');
  const acceptBtn = $('#acceptBtn');
  const declineBtn = $('#declineBtn');

  const callBar = $('#callBar');
  const callWith = $('#callWith');
  const endCallBtn = $('#endCallBtn');
  const outputSelect = $('#outputSelect');
  const localAudio = $('#localAudio');
  const remoteAudio = $('#remoteAudio');

  let ws = null;

  // Background keep-alive
  let wakeLock = null;
  async function enableWakeLock() {
    if (!('wakeLock' in navigator)) return;
    try {
      wakeLock = await navigator.wakeLock.request('screen');
      wakeLock.addEventListener('release', () => console.log('WakeLock released'));
      console.log('WakeLock acquired');
    } catch (e) { console.warn('WakeLock error', e); }
  }
  function releaseWakeLock() { try { wakeLock && wakeLock.release(); } catch {} }

  let pingTimer = null;
  function startPing() {
    stopPing();
    pingTimer = setInterval(() => {
      if (ws && ws.readyState === WebSocket.OPEN) {
        try { ws.send(JSON.stringify({ type: 'ping', ts: Date.now() })); } catch {}
      }
    }, 25000);
  }
  function stopPing() { if (pingTimer) clearInterval(pingTimer); pingTimer = null; }

  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') { enableWakeLock(); }
  });
  window.addEventListener('beforeunload', () => { releaseWakeLock(); });

  let me = { id: null, username: null, room: null };

  // WebRTC state
  let peer = null;
  let localStream = null;
  let activePeerId = null; // طرف مقابل
  let pendingOffer = null; // برای UI تماس ورودی

  // جلوگیری از دوبار ارسال پیام در برخی دستگاه‌ها
  let sending = false;

  function pushMessage({ id, user, text, ts }) {
    const li = document.createElement('li');
    li.className = user && user.name === me.username ? 'msg mine' : 'msg';
    const time = new Date(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    li.innerHTML = `
      <div class="meta">
        <span class="user">${user?.name || 'سیستم'}</span>
        <span class="time">${time}</span>
      </div>
      <div class="text"></div>
    `;
    li.querySelector('.text').textContent = text;
    msgsEl.appendChild(li);
    msgsEl.scrollTop = msgsEl.scrollHeight;
  }

  function pushSystem(text) {
    pushMessage({ id: crypto.randomUUID(), user: null, text, ts: Date.now() });
  }

  function renderUserList(users) {
    userListEl.innerHTML = '';
    users.forEach((u) => {
      const li = document.createElement('li');
      li.className = 'user-item';
      li.dataset.id = u.id;
      li.innerHTML = `
        <span class="user-name">${u.name}</span>
        <button class="call-btn" title="تماس">📞</button>
      `;
      const btn = li.querySelector('.call-btn');
      if (u.id === me.id) {
        btn.disabled = true; btn.textContent = 'خودم';
      } else {
        btn.onclick = () => startCall(u.id, u.name);
      }
      userListEl.appendChild(li);
    });
  }

  async function joinRoom(roomName) {
    const res = await fetch(`/api/guest-token?room=${encodeURIComponent(roomName)}`);
    const data = await res.json();
    me.username = data.username;
    me.room = data.room;
    me.id = data.uid;

    const protocol = location.protocol === 'https:' ? 'wss' : 'ws';
    const wsUrl = `${protocol}://${location.host}/ws?token=${encodeURIComponent(data.token)}`;

    statusEl.textContent = `در حال اتصال به روم «${me.room}»…`;
    ws = new WebSocket(wsUrl);

    ws.addEventListener('open', () => {
      startPing(); enableWakeLock();
      statusEl.textContent = `وصل شد. نام شما: ${me.username} | روم: ${me.room}`;
      msgInput.disabled = false;
      sendBtn.disabled = false;
      msgInput.focus();
    });

    ws.addEventListener('message', (ev) => {
      let payload;
      try { payload = JSON.parse(ev.data); } catch { return; }

      if (payload.type === 'welcome') {
        if (payload.uid) me.id = payload.uid;
        pushSystem(`به روم «${payload.room}» خوش آمدید.`);
      } else if (payload.type === 'history') {
        (payload.messages || []).forEach((m) => pushMessage(m));
      } else if (payload.type === 'presence') {
        if (payload.subtype === 'join') pushSystem(`👋 ${payload.user.name} وارد شد.`);
        if (payload.subtype === 'leave') pushSystem(`👋 ${payload.user.name} خارج شد.`);
      } else if (payload.type === 'user_list') {
        renderUserList(payload.users || []);
      } else if (payload.type === 'chat') {
        pushMessage(payload.message);
      }

      // --- سیگنالینگ تماس ---
      else if (payload.type === 'call-offer') {
        onIncomingOffer(payload);
      } else if (payload.type === 'call-answer') {
        if (peer) {
          peer.setRemoteDescription(new RTCSessionDescription(payload.data)).catch(() => {});
        }
      } else if (payload.type === 'ice-candidate') {
        if (peer) {
          const c = payload.data;
          if (c && c.candidate) peer.addIceCandidate(new RTCIceCandidate(c)).catch(() => {});
        }
      } else if (payload.type === 'call-decline') {
        pushSystem(`${payload.from?.name || 'طرف مقابل'} تماس را رد کرد.`);
        endCallLocal(false);
      } else if (payload.type === 'call-end') {
        pushSystem(`تماس توسط ${payload.from?.name || 'طرف مقابل'} پایان یافت.`);
        endCallLocal(false);
      }
    });

    ws.addEventListener('close', () => {
      stopPing(); releaseWakeLock();
      statusEl.textContent = 'اتصال بسته شد.';
      msgInput.disabled = true;
      sendBtn.disabled = true;
      endCallLocal(false);
    });

    ws.addEventListener('error', () => {
      statusEl.textContent = 'خطای اتصال.';
    });
  }

  roomForm.addEventListener('submit', (e) => {
    e.preventDefault();
    const roomName = (roomInput.value || 'lobby').trim();
    if (!roomName) return;
    if (ws && ws.readyState === WebSocket.OPEN) ws.close(1000, 'switching room');
    msgsEl.innerHTML = '';
    userListEl.innerHTML = '';
    joinRoom(roomName);
  });

  chatForm.addEventListener('submit', (e) => {
    e.preventDefault();
    if (sending) return;
    const text = msgInput.value.trim();
    if (!text || !ws || ws.readyState !== WebSocket.OPEN) return;
    sending = true;
    ws.send(JSON.stringify({ type: 'chat', text }));
    msgInput.value = '';
    msgInput.focus();
    setTimeout(() => { sending = false; }, 150);
  });

  refreshUsersBtn.addEventListener('click', () => {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'get-users' }));
  });

  // ---------------- WebRTC helpers ----------------
  function ensurePeer() {
    const cfg = { iceServers: [{ urls: 'stun:stun.l.google.com:19302' }] };
    const pc = new RTCPeerConnection(cfg);

    pc.onicecandidate = (e) => {
    // Ensure audio transceiver exists for send/recv
    try { pc.addTransceiver('audio', { direction: 'sendrecv' }); } catch {}

      if (e.candidate && activePeerId) {
        ws?.send(JSON.stringify({ type: 'ice-candidate', target: activePeerId, data: e.candidate }));
      }
    };

    pc.ontrack = (e) => {
      remoteAudio.srcObject = e.streams[0];
      try { remoteAudio.play(); } catch (err) { console.warn('autoplay blocked?', err); }
    };

    return pc;
  }

  async function getLocalStream() {
    // If cached stream exists but tracks are ended, re-acquire
    if (localStream && localStream.getAudioTracks().some(t => t.readyState === 'live')) {
      return localStream;
    }
    try {
      localStream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true
        }
      });
      localAudio.srcObject = localStream;
      return localStream;
    } catch (err) {
      console.error('getUserMedia error:', err);
      throw err;
    }
  }

  function showCallBar(name) {
    callWith.textContent = `در تماس با: ${name}`;
    callBar.classList.remove('hidden');
    populateOutputs();
  }

  function hideCallBar() { callBar.classList.add('hidden'); }

  async function populateOutputs() {
    outputSelect.innerHTML = '';
    try {
      const devices = await navigator.mediaDevices.enumerateDevices();
      const outs = devices.filter(d => d.kind === 'audiooutput');
      if (!('setSinkId' in HTMLMediaElement.prototype) || outs.length === 0) {
        const opt = document.createElement('option');
        opt.textContent = 'خروجی: پیش‌فرض';
        outputSelect.appendChild(opt);
        outputSelect.disabled = true;
        return;
      }
      outs.forEach((d, i) => {
        const opt = document.createElement('option');
        opt.value = d.deviceId;
        opt.textContent = d.label || `خروجی ${i+1}`;
        outputSelect.appendChild(opt);
      });
      outputSelect.disabled = false;
    } catch (e) {
      const opt = document.createElement('option');
      opt.textContent = 'خروجی: پیش‌فرض';
      outputSelect.appendChild(opt);
      outputSelect.disabled = true;
    }
  }

  outputSelect.addEventListener('change', async () => {
    const id = outputSelect.value;
    if (!id || !('setSinkId' in HTMLMediaElement.prototype)) return;
    try { await remoteAudio.setSinkId(id); } catch {}
  });

  async function startCall(targetId, targetName) {
    if (peer) endCallLocal(false);
    activePeerId = targetId;
    peer = ensurePeer();

    const stream = await getLocalStream();
    stream.getTracks().forEach(t => peer.addTrack(t, stream));

    showCallBar(targetName || '—');
    enableWakeLock();

    try {
      const offer = await peer.createOffer({ offerToReceiveAudio: true });
      await peer.setLocalDescription(offer);
      ws?.send(JSON.stringify({ type: 'call-offer', target: targetId, data: offer }));
    } catch (e) {
      pushSystem('خطا در شروع تماس');
      endCallLocal(false);
    }
  }

  function onIncomingOffer(payload) {
    pendingOffer = payload;
    callerNameEl.textContent = payload.from?.name || '—';
    incomingEl.classList.remove('hidden');
    try { navigator.vibrate?.(200); } catch {}
  }

  acceptBtn.addEventListener('click', async () => {
    const payload = pendingOffer; pendingOffer = null;
    incomingEl.classList.add('hidden');
    if (!payload) return;

    activePeerId = payload.from?.id;
    peer = ensurePeer();

    const stream = await getLocalStream();
    stream.getTracks().forEach(t => peer.addTrack(t, stream));

    showCallBar(payload.from?.name || '—');
    enableWakeLock();

    try {
      await peer.setRemoteDescription(new RTCSessionDescription(payload.data));
      const answer = await peer.createAnswer();
      await peer.setLocalDescription(answer);
      ws?.send(JSON.stringify({ type: 'call-answer', target: activePeerId, data: answer }));
    } catch (e) {
      pushSystem('خطا در پاسخ به تماس');
      endCallLocal(false);
    }
  });

  declineBtn.addEventListener('click', () => {
    const payload = pendingOffer; pendingOffer = null;
    incomingEl.classList.add('hidden');
    if (payload?.from?.id) {
      ws?.send(JSON.stringify({ type: 'call-decline', target: payload.from.id }));
    }
  });

  endCallBtn.addEventListener('click', () => {
    if (activePeerId) ws?.send(JSON.stringify({ type: 'call-end', target: activePeerId }));
    endCallLocal(true);
  });

  function endCallLocal(showMsg) {
    if (peer) {
      try { peer.getSenders().forEach(s => { try { s.track?.stop(); } catch {} }); } catch {}
      try { peer.close(); } catch {}
    }
    peer = null;
    activePeerId = null;
    hideCallBar();
    if (showMsg) pushSystem('تماس پایان یافت.');
  }

  // شروع اتوماتیک با ?room=...
  const params = new URLSearchParams(location.search);
  const initRoom = params.get('room');
  if (initRoom) {
    roomInput.value = initRoom;
    roomForm.requestSubmit();
  }
})();