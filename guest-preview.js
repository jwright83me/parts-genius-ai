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
      <button class="primary" type="button" id="nova-guest-continue">Ask another question</button>
      <button type="button" id="nova-guest-replay" hidden>Play Nova’s answer</button>
      <p class="small muted">Continue with a 3-day free trial.</p>
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
  const replay = panel.querySelector('#nova-guest-replay');
  let answerAudio;
  let answerContext;
  let playingAnswer;
  const unlockAnswerAudio = () => {
    const AudioContext = window.AudioContext || window.webkitAudioContext;
    if (!AudioContext) return;
    try {
      answerContext ||= new AudioContext();
      answerContext.resume().catch(() => {});
    } catch {}
  };
  const heroTalk = home.querySelector('.nova-voice-button');
  const memberTalk = heroTalk.onclick;
  const welcomeSource = '/api/guest/welcome-audio';
  const welcomeAudio = new Audio(welcomeSource);
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
  let microphoneStarted = false;
  let needsMicTap = false;
  let turn = 0;
  const welcome = 'Hi, I’m Nova. I can show you a hologram of your car and help you figure out what to check first. Tell me the year, make and model, and what it’s doing.';
  const canRecord = Boolean(navigator.mediaDevices?.getUserMedia && window.MediaRecorder);
  let stopRecording;
  let cancelRecording = () => {};
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;

  const vehicleName = () => [preview.vehicle.year, preview.vehicle.make,
    preview.vehicle.model, preview.vehicle.engine].filter(Boolean).join(' ');

  const save = () => {
    try { sessionStorage.setItem(storageKey, JSON.stringify(preview)); } catch {}
  };

  const playClip = (source, done) => {
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
    try {
      if (welcomeAudio.getAttribute('src') !== source) welcomeAudio.src = source;
      welcomeAudio.currentTime = 0;
      welcomeAudio.play().catch(() => finish(false));
    }
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
    if (preview.completed && hasVehicle) prompt.textContent = `Your ${vehicleName()} is in My Garage for this visit.`;
    label.textContent = hasVehicle ? 'What’s going on with your vehicle?' : 'Year, make, model and engine (if known)';
    input.placeholder = hasVehicle ? 'Example: It clicks rapidly and won’t start' : 'Example: 2020 Dodge Challenger 6.4L';
    input.maxLength = hasVehicle ? 500 : 120;
    submit.textContent = hasVehicle ? 'Ask Nova' : 'Show my vehicle';
    form.hidden = prompt.hidden = !typing || Boolean(preview.completed);
    typeButton.hidden = typing || Boolean(preview.completed);
    offer.hidden = !preview.completed;
    replay.hidden = !preview.answer || (!answerAudio && !window.speechSynthesis);
    reply.textContent = preview.answer || (preview.completed ? 'You’ve already used your free question in this browser.' : '');
  };

  const markComplete = () => {
    preview.completed = true;
    save();
    render();
    buttonState('ready', 'Ask another question');
    status.textContent = '';
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

  const offerMicrophone = (message = 'Tap the microphone to speak.') => {
    if (!canRecord && !Recognition) {
      buttonState('idle');
      status.textContent = 'This browser can’t listen directly. Tap Type instead and use your keyboard microphone.';
      return;
    }
    needsMicTap = true;
    buttonState('ready', microphoneStarted ? '🎙 Tap to speak' : '🎙 Enable microphone');
    status.textContent = message;
  };

  const record = async (currentTurn) => {
    let stream;
    let recorder;
    let timer;
    const isCurrent = () => currentTurn === turn;
    const cleanup = () => {
      clearTimeout(timer);
      stream?.getTracks().forEach(track => track.stop());
      stopRecording = undefined;
      cancelRecording = () => {};
    };
    cancelRecording = () => {
      if (recorder?.state === 'recording') recorder.stop();
      cleanup();
    };
    buttonState('connecting', 'Opening microphone…');
    status.textContent = 'Allow microphone access if your browser asks.';
    timer = setTimeout(() => {
      if (!isCurrent()) return;
      ++turn;
      cleanup();
      offerMicrophone('Microphone access didn’t open. Tap to try again, or type instead.');
    }, 20000);
    try {
      stream = await navigator.mediaDevices.getUserMedia({audio: true});
      if (!isCurrent()) {
        stream.getTracks().forEach(track => track.stop());
        return;
      }
      clearTimeout(timer);
      const formats = ['audio/mp4', 'audio/webm', 'audio/ogg'];
      const mimeType = formats.find(type => MediaRecorder.isTypeSupported?.(type));
      recorder = new MediaRecorder(stream, mimeType ? {mimeType, audioBitsPerSecond: 48000} : undefined);
      const chunks = [];
      recorder.ondataavailable = ({data}) => { if (data.size) chunks.push(data); };
      recorder.onerror = () => {
        if (!isCurrent()) return;
        ++turn;
        cancelRecording();
        offerMicrophone('The recording stopped. Tap the microphone to try again.');
      };
      recorder.onstop = async () => {
        if (!isCurrent()) return;
        cleanup();
        buttonState('connecting', 'Nova is processing…');
        status.textContent = 'Nova is transcribing your voice message.';
        try {
          const audio = new Blob(chunks, {type: recorder.mimeType || chunks[0]?.type});
          if (!audio.size) throw new Error('Nova didn’t hear anything. Tap to try again.');
          const response = await fetch('/api/guest/transcribe', {method: 'POST', body: audio});
          if (!isCurrent()) return;
          const result = await response.json();
          if (!isCurrent()) return;
          if (response.status === 409) { markComplete(); return; }
          if (!response.ok || !result.text) throw new Error(result.error || 'Nova didn’t hear anything. Tap to try again.');
          buttonState('idle');
          await accept(result.text);
        } catch (error) {
          if (isCurrent()) offerMicrophone(error.message || 'Try the microphone again, or type instead.');
        }
      };
      recorder.start();
      microphoneStarted = true;
      needsMicTap = false;
      stopRecording = () => {
        clearTimeout(timer);
        stopRecording = undefined;
        buttonState('connecting', 'Nova is processing…');
        if (recorder.state === 'recording') recorder.stop();
      };
      buttonState('listening', '🎙 Done speaking');
      status.textContent = 'Recording for transcription. Speak now, then tap Done speaking.';
      timer = setTimeout(() => stopRecording?.(), 15000);
    } catch (error) {
      if (!isCurrent()) return;
      cleanup();
      offerMicrophone(error.name === 'NotAllowedError'
        ? 'Microphone access is blocked. Allow access in your browser’s site settings or choose Type instead.'
        : 'The microphone couldn’t open. If you opened Nova inside another app, try opening the site in Safari.');
    }
  };

  const listen = (currentTurn) => {
    if (!guest || home.hidden || typing || preview.completed || currentTurn !== turn) return;
    if (!Recognition) {
      offerMicrophone();
      return;
    }
    const mic = new Recognition();
    recognition = mic;
    mic.lang = 'en-US';
    const isCurrent = () => currentTurn === turn && recognition === mic;
    const retry = (message) => {
      if (!isCurrent()) return;
      clearTimeout(timer);
      recognition = undefined;
      offerMicrophone(message);
    };
    const timer = setTimeout(() => {
      if (!isCurrent()) return;
      retry('The microphone didn’t start. Tap to try again or choose Type instead.');
      mic.abort();
    }, 15000);
    mic.onstart = () => {
      if (!isCurrent()) return;
      clearTimeout(timer);
      microphoneStarted = true;
      needsMicTap = false;
      buttonState('listening', 'Nova is listening…');
      status.textContent = 'Listening…';
    };
    mic.onresult = (event) => {
      if (!isCurrent()) return;
      clearTimeout(timer);
      recognition = undefined;
      microphoneStarted = true;
      needsMicTap = false;
      buttonState('idle');
      accept(event.results[0][0].transcript);
    };
    mic.onerror = (event) => {
      retry(event.error === 'not-allowed' || event.error === 'service-not-allowed'
        ? 'Allow microphone access when your browser asks. If access is blocked, check this site’s permissions or tap Type instead.'
        : 'Nova couldn’t hear you. Tap the microphone to try again, or type instead.');
    };
    mic.onend = () => retry('Tap the microphone when you’re ready to speak.');
    try {
      buttonState('connecting', 'Opening microphone…');
      status.textContent = 'Allow microphone access if your browser asks.';
      // Keep start in the button’s click handler for Safari’s user gesture requirement.
      mic.start();
    } catch {
      retry('The microphone didn’t start. Tap to try again or choose Type instead.');
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
      if (!played && requirePlayback) {
        status.textContent = 'Tap Talk to Nova to hear her welcome.';
        return;
      }
      if (microphoneStarted && !canRecord) listen(currentTurn);
      else offerMicrophone(microphoneStarted
        ? 'Tap the microphone to record your reply.'
        : 'Tap Enable microphone and allow access to speak to Nova.');
    };
    if (text === welcome) {
      playClip(welcomeSource, played => {
        if (currentTurn !== turn) return;
        if (played || requirePlayback) finish(played);
        else speak(text, finish);
      });
    } else speak(text, finish);
  };

  const open = () => {
    if (!guest) return false;
    window.trackNovaFunnel?.('preview_started');
    show('home');
    if (stopRecording) { unlockAnswerAudio(); stopRecording(); return true; }
    if (preview.completed) showSignup();
    else if (!busy) {
      typing = false;
      voice = true;
      render();
      if (recognition) return true;
      if (needsMicTap) {
        unlockAnswerAudio();
        if (canRecord) void record(++turn);
        else listen(++turn);
      }
      else promptAndListen(preview.vehicle ? 'What’s going on with your vehicle?' : welcome);
    }
    return true;
  };

  typeButton.onclick = () => {
    ++turn;
    cancelRecording();
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

    window.trackNovaFunnel?.('preview_started');
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
      if (result.answer) window.trackNovaFunnel?.('first_answer');
      preview.answer = result.answer;
      if (result.audio) answerAudio = result.audio;
      markComplete();
      if (result.answer && guest && !home.hidden) playAnswer();
    } catch (error) {
      if (voice) buttonState('idle');
      status.textContent = (error.message || 'Nova is temporarily unavailable.') + ' Tap Talk to Nova or type to try again.';
    } finally {
      busy = submit.disabled = input.disabled = false;
    }
  };

  form.onsubmit = (event) => {
    event.preventDefault();
    unlockAnswerAudio();
    accept(input.value);
  };

  const playAnswer = () => {
    const currentTurn = ++turn;
    cancelWelcome();
    window.speechSynthesis?.cancel();
    buttonState('connecting', 'Nova is loading…');
    replay.disabled = true;
    const finish = (played) => {
      if (currentTurn !== turn) return;
      buttonState('ready', 'Ask another question');
      replay.disabled = false;
      status.textContent = played ? '' : 'Tap Play Nova’s answer to try the audio again.';
    };
    const fallback = () => {
      if (currentTurn !== turn) return;
      if (answerAudio) {
        playClip('data:audio/mpeg;base64,' + answerAudio, played => {
          if (currentTurn !== turn) return;
          if (played) finish(true);
          else speak(preview.answer, finish);
        });
      } else speak(preview.answer, finish);
    };
    if (answerAudio && answerContext?.state === 'running') {
      const bytes = Uint8Array.from(atob(answerAudio), char => char.charCodeAt(0));
      answerContext.decodeAudioData(bytes.buffer).then(buffer => {
        if (currentTurn !== turn) return;
        if (answerContext.state !== 'running') { fallback(); return; }
        const player = answerContext.createBufferSource();
        player.buffer = buffer;
        player.connect(answerContext.destination);
        player.onended = () => {
          if (playingAnswer === player) playingAnswer = undefined;
          finish(true);
        };
        player.start();
        playingAnswer = player;
        buttonState('speaking', 'Nova is speaking…');
      }).catch(fallback);
    } else fallback();
  };

  replay.onclick = () => { unlockAnswerAudio(); playAnswer(); };
  panel.querySelector('#nova-guest-continue').onclick = showSignup;
  window.openNovaGuestPreview = open;
  window.addEventListener('pagehide', () => { ++turn; cancelRecording(); });
  window.pauseNovaGuestPreview = () => {
    if (!guest) return;
    ++turn;
    cancelRecording();
    recognition?.abort();
    recognition = undefined;
    cancelWelcome();
    window.speechSynthesis?.cancel();
    playingAnswer?.stop();
    playingAnswer = undefined;
    replay.disabled = false;
    if (preview.completed) buttonState('ready', 'Ask another question');
    else buttonState('idle');
  };
  window.addEventListener('nova-membership-state', ({detail}) => {
    guest = window.novaGuestPreviewEnabled = !detail.signedIn;
    panel.hidden = !guest || home.hidden;
    heroTalk.onclick = guest ? open : memberTalk;
    if (guest) {
      if (detail.guestPreviewComplete && !preview.completed) markComplete();
      render();
      showVehicle();
      if (preview.completed && !heroTalk.disabled) buttonState('ready', 'Ask another question');
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
