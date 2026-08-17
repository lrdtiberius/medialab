(() => {
  const processing = document.querySelector('.status-processing');
  if (processing) {
    window.setTimeout(() => window.location.reload(), 8000);
  }

  const mediaTypeSelect = document.querySelector('[data-media-type-select]');
  const librarySelect = document.querySelector('[data-media-library-select]');
  const updateLibraryChoices = () => {
    if (!mediaTypeSelect || !librarySelect) return;
    const mediaType = mediaTypeSelect.value;
    let firstAllowed = null;
    let selectedIsAllowed = false;
    for (const option of librarySelect.options) {
      const allowed = (option.dataset.allowedFor || '').split(/\s+/).includes(mediaType);
      option.disabled = !allowed;
      option.hidden = !allowed;
      if (allowed && firstAllowed === null) firstAllowed = option.value;
      if (allowed && option.selected) selectedIsAllowed = true;
    }
    if (!selectedIsAllowed && firstAllowed !== null) {
      librarySelect.value = firstAllowed;
    }
  };
  if (mediaTypeSelect && librarySelect) {
    mediaTypeSelect.addEventListener('change', updateLibraryChoices);
    updateLibraryChoices();
  }

  const scanPanel = document.querySelector('[data-technical-scan]');
  if (scanPanel) {
    let wasRunning = scanPanel.dataset.running === 'true';
    const setText = (selector, value) => {
      const element = scanPanel.querySelector(selector);
      if (element) element.textContent = value;
    };
    const pollTechnicalScan = async () => {
      try {
        const response = await fetch('/library/technical-status', {
          headers: { 'Accept': 'application/json' },
          cache: 'no-store',
        });
        if (!response.ok) return;
        const state = await response.json();
        const progress = scanPanel.querySelector('[data-scan-progress]');
        if (progress) progress.style.width = `${state.percent || 0}%`;
        setText('[data-scan-percent]', `${state.percent || 0} %`);
        setText('[data-scan-state]', state.running ? 'läuft' : 'bereit');
        setText('[data-scan-completed]', state.completed || 0);
        setText('[data-scan-total]', state.total || 0);
        setText('[data-scan-analyzed]', state.analyzed || 0);
        setText('[data-scan-cached]', state.cached || 0);
        setText('[data-scan-errors]', state.errors || 0);
        setText('[data-scan-current]', state.current_file || '');
        setText('[data-scan-message]', state.message || '');
        if (wasRunning && !state.running) {
          window.setTimeout(() => window.location.reload(), 700);
          return;
        }
        wasRunning = Boolean(state.running);
      } catch (_error) {
        // A temporary network error must not break the rest of the interface.
      }
    };
    pollTechnicalScan();
    window.setInterval(pollTechnicalScan, 2000);
  }

  const selectAll = document.querySelector('[data-select-all]');
  if (selectAll) {
    selectAll.addEventListener('change', () => {
      for (const checkbox of document.querySelectorAll('[data-error-checkbox]')) {
        checkbox.checked = selectAll.checked;
      }
    });
  }

  for (const form of document.querySelectorAll('form')) {
    form.addEventListener('submit', () => {
      const button = form.querySelector('button[type="submit"]');
      if (button) {
        button.disabled = true;
        button.dataset.originalText = button.textContent;
        button.textContent = 'Bitte warten …';
      }
    }, { once: true });
  }
})();
