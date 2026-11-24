(() => {
  const panel = document.createElement('section');
  panel.id = 'nova-guest-preview';
  panel.className = 'panel';
  panel.hidden = true;
  panel.setAttribute('aria-labelledby', 'nova-guest-title');
  panel.innerHTML = `
    <h2 id="nova-guest-title">Hi, I’m Nova.</h2>
    <p id="nova-guest-prompt"></p>
    <form id="nova-guest-form">
      <label class="field"><span id="nova-guest-label"></span>
        <input id="nova-guest-input" type="text" required autocomplete="off">
      </label>
      <button class="primary" type="submit"></button>
      <button type="button" id="nova-guest-talk">Talk to Nova</button>
    </form>
    <p id="nova-guest-status" role="status"></p>
    <img id="nova-guest-hologram" alt="" hidden>
    <p id="nova-guest-answer" aria-live="polite"></p>
    <div id="nova-guest-offer" hidden>
      <button class="primary" type="button">Start my 3-day free trial</button>
      <p class="small muted">$9.99/month after trial. Card required. Cancel before the trial ends to avoid being charged.</p>
    </div>`;

  const home = document.getElementById('novaHome');
  home.before(panel);
  const previewImage = panel.querySelector('img');
  const form = panel.querySelector('form');
  const input = panel.querySelector('input');
  const submit = form.querySelector('[type=submit]');
  const talk = panel.querySelector('#nova-guest-talk');
  const label = panel.querySelector('#nova-guest-label');
  const prompt = panel.querySelector('#nova-guest-prompt');
  const status = panel.querySelector('#nova-guest-status');
  const reply = panel.querySelector('#nova-guest-answer');
  const offer = panel.querySelector('#nova-guest-offer');
  const heroTalk = home.querySelector('.nova-voice-button');
  const memberTalk = heroTalk.onclick;
  const storageKey = 'novaGuestPreview';
  let preview = {};
  try { preview = JSON.parse(sessionStorage.getItem(storageKey)) || {}; } catch {}
  let guest = false;
  let voice = false;

  const vehicleName = () => [preview.vehicle.year, preview.vehicle.make,
    preview.vehicle.model, preview.vehicle.engine].filter(Boolean).join(' ');

  const save = () => {
    try { sessionStorage.setItem(storageKey, JSON.stringify(preview)); } catch {}
  };

  const speak = (text, finished) => {
    if (!window.speechSynthesis || !window.SpeechSynthesisUtterance) {
      finished?.();
      return;
    }
    const words = new SpeechSynthesisUtterance(text);
    words.lang = 'en-AU';
    words.onend = words.onerror = () => finished?.();
    window.speechSynthesis.cancel();
    window.speechSynthesis.speak(words);
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
    form.hidden = Boolean(preview.completed);
    offer.hidden = !preview.completed;
    reply.textContent = preview.answer || '';
  };

  const showVehicle = () => {
    if (!preview.vehicle) return;
    window.novaGuestVehicle = preview.vehicle;
    showMyGarage();
    const holo = document.getElementById('novaVehicleHologram');
    const image = getVehicleHologram(preview.vehicle);
    previewImage.hidden = !image;
    previewImage.alt = image?.includes('generic-') ? 'Illustrative vehicle hologram' : vehicleName() + ' hologram';
    if (image) previewImage.src = holo.src = '/' + image;
    else holo.removeAttribute('src');
    holo.style.display = image ? 'block' : 'none';
    holo.alt = image?.includes('generic-') ? 'Illustrative vehicle hologram' : 'Vehicle hologram';
    status.textContent = !image ? 'This model doesn’t have a hologram yet.' : '';
  };

  const open = () => {
    if (!guest) return false;
    show('home');
    panel.scrollIntoView({block: 'center'});
    (preview.completed ? offer.querySelector('button') : input).focus({preventScroll: true});
    return true;
  };

  const listen = () => {
    const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!Recognition) {
      talk.disabled = false;
      status.textContent = 'Use your keyboard microphone or type below.';
      input.focus();
      return;
    }
    const recognition = new Recognition();
    recognition.lang = 'en-US';
    recognition.onresult = (event) => {
      input.value = event.results[0][0].transcript.slice(0, input.maxLength);
      status.textContent = 'Check what I heard, then submit.';
      input.focus();
    };
    recognition.onerror = () => { status.textContent = 'Microphone unavailable. You can still type.'; };
    recognition.onend = () => {
      talk.disabled = false;
      if (status.textContent === 'Listening…') status.textContent = 'Tap Talk to Nova to try again, or type below.';
    };
    try {
      recognition.start();
      status.textContent = 'Listening…';
    } catch {
      talk.disabled = false;
      status.textContent = 'Microphone unavailable. You can still type.';
    }
  };

  talk.onclick = () => {
    voice = true;
    talk.disabled = true;
    speak(label.textContent, listen);
  };

  form.onsubmit = async (event) => {
    event.preventDefault();
    if (submit.disabled || preview.completed) return;
    const value = input.value.trim();
    if (!preview.vehicle) {
      const vehicle = window.parseNovaVehicle(value);
      if (!vehicle || !vehicle.model || Number(vehicle.year) > new Date().getFullYear() + 1) {
        status.textContent = 'Enter your year, make and model, like 2020 Dodge Challenger.';
        return;
      }
      preview.vehicle = vehicle;
      save();
      showVehicle();
      input.value = '';
      render();
      input.focus();
      if (voice) speak(prompt.textContent);
      return;
    }

    submit.disabled = input.disabled = talk.disabled = true;
    status.textContent = 'Nova is thinking…';
    try {
      const response = await fetch('/api/guest/diagnose', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({vehicle: vehicleName(), symptom: value})
      });
      const result = await response.json();
      if (!response.ok && !result.completed) throw new Error(result.error || 'Please try again.');
      preview.completed = true;
      preview.answer = result.answer || result.error;
      save();
      status.textContent = '';
      render();
      if (voice) speak(preview.answer);
    } catch (error) {
      status.textContent = error.message || 'Nova is temporarily unavailable.';
    } finally {
      submit.disabled = input.disabled = talk.disabled = false;
    }
  };

  offer.querySelector('button').onclick = () => window.openNovaMembership();
  window.openNovaGuestPreview = open;
  window.addEventListener('nova-membership-state', ({detail}) => {
    guest = window.novaGuestPreviewEnabled = !detail.signedIn;
    panel.hidden = !guest || home.hidden;
    heroTalk.onclick = guest ? () => {
      open();
      if (!preview.completed) talk.click();
    } : memberTalk;
    if (guest) {
      render();
      showVehicle();
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
