(() => {
  const panel = document.createElement('section');
  panel.id = 'nova-guest-preview';
  panel.hidden = true;
  panel.setAttribute('aria-labelledby', 'nova-guest-title');
  panel.innerHTML = `
    <h2 id="nova-guest-title" class="nova-guest-sr-only">Talk to Nova</h2>
    <p id="nova-guest-prompt" hidden></p>
    <button type="button" id="nova-guest-type">Type instead</button>
    <form id="nova-guest-form" hidden>
      <label class="field"><span id="nova-guest-label"></span>
        <input id="nova-guest-input" type="text" required autocomplete="off">
      </label>
      <button class="primary" type="submit"></button>

    </form>
    <p id="nova-guest-status" role="status"></p>
    <p id="nova-guest-answer" aria-live="polite"></p>
    <div id="nova-guest-offer" hidden>
      <button class="primary" type="button">Start my 3-day free trial</button>
      <p class="small muted">$9.99/month after trial. Card required. Cancel before the trial ends to avoid being charged.</p>
    </div>`;

  const home = document.getElementById('novaHome');
  home.after(panel);
  const form = panel.querySelector('form');
  const input = panel.querySelector('input');
  const submit = form.querySelector('[type=submit]');
  const typeButton = panel.querySelector('#nova-guest-type');
  const label = panel.querySelector('#nova-guest-label');
  const prompt = panel.querySelector('#nova-guest-prompt');
  const status = panel.querySelector('#nova-guest-status');
  const reply = panel.querySelector('#nova-guest-answer');
  const offer = panel.querySelector('#nova-guest-offer');
  const heroTalk = home.querySelector('.nova-voice-button');
  const memberTalk = heroTalk.onclick;
  const welcomeAudio = new Audio('/api/guest/welcome-audio');
  welcomeAudio.preload = 'none';
  let cancelWelcome = () => {};
  const buttonState = (state, label = '') => {
    heroTalk.dataset.voiceState = state;
    heroTalk.textContent = label;
    heroTalk.setAttribute('aria-label', label || 'Talk to Nova');
    heroTalk.disabled = state === 'connecting' || state === 'speaking';
  };
  const storageKey = 'novaGuestPreview';
  let preview = {};
  try { preview = JSON.parse(sessionStorage.getItem(storageKey)) || {}; } catch {}
  let guest = false;
  let voice = false;
  let typing = false;
  let busy = false;
  let recognition;
  let triedWelcome = false;
  let needsMicTap = false;
  let turn = 0;
  const welcome = 'Hi, I’m Nova. I can show you a hologram of your car and help you figure out what to check first. Tell me the year, make and model, and what it’s doing.';
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;

  const vehicleName = () => [preview.vehicle.year, preview.vehicle.make,
    preview.vehicle.model, preview.vehicle.engine].filter(Boolean).join(' ');

  const save = () => {
    try { sessionStorage.setItem(storageKey, JSON.stringify(preview)); } catch {}
  };

  const playWelcome = (done) => {
    let finished = false;
    let timer = setTimeout(() => finish(false), 12000);
    const finish = (played) => {
      if (finished) return;
      finished = true;
      clearTimeout(timer);
      cancelWelcome = () => {};
      welcomeAudio.onplaying = welcomeAudio.onended = welcomeAudio.onerror = null;
      if (!played) welcomeAudio.pause();
      done(played);
    };
    welcomeAudio.onplaying = () => {
      clearTimeout(timer);
      timer = setTimeout(() => finish(false), 30000);
      buttonState('speaking', 'Nova is speaking…');
    };
    welcomeAudio.onended = () => finish(true);
    welcomeAudio.onerror = () => finish(false);
    cancelWelcome = () => finish(false);
    try { welcomeAudio.currentTime = 0; welcomeAudio.play().catch(() => finish(false)); }
    catch { finish(false); }
  };

  const speak = (text, done) => {
    if (!window.speechSynthesis || !window.SpeechSynthesisUtterance) {
      done(false);
      return;
    }
    const words = new SpeechSynthesisUtterance(text);
    words.lang = 'en-AU';
    let started = false;
    let timer;
    let finished = false;
    const finish = (played) => {
      if (finished) return;
      finished = true;
      clearTimeout(timer);
      done(played);
    };
    words.onstart = () => {
      started = true;
      buttonState('speaking', 'Nova is speaking…');
      clearTimeout(timer);
      timer = setTimeout(() => finish(true), 45000);
    };
    words.onend = () => finish(started);
    words.onerror = () => finish(false);
    window.speechSynthesis.cancel();
    timer = setTimeout(() => finish(false), 2200);
    try { window.speechSynthesis.speak(words); }
    catch { finish(false); }
  };

  const render = () => {
    const hasVehicle = Boolean(preview.vehicle);
    prompt.textContent = hasVehicle
      ? `Your ${vehicleName()} is in My Garage for this visit. What’s going on with it?`
      : 'Tell me what you drive. I’ll show you your vehicle and help you figure out what to check first.';
    if (preview.completed) prompt.textContent = `Your ${vehicleName()} is in My Garage for this visit.`;
    label.textContent = hasVehicle ? 'What’s going on with your vehicle?' : 'Year, make, model and engine (if known)';
    input.placeholder = hasVehicle ? 'Example: It clicks rapidly and won’t start' : 'Example: 2020 Dodge Challenger 6.4L';
    input.maxLength = hasVehicle ? 500 : 120;
    submit.textContent = hasVehicle ? 'Ask Nova' : 'Show my vehicle';
    form.hidden = prompt.hidden = !typing || Boolean(preview.completed);
    typeButton.hidden = typing || Boolean(preview.completed);
    offer.hidden = !preview.completed;
    reply.textContent = preview.answer || '';
  };

  const showVehicle = () => {
    if (!preview.vehicle) return;
    window.novaGuestVehicle = preview.vehicle;
    showMyGarage();
    const holo = document.getElementById('novaVehicleHologram');
    const image = getVehicleHologram(preview.vehicle);
    if (image) holo.src = '/' + image;
    else holo.removeAttribute('src');
    holo.style.display = image ? 'block' : 'none';
    holo.alt = image?.includes('generic-') ? 'Illustrative vehicle hologram' : 'Vehicle hologram';
    status.textContent = !image ? 'This model doesn’t have a hologram yet.' : '';
  };

  const showSignup = () => {
    if (guest && !home.hidden && !document.querySelector('dialog[open]')) {
      window.openNovaMembership?.();
    }
  };

  const listen = (currentTurn) => {
    if (!guest || home.hidden || typing || preview.completed || currentTurn !== turn) return;
    if (!Recognition) {
      buttonState('idle');
      status.textContent = 'Voice is unavailable here. Tap Type instead to continue.';
      return;
    }
    const mic = new Recognition();
    recognition = mic;
    mic.lang = 'en-US';
    mic.onresult = (event) => {
      if (currentTurn !== turn) return;
      recognition = undefined;
      needsMicTap = false;
      accept(event.results[0][0].transcript);
    };
    mic.onerror = () => {
      if (currentTurn !== turn) return;
      needsMicTap = true;
      buttonState('idle');
      status.textContent = 'Tap Talk to Nova to try the microphone again, or type instead.';
    };
    mic.onend = () => {
      if (recognition !== mic) return;
      recognition = undefined;
      if (heroTalk.dataset.voiceState === 'listening') buttonState('idle');
      if (status.textContent === 'Listening…') status.textContent = 'Tap Talk to Nova when you’re ready to speak.';
    };
    try {
      mic.start();
      buttonState('listening', 'Nova is listening…');
      status.textContent = 'Listening…';
    } catch {
      recognition = undefined;
      needsMicTap = true;
      buttonState('idle');
      status.textContent = 'Tap Talk to Nova to enable the microphone, or type instead.';
    }
  };

  const promptAndListen = (text, requirePlayback = false) => {
    const currentTurn = ++turn;
    cancelWelcome();
    recognition?.abort();
    recognition = undefined;
    if (!requirePlayback) buttonState('connecting', 'Nova is loading…');
    const finish = (played) => {
      if (currentTurn !== turn) return;
      buttonState('idle');
      if (played || !requirePlayback) listen(currentTurn);
      else status.textContent = 'Tap Talk to Nova to hear her welcome.';
    };
    if (text === welcome) {
      playWelcome(played => {
        if (currentTurn !== turn) return;
        if (played || requirePlayback) finish(played);
        else speak(text, finish);
      });
    } else speak(text, finish);
  };

  const open = () => {
    if (!guest) return false;
    show('home');
    if (preview.completed) showSignup();
    else if (!busy) {
      typing = false;
      voice = true;
      render();
      if (needsMicTap) {
        recognition?.abort();
        listen(++turn);
      } else promptAndListen(preview.vehicle ? 'What’s going on with your vehicle?' : welcome);
    }
    return true;
  };

  typeButton.onclick = () => {
    ++turn;
    recognition?.abort();
    recognition = undefined;
    cancelWelcome();
    window.speechSynthesis?.cancel();
    buttonState('idle');
    voice = false;
    typing = true;
    status.textContent = '';
    render();
    input.focus();
  };

  const accept = async (message) => {
    if (busy || preview.completed) return;
    let symptom = message.trim().slice(0, 500);
    if (!preview.vehicle) {
      // Leave model names intact and separate common spoken symptom transitions.
      const match = symptom.match(/\s+(?:and\s+)?(?:my (?:problem|issue) is(?: that)?|the (?:problem|issue) is(?: that)?|it(?=\s|['’])|and I(?=\s))\b|[.;?!]\s+(?=[A-Za-z])/i);
      const description = match ? symptom.slice(0, match.index) : symptom;
      const vehicle = window.parseNovaVehicle(description);
      if (!vehicle || !vehicle.model || Number(vehicle.year) > new Date().getFullYear() + 1) {
        status.textContent = 'Tell me the year, make and model. For example, 2020 Dodge Challenger.';
        if (voice) promptAndListen(status.textContent);
        return;
      }
      preview.vehicle = vehicle;
      save();
      showVehicle();
      input.value = '';
      render();
      symptom = match ? symptom.slice(match.index).replace(/^[.;?!]\s+/, '').trim() : '';
      if (symptom.length < 4) {
        if (voice) promptAndListen(`I’ve added your ${vehicleName()} to My Garage for this visit. What’s going on with it?`);
        else input.focus();
        return;
      }
    }
    if (symptom.length < 4) {
      status.textContent = 'Tell me what the vehicle is doing.';
      if (voice) promptAndListen(status.textContent);
      return;
    }

    busy = submit.disabled = input.disabled = true;
    status.textContent = 'Nova is thinking…';
    if (voice) buttonState('connecting', 'Nova is thinking…');
    try {
      const response = await fetch('/api/guest/diagnose', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({vehicle: vehicleName(), symptom})
      });
      const result = await response.json();
      if (!response.ok && !result.completed) throw new Error(result.error || 'Please try again.');
      preview.completed = true;
      preview.answer = result.answer || result.error;
      save();
      status.textContent = '';
      render();
      const currentTurn = ++turn;
      const invite = (played) => {
        if (currentTurn !== turn) return;
        if (voice) buttonState('idle');
        if (played) showSignup();
        else setTimeout(() => { if (currentTurn === turn) showSignup(); }, 4500);
      };
      if (voice && guest && !home.hidden) speak(preview.answer, invite);
      else invite(false);
    } catch (error) {
      if (voice) buttonState('idle');
      status.textContent = (error.message || 'Nova is temporarily unavailable.') + ' Tap Talk to Nova or type to try again.';
    } finally {
      busy = submit.disabled = input.disabled = false;
    }
  };

  form.onsubmit = (event) => {
    event.preventDefault();
    accept(input.value);
  };

  offer.querySelector('button').onclick = () => window.openNovaMembership();
  window.openNovaGuestPreview = open;
  window.pauseNovaGuestPreview = () => {
    if (!guest) return;
    ++turn;
    recognition?.abort();
    recognition = undefined;
    cancelWelcome();
    window.speechSynthesis?.cancel();
    buttonState('idle');
  };
  window.addEventListener('nova-membership-state', ({detail}) => {
    guest = window.novaGuestPreviewEnabled = !detail.signedIn;
    panel.hidden = !guest || home.hidden;
    heroTalk.onclick = guest ? open : memberTalk;
    if (guest) {
      render();
      showVehicle();
      if (!triedWelcome && !preview.completed && !home.hidden &&
          !new URLSearchParams(location.search).has('membership')) {
        triedWelcome = true;
        voice = true;
        promptAndListen(preview.vehicle ? 'What’s going on with your vehicle?' : welcome, true);
      }
    } else {
      if (window.novaGuestVehicle) {
        delete window.novaGuestVehicle;
        showMyGarage();
        const hologram = document.getElementById('novaVehicleHologram');
        const image = getVehicleHologram(JSON.parse(localStorage.getItem('pgVehicle') || 'null'));
        if (image) hologram.src = '/' + image;
        else hologram.removeAttribute('src');
        hologram.style.display = image ? 'block' : 'none';
      }
      if (detail.active && preview.vehicle && !localStorage.getItem('pgVehicle')) {
        window.activateNovaVehicle(preview.vehicle);
      }
    }
  });
})();
