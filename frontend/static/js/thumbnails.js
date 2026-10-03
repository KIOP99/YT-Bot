/* ─────────────────────────────────────────────────────────────────────
   thumbnails.js — Live preview, upload, drag-and-drop reordering
   ───────────────────────────────────────────────────────────────────── */

document.addEventListener('DOMContentLoaded', () => {
  const dropZone = document.getElementById('thumb-drop-zone');
  const fileInput = document.getElementById('thumb-file-input');
  const previewBox = document.getElementById('thumb-preview-box');
  const previewImg = document.getElementById('thumb-preview-img');
  const previewFilename = document.getElementById('preview-filename');
  const previewDimensions = document.getElementById('preview-dimensions');
  const clearPreviewBtn = document.getElementById('clear-preview-btn');
  const thumbForm = document.getElementById('thumb-upload-form');
  const submitBtn = document.getElementById('thumb-submit-btn');
  const grid = document.getElementById('thumb-grid');

  // ── File Selection & Live Preview ───────────────────────────────────────
  if (dropZone && fileInput) {
    dropZone.addEventListener('click', () => fileInput.click());

    dropZone.addEventListener('dragover', e => {
      e.preventDefault();
      dropZone.style.borderColor = 'var(--accent)';
      dropZone.style.background = 'rgba(99, 102, 241, 0.08)';
    });

    dropZone.addEventListener('dragleave', () => {
      dropZone.style.borderColor = '';
      dropZone.style.background = '';
    });

    dropZone.addEventListener('drop', e => {
      e.preventDefault();
      dropZone.style.borderColor = '';
      dropZone.style.background = '';
      if (e.dataTransfer.files && e.dataTransfer.files[0]) {
        fileInput.files = e.dataTransfer.files;
        handleFilePreview(e.dataTransfer.files[0]);
      }
    });

    fileInput.addEventListener('change', () => {
      if (fileInput.files && fileInput.files[0]) {
        handleFilePreview(fileInput.files[0]);
      }
    });
  }

  function handleFilePreview(file) {
    if (!file || !file.type.startsWith('image/')) {
      YTBot.showToast('Please select a valid JPEG or PNG image', 'error');
      return;
    }

    const reader = new FileReader();
    reader.onload = e => {
      previewImg.src = e.target.result;
      previewBox.style.display = 'block';
      previewFilename.textContent = file.name;

      // Measure dimensions
      const img = new Image();
      img.onload = () => {
        previewDimensions.textContent = `${img.naturalWidth}×${img.naturalHeight}px`;
        if (img.naturalWidth < 1280 || img.naturalHeight < 720) {
          YTBot.showToast('Note: Recommended resolution is at least 1280×720', 'warn');
        }
      };
      img.src = e.target.result;
    };
    reader.readAsDataURL(file);
  }

  clearPreviewBtn?.addEventListener('click', () => {
    fileInput.value = '';
    previewBox.style.display = 'none';
    previewImg.src = '';
  });

  // ── Upload Thumbnail ────────────────────────────────────────────────────
  thumbForm?.addEventListener('submit', async e => {
    e.preventDefault();
    const channelSelect = document.getElementById('channel-select');
    const channelId = channelSelect?.value;

    if (!channelId) {
      YTBot.showToast('Please select a channel first', 'warn');
      return;
    }

    if (!fileInput.files || fileInput.files.length === 0) {
      YTBot.showToast('Please select an image file to upload', 'warn');
      return;
    }

    const formData = new FormData(thumbForm);
    submitBtn.disabled = true;
    submitBtn.textContent = 'Uploading to Stock…';

    try {
      const resp = await YTBot.apiFetch('/api/thumbnails/upload', { method: 'POST', body: formData });
      const data = await resp.json();
      YTBot.showToast(`Thumbnail added to stock! (${data.width}×${data.height})`, 'success');

      // Reload preserving the active channel so it instantly displays in the stock grid
      setTimeout(() => {
        window.location.href = `/api/thumbnails?channel_id=${channelId}`;
      }, 500);
    } catch (err) {
      YTBot.showToast(`Upload failed: ${err.message}`, 'error');
      submitBtn.disabled = false;
      submitBtn.textContent = '⬆️ Add to Thumbnail Stock';
    }
  });

  // ── Drag-and-drop Reordering in Stock ───────────────────────────────────
  if (grid) {
    let dragged = null;

    grid.addEventListener('dragstart', e => {
      dragged = e.target.closest('.thumb-item');
      if (dragged) {
        setTimeout(() => dragged.classList.add('dragging'), 0);
      }
    });

    grid.addEventListener('dragend', () => {
      dragged?.classList.remove('dragging');
      dragged = null;
      saveOrder();
    });

    grid.addEventListener('dragover', e => {
      e.preventDefault();
      const target = e.target.closest('.thumb-item');
      if (!target || target === dragged) return;

      const rect = target.getBoundingClientRect();
      const midX = rect.left + rect.width / 2;
      if (e.clientX < midX) {
        grid.insertBefore(dragged, target);
      } else {
        grid.insertBefore(dragged, target.nextSibling);
      }
    });

    // Delete from Stock
    grid.addEventListener('click', async e => {
      const btn = e.target.closest('[data-action="delete-thumb"]');
      if (!btn) return;
      const id = btn.dataset.id;
      await YTBot.deleteResource(`/api/thumbnails/${id}`, () => {
        btn.closest('.thumb-item')?.remove();
        YTBot.showToast('Removed from stock', 'info');
      });
    });
  }

  async function saveOrder() {
    const channelId = document.getElementById('channel-select')?.value;
    if (!channelId || !grid) return;

    const ids = [...grid.querySelectorAll('.thumb-item')].map(el => parseInt(el.dataset.id));
    const formData = new FormData();
    formData.append('channel_id', channelId);
    formData.append('ordered_ids', JSON.stringify(ids));

    try {
      await YTBot.apiFetch('/api/thumbnails/reorder', { method: 'POST', body: formData });
      YTBot.showToast('Stock rotation order updated', 'success');
    } catch (err) {
      YTBot.showToast(`Failed to update order: ${err.message}`, 'error');
    }
  }
});
